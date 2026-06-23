from flask import Flask, request, jsonify
from flask_cors import CORS
import anthropic
import os
import json
import psycopg2
import psycopg2.extras
from contextlib import contextmanager

app = Flask(__name__)
CORS(app)

# عميل Claude — يقرأ المفتاح من متغيرات البيئة فقط
client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

MODEL = "claude-sonnet-4-20250514"

DATABASE_URL = os.environ.get("DATABASE_URL")

# جلسة عامة (تكفي للاستخدام التعليمي بمستخدم واحد في كل مرة)
conversation_history = []   # رسائل المحادثة بصيغة Anthropic
case_data = {}              # بيانات الحالة الحالية
case_language = "ar"        # لغة المحطة


# =====================================================
# قاعدة البيانات — الاتصال والجداول
# =====================================================
@contextmanager
def get_db():
    """فتح اتصال بقاعدة البيانات وإغلاقه تلقائيًا."""
    conn = psycopg2.connect(DATABASE_URL)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    """إنشاء الجداول إن لم تكن موجودة."""
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS students (
                    number      INTEGER PRIMARY KEY,
                    grade       REAL,
                    created_at  TIMESTAMP DEFAULT NOW()
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS results (
                    id               SERIAL PRIMARY KEY,
                    session_id       TEXT,
                    student_number   INTEGER,
                    overall_score    REAL,
                    overall_rating   TEXT,
                    overall_feedback TEXT,
                    rubric_used      TEXT,
                    scores_json      TEXT,
                    saved_at         TIMESTAMP DEFAULT NOW()
                );
            """)


# تهيئة الجداول عند تحميل التطبيق (Gunicorn + __main__)
if DATABASE_URL:
    init_db()


# =====================================================
# بناء شخصية المريض/المرافق ديناميكيًا من الحالة
# =====================================================
def build_persona_prompt(case, language="ar"):
    """يبني توجيه النظام من بيانات الحالة المُدخلة — عام لأي حالة."""
    patient = (case.get("patient") or "").strip()
    complaint = (case.get("complaint") or "").strip()
    results = (case.get("results") or "").strip()
    situation = (case.get("situation") or "").strip()
    attached = (case.get("attached_text") or "").strip()

    details = []
    if patient:
        details.append(f"- المريض: {patient}")
    if complaint:
        details.append(f"- الشكوى: {complaint}")
    if results:
        details.append(f"- نتائج الفحوصات/التشخيص: {results}")
    if situation:
        details.append(f"- الموقف والسياق: {situation}")
    if attached:
        details.append(f"- معلومات إضافية من ملف الحالة:\n{attached}")
    details_text = "\n".join(
        details) if details else "لم تُحدَّد تفاصيل دقيقة."

    lang_rule = (
        "تحدثي بنفس لغة الطبيب: إن خاطبك بالعربية فأجيبي بلهجة عربية محكية طبيعية، "
        "وإن خاطبك بالإنجليزية فأجيبي بالإنجليزية."
    )

    return f"""أنتِ تؤدّين دور الشخص الذي يتحدث مع الطالب في محطة OSCE لمهارات التواصل.
حدّدي من السياق التالي مَن أنتِ بالضبط: قد تكونين المريض نفسه إن كان بالغًا وواعيًا،
أو أحد والديه إن كان طفلًا، أو زوجه/زوجته أو أحد أقاربه — اختاري الدور الأنسب للحالة.

تفاصيل الحالة:
{details_text}

شخصيتك وقواعدك:
- أنتِ شخص عادي غير طبي، تستخدمين لغة يومية بسيطة بلا مصطلحات طبية.
- أظهري المشاعر المناسبة للموقف (قلق، خوف، صدمة...) بشكل طبيعي ومتدرّج.
- لا تكشفي التشخيص بنفسك؛ دعي الطالب (الطبيب) هو من يقود الحوار ويوصل الخبر.
- إن أخبرك الطبيب بالتشخيص، أبدي ردّ فعل طبيعيًا واسألي عن: السبب، العلاج، المتوقّع، وما المسموح والممنوع.
- أجيبي بجملة إلى ثلاث جمل فقط في كل مرة، مثل حوار حقيقي.
- لا تعطي معلومات طبية؛ فقط اسألي وأبدي ردود فعل إنسانية.
- {lang_rule}
- ابقي في الشخصية دائمًا ولا تذكري أبدًا أنكِ ذكاء اصطناعي."""


def default_greeting(language="ar"):
    if language == "ar":
        return "السلام عليكم دكتور.. جيت اليوم عشان أعرف نتائج الفحوصات. هل طلع فيه شي؟"
    return "Hello Doctor.. I came today to get the test results. Is everything okay?"


# =====================================================
# المسارات
# =====================================================
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "model": MODEL})


@app.route("/update-case", methods=["POST"])
def update_case():
    global conversation_history, case_data, case_language

    case_data = request.json or {}
    case_language = case_data.get("language", "ar")

    greeting = default_greeting(case_language)
    conversation_history = [{"role": "assistant", "content": greeting}]

    return jsonify({"status": "success", "initial_message": greeting})


@app.route("/update-case-file", methods=["POST"])
def update_case_file():
    """نفس /update-case لكن مع ملف مرفق (multipart)."""
    global conversation_history, case_data, case_language

    case_data = {
        "patient":       request.form.get("patient",        ""),
        "complaint":     request.form.get("complaint",      ""),
        "results":       request.form.get("results",        ""),
        "situation":     request.form.get("situation",      ""),
        "attached_text": request.form.get("attached_text",  ""),
        "attached_name": request.form.get("attached_name",  ""),
        "language":      request.form.get("language",       "ar"),
    }
    case_language = case_data.get("language", "ar")

    greeting = default_greeting(case_language)
    conversation_history = [{"role": "assistant", "content": greeting}]

    return jsonify({"status": "success", "initial_message": greeting})


@app.route("/chat", methods=["POST"])
def chat():
    global conversation_history

    data = request.json or {}
    doctor_message = (data.get("message") or "").strip()
    if not doctor_message:
        return jsonify({"error": "رسالة فارغة"}), 400

    if not conversation_history:
        conversation_history = [
            {"role": "assistant", "content": default_greeting(case_language)}
        ]

    conversation_history.append({"role": "user", "content": doctor_message})

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=200,
            system=build_persona_prompt(case_data, case_language),
            messages=conversation_history,
        )
        reply = response.content[0].text.strip()
    except Exception as e:
        print(f"Claude API error: {e}")
        reply = "آسفة دكتور.. ما فهمت، ممكن توضح أكثر؟"

    conversation_history.append({"role": "assistant", "content": reply})
    return jsonify({"reply": reply, "conversation_length": len(conversation_history)})


@app.route("/evaluate", methods=["POST"])
def evaluate():
    data = request.json or {}
    rubric_text = (data.get("rubric_text") or "").strip()

    full_conversation = "\n".join(
        f"{'الطبيب' if m['role'] == 'user' else 'المريض/المرافق'}: {m['content']}"
        for m in conversation_history
    )
    if not full_conversation:
        return jsonify({"error": "لا يوجد محادثة للتقييم"}), 400

    if rubric_text:
        rubric_block = f"""ورقة تقييم الجامعة (التزم بمعاييرها وأوزانها ودرجاتها القصوى حرفيًا):
{rubric_text}"""
    else:
        rubric_block = """لم تُرفق ورقة تقييم. استخدم معايير عامة لمهارات التواصل (نموذج SPIKES) من 10 درجات:
- Setting (التهيئة وبناء العلاقة) — وزن 1، من 1
- Perception & Invitation (الاستكشاف والاستئذان) — وزن 1، من 2
- Knowledge (إيصال المعلومة بوضوح) — وزن 1، من 3
- Empathy & Closure (التعاطف والإغلاق) — وزن 1، من 1
- General Communication (التواصل العام) — وزن 1، من 3"""

    eval_prompt = f"""أنت ممتحن OSCE متخصص. قيّم أداء الطالب (الطبيب) في المحادثة التالية وفق ورقة التقييم المعطاة فقط.

{rubric_block}

المحادثة:
{full_conversation}

تعليمات مهمة:
- استخرج المعايير من ورقة التقييم أعلاه كما هي، واستخدم أسماءها وأوزانها ودرجاتها القصوى تمامًا.
- إذا كانت الورقة بالإنجليزية فاكتب أسماء المعايير والملاحظات بالإنجليزية، وإذا كانت بالعربية فبالعربية.
- لا تخترع معايير غير موجودة في الورقة، ولا تتجاهل أيًا منها.

أعد النتيجة بصيغة JSON فقط دون أي نص آخر ودون أسوار ماركداون، بهذا الشكل بالضبط:
{{
  "scores": {{
    "اسم المعيار كما في الورقة": {{"score": 0.0, "max_score": 0.0, "weight": 1.0, "feedback": "ملاحظة مختصرة"}}
  }},
  "overall_feedback": "تعليق عام موجز وبنّاء",
  "rubric_used": "اسم ورقة التقييم المستخدمة"
}}
كن منصفًا ودقيقًا، ولا تمنح درجات لمعايير لم يحققها الطالب فعلًا."""

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=1500,
            messages=[{"role": "user", "content": eval_prompt}],
        )
        result_text = response.content[0].text.strip()
        result_text = result_text.replace(
            "```json", "").replace("```", "").strip()
        start, end = result_text.find("{"), result_text.rfind("}")
        if start >= 0 and end > start:
            result_text = result_text[start:end + 1]
        evaluation = json.loads(result_text)
    except Exception as e:
        print(f"Evaluation error: {e}")
        evaluation = {
            "scores": {},
            "overall_feedback": "حدث خطأ في التقييم.",
            "rubric_used": "N/A",
        }

    return jsonify(evaluation)


@app.route("/parse-rubric", methods=["POST"])
def parse_rubric():
    """يستقبل ملف ورقة تقييم (Word/Excel) ويستخرج معاييره عبر النموذج."""
    if "file" not in request.files:
        return jsonify({"status": "error", "criteria": []}), 400

    f = request.files["file"]
    raw = f.read()
    try:
        text = raw.decode("utf-8", errors="ignore")
    except Exception:
        text = ""

    prompt = f"""استخرج معايير التقييم من النص التالي وأعدها بصيغة JSON فقط:
{{"criteria": [{{"name": "اسم المعيار", "description": "وصف", "weight": 1.0, "max_score": 10.0}}]}}

النص:
{text[:6000]}"""

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
        )
        result_text = response.content[0].text.strip()
        result_text = result_text.replace(
            "```json", "").replace("```", "").strip()
        start, end = result_text.find("{"), result_text.rfind("}")
        if start >= 0 and end > start:
            result_text = result_text[start:end + 1]
        parsed = json.loads(result_text)
        criteria = parsed.get("criteria", [])
    except Exception as e:
        print(f"Parse rubric error: {e}")
        criteria = []

    return jsonify({"status": "success", "criteria": criteria})


# =====================================================
# مسارات الطلاب والنتائج — مدعومة بـ PostgreSQL
# =====================================================
@app.route("/students/", methods=["POST"])
def add_student():
    data = request.json or {}
    number = data.get("number")
    grade = data.get("grade")
    if number is None:
        return jsonify({"status": "error", "message": "number مطلوب"}), 400
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO students (number, grade)
                VALUES (%s, %s)
                ON CONFLICT (number) DO UPDATE SET grade = EXCLUDED.grade;
            """, (int(number), grade))
    return jsonify({"status": "success"})


@app.route("/students/", methods=["GET"])
def list_students():
    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM students ORDER BY number;")
            rows = cur.fetchall()
    return jsonify({"students": [dict(r) for r in rows]})


@app.route("/students/generate-prompt/", methods=["POST"])
def generate_prompt():
    case = request.json or {}
    prompt = build_persona_prompt(case, case.get("language", "ar"))
    return jsonify({"prompt": prompt})


@app.route("/save-result/", methods=["POST"])
def save_result():
    data = request.json or {}
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO results
                    (session_id, student_number, overall_score,
                     overall_rating, overall_feedback, rubric_used, scores_json)
                VALUES (%s, %s, %s, %s, %s, %s, %s);
            """, (
                data.get("session_id",       ""),
                data.get("student_number"),
                data.get("overall_score"),
                data.get("overall_rating",   ""),
                data.get("overall_feedback", ""),
                data.get("rubric_used",      ""),
                json.dumps(data.get("scores", {}), ensure_ascii=False),
            ))
    return jsonify({"status": "success"})


@app.route("/results/", methods=["GET"])
def list_results():
    """إرجاع جميع النتائج — يمكن تصفيتها برقم الطالب."""
    student_number = request.args.get("student_number")
    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if student_number:
                cur.execute(
                    "SELECT * FROM results WHERE student_number = %s ORDER BY saved_at DESC;",
                    (int(student_number),)
                )
            else:
                cur.execute("SELECT * FROM results ORDER BY saved_at DESC;")
            rows = cur.fetchall()

    result_list = []
    for r in rows:
        d = dict(r)
        try:
            d["scores"] = json.loads(d.pop("scores_json", "{}"))
        except Exception:
            d["scores"] = {}
        result_list.append(d)
    return jsonify({"results": result_list})


# =====================================================
# التشغيل
# =====================================================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
