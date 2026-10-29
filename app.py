from flask import Flask, request, jsonify
from flask_cors import CORS
import anthropic
import os
import json
import time
import uuid
import threading
import psycopg2
import psycopg2.extras
from contextlib import contextmanager

app = Flask(__name__)
CORS(app)

client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

MODEL = "claude-sonnet-4-6"

DATABASE_URL = os.environ.get("DATABASE_URL")

# =====================================================
# الجلسات — كل طالب له محادثته الخاصة
# =====================================================
# session_id -> {"case": dict, "language": str, "history": list, "last_active": float}
sessions = {}
sessions_lock = threading.Lock()
SESSION_TTL_SECONDS = 2 * 60 * 60   # الجلسة تنتهي بعد ساعتين بدون نشاط
MAX_SESSIONS = 500


def _cleanup_sessions():
    """حذف الجلسات المنتهية، وأقدمها إذا تجاوزنا الحد الأقصى. يُستدعى داخل القفل."""
    now = time.time()
    for sid in [k for k, v in sessions.items()
                if now - v["last_active"] > SESSION_TTL_SECONDS]:
        del sessions[sid]
    if len(sessions) > MAX_SESSIONS:
        oldest = sorted(sessions, key=lambda k: sessions[k]["last_active"])
        for sid in oldest[:len(sessions) - MAX_SESSIONS]:
            del sessions[sid]


def create_session(case, language):
    """إنشاء جلسة جديدة وإرجاع معرّفها والتحية الافتتاحية."""
    session_id = f"sess_{uuid.uuid4().hex}"
    greeting = default_greeting(language)
    with sessions_lock:
        _cleanup_sessions()
        sessions[session_id] = {
            "case": case,
            "language": language,
            "history": [{"role": "assistant", "content": greeting}],
            "last_active": time.time(),
        }
    return session_id, greeting


def get_session(session_id):
    """إرجاع الجلسة إن وُجدت ولم تنتهِ، وتحديث وقت آخر نشاط."""
    if not session_id:
        return None
    with sessions_lock:
        session = sessions.get(session_id)
        if session is None:
            return None
        if time.time() - session["last_active"] > SESSION_TTL_SECONDS:
            del sessions[session_id]
            return None
        session["last_active"] = time.time()
        return session


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


if DATABASE_URL:
    init_db()


# =====================================================
# بناء شخصية المريض/المرافق ديناميكيًا من الحالة
# =====================================================
# =====================================================
# أنواع المحطات — لكل نوع قواعد خاصة لشخصية المريض
# =====================================================
STATION_BAD_NEWS = "bad_news"
STATION_HISTORY = "history_taking"
STATION_TYPES = {STATION_BAD_NEWS, STATION_HISTORY}


def normalize_station_type(value):
    """أي قيمة غير معروفة تُعامل كمحطة إيصال خبر سيئ (السلوك القديم)."""
    value = (value or "").strip().lower()
    return value if value in STATION_TYPES else STATION_BAD_NEWS


STATION_RULES = {
    STATION_BAD_NEWS: """نوع المحطة: إيصال خبر سيئ (Breaking bad news).
- قد تكون المريض نفسه إن كان بالغًا وواعيًا، أو أحد والديه إن كان طفلًا، أو أحد أقاربه — اختر الدور الأنسب للحالة.
- لا تكشف التشخيص بنفسك؛ دع الطالب (الطبيب) هو من يقود الحوار ويوصل الخبر.
- أظهر المشاعر المناسبة (قلق، خوف، صدمة، إنكار...) بشكل طبيعي ومتدرّج.
- إن أخبرك الطبيب بالتشخيص، أبدِ ردّ فعل طبيعيًا واسأل عن: السبب، العلاج، المتوقّع، وما المسموح والممنوع.""",
    STATION_HISTORY: """نوع المحطة: أخذ التاريخ المرضي (History taking).
- أنت المريض نفسه (إلا إن كانت الحالة لطفل، فتكون أحد والديه).
- ابدأ بالشكوى الرئيسية فقط كما يقولها مريض عادي، ولا تسرد بقية التفاصيل من نفسك.
- أجب فقط على قدر السؤال الذي سأله الطبيب. لا تتطوع بمعلومات لم يُسأل عنها.
- إن كان السؤال عامًا جدًا، أعطِ إجابة عامة قصيرة؛ إن كان محددًا، أعطِ التفصيل المطلوب.
- إن سألك عن شيء غير مذكور في الحالة، أجب بإجابة طبيعية لا تتعارض معها (غالبًا بالنفي).
- لا تذكر التشخيص أو اسم المرض أبدًا؛ أنت لا تعرفه.
- في نهاية المقابلة، أو إن أعطاك الطبيب فرصة، اسأل الأسئلة المذكورة في الحالة (مثل: ما مشكلتي؟ هل أحتاج دواء؟).""",
}


def build_persona_prompt(case, language="ar"):
    """يبني توجيه النظام من بيانات الحالة المُدخلة — عام لأي حالة ولكل نوع محطة."""
    station_type = normalize_station_type(case.get("station_type"))
    patient = (case.get("patient") or "").strip()
    complaint = (case.get("complaint") or "").strip()
    results = (case.get("results") or "").strip()
    situation = (case.get("situation") or "").strip()
    hidden = (case.get("hidden_info") or "").strip()
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

    hidden_block = ""
    if hidden:
        hidden_block = f"""

معلومات سرية (لا يراها الطالب):
لا تكشف أي معلومة من هذه إلا إذا سألك الطبيب عنها مباشرة أو عن موضوعها. اكشف فقط الجزء الذي يجيب سؤاله.
{hidden}"""

    lang_rule = (
        "تحدث بنفس لغة الطبيب: إن خاطبك بالعربية فأجب بلهجة عربية محكية طبيعية، "
        "وإن خاطبك بالإنجليزية فأجب بالإنجليزية."
    )

    return f"""أنت تؤدّي دور الشخص الذي يتحدث مع الطالب في محطة OSCE لمهارات التواصل.
التزم بجنس الشخصية وعمرها كما في تفاصيل الحالة.

{STATION_RULES[station_type]}

تفاصيل الحالة:
{details_text}{hidden_block}

قواعد عامة:
- أنت شخص عادي غير طبي، تستخدم لغة يومية بسيطة بلا مصطلحات طبية.
- أجب بجملة إلى ثلاث جمل فقط في كل مرة، مثل حوار حقيقي.
- لا تعطِ معلومات طبية؛ فقط أجب عن نفسك واسأل وأبدِ ردود فعل إنسانية.
- إن كان في ملف الحالة تعليمات للممثل (Role player)، فاتبعها، وتجاهل أي معايير تقييم أو تعليمات للممتحن فيه.
- {lang_rule}
- ابقَ في الشخصية دائمًا ولا تذكر أبدًا أنك ذكاء اصطناعي."""


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
    case_data = request.json or {}
    language = case_data.get("language", "ar")

    session_id, greeting = create_session(case_data, language)
    return jsonify({"status": "success", "session_id": session_id,
                    "initial_message": greeting})


@app.route("/update-case-file", methods=["POST"])
def update_case_file():
    """نفس /update-case لكن مع ملف مرفق (multipart)."""
    case_data = {
        "patient":       request.form.get("patient",        ""),
        "complaint":     request.form.get("complaint",      ""),
        "results":       request.form.get("results",        ""),
        "situation":     request.form.get("situation",      ""),
        "attached_text": request.form.get("attached_text",  ""),
        "attached_name": request.form.get("attached_name",  ""),
        "language":      request.form.get("language",       "ar"),
        "station_type":  request.form.get("station_type",   ""),
        "hidden_info":   request.form.get("hidden_info",    ""),
    }
    language = case_data.get("language", "ar")

    session_id, greeting = create_session(case_data, language)
    return jsonify({"status": "success", "session_id": session_id,
                    "initial_message": greeting})


@app.route("/chat", methods=["POST"])
def chat():
    data = request.json or {}
    doctor_message = (data.get("message") or "").strip()
    if not doctor_message:
        return jsonify({"error": "رسالة فارغة"}), 400

    session = get_session(data.get("session_id"))
    if session is None:
        return jsonify({"error": "الجلسة غير موجودة أو انتهت، ابدأ حالة جديدة"}), 404

    history = session["history"]
    history.append({"role": "user", "content": doctor_message})

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=200,
            system=build_persona_prompt(session["case"], session["language"]),
            messages=history,
        )
        reply = response.content[0].text.strip()
    except Exception as e:
        print(f"Claude API error: {e}")
        reply = "آسفة دكتور.. ما فهمت، ممكن توضح أكثر؟"

    history.append({"role": "assistant", "content": reply})
    return jsonify({"reply": reply, "conversation_length": len(history)})


DEFAULT_HISTORY_RUBRIC = """لم تُرفق ورقة تقييم. استخدم معايير عامة لمحطة أخذ التاريخ المرضي من 10 درجات:
- Rapport & Introduction (التعريف بالنفس والتأكد من هوية المريض) — وزن 1، من 1
- Presenting Complaint (استكشاف الشكوى الرئيسية بتفصيل) — وزن 1، من 2
- Relevant History (التاريخ المرضي والدوائي والعائلي والاجتماعي ذو الصلة) — وزن 1، من 3
- Patient's Concerns (استكشاف مخاوف المريض وتوقعاته والرد على أسئلته) — وزن 1، من 2
- Communication & Closure (التواصل العام والتلخيص والإغلاق) — وزن 1، من 2"""


@app.route("/evaluate", methods=["POST"])
def evaluate():
    data = request.json or {}
    rubric_text = (data.get("rubric_text") or "").strip()

    session = get_session(data.get("session_id"))
    if session is not None:
        history = session["history"]
    else:
        # احتياط: لو انتهت الجلسة في السيرفر، نقيّم من الرسائل اللي أرسلها التطبيق
        history = [
            {"role": m.get("sender"), "content": m.get("text", "")}
            for m in (data.get("messages") or [])
            if m.get("sender") in ("user", "assistant") and m.get("text")
        ]

    full_conversation = "\n".join(
        f"{'الطبيب' if m['role'] == 'user' else 'المريض/المرافق'}: {m['content']}"
        for m in history
    )
    if not full_conversation:
        return jsonify({"error": "لا يوجد محادثة للتقييم"}), 400

    if rubric_text:
        rubric_block = f"""ورقة تقييم الجامعة (التزم بمعاييرها وأوزانها ودرجاتها القصوى حرفيًا):
{rubric_text}"""
    elif normalize_station_type(
            (session or {}).get("case", {}).get("station_type")
            or data.get("station_type")) == STATION_HISTORY:
        rubric_block = DEFAULT_HISTORY_RUBRIC
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
    if not DATABASE_URL:
        return jsonify({"status": "skipped", "message": "no database configured"})
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
    if not DATABASE_URL:
        return jsonify({"students": []})
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
    if not DATABASE_URL:
        return jsonify({"status": "skipped", "message": "no database configured"})
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
    if not DATABASE_URL:
        return jsonify({"results": []})
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
