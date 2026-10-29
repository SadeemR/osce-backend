"""Station type, hidden info and database-optional tests — run with: pytest"""
import os
import sys

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as A  # noqa: E402
from test_sessions import env, FakeMessages  # noqa: E402,F401


def test_default_station_is_bad_news():
    prompt = A.build_persona_prompt({"patient": "Ahmad"})
    assert "Breaking bad news" in prompt
    assert "History taking" not in prompt


def test_unknown_station_falls_back_to_bad_news():
    assert A.normalize_station_type("something-else") == A.STATION_BAD_NEWS
    assert A.normalize_station_type(None) == A.STATION_BAD_NEWS
    assert A.normalize_station_type(" History_Taking ") == A.STATION_HISTORY


def test_history_taking_rules():
    prompt = A.build_persona_prompt(
        {"patient": "Shady, 29", "station_type": "history_taking"})
    assert "History taking" in prompt
    assert "Breaking bad news" not in prompt
    assert "لا تتطوع" in prompt


def test_hidden_info_is_given_to_patient_with_reveal_rule():
    prompt = A.build_persona_prompt(
        {"patient": "Shady", "hidden_info": "Cough starts at work, dust exposure"})
    assert "Cough starts at work, dust exposure" in prompt
    assert "إلا إذا سألك الطبيب" in prompt


def test_no_hidden_block_when_empty():
    prompt = A.build_persona_prompt({"patient": "Shady"})
    assert "معلومات سرية" not in prompt


def test_station_type_and_hidden_info_from_json_and_form(env):
    c, fake = env
    a = c.post("/update-case", json={
        "patient": "Shady", "station_type": "history_taking",
        "hidden_info": "SECRET-JSON"}).get_json()
    c.post("/chat", json={"session_id": a["session_id"], "message": "hi"})
    assert "History taking" in fake.calls[-1]["system"]
    assert "SECRET-JSON" in fake.calls[-1]["system"]

    b = c.post("/update-case-file", data={
        "patient": "Sara", "station_type": "history_taking",
        "hidden_info": "SECRET-FORM"}).get_json()
    c.post("/chat", json={"session_id": b["session_id"], "message": "hi"})
    assert "History taking" in fake.calls[-1]["system"]
    assert "SECRET-FORM" in fake.calls[-1]["system"]


def test_default_rubric_follows_station_type(env):
    c, fake = env
    h = c.post("/update-case", json={"patient": "Shady",
                                     "station_type": "history_taking"}).get_json()
    c.post("/chat", json={"session_id": h["session_id"], "message": "hi"})
    c.post("/evaluate", json={"session_id": h["session_id"]})
    assert "Presenting Complaint" in fake.calls[-1]["messages"][0]["content"]

    b = c.post("/update-case", json={"patient": "Ahmad"}).get_json()
    c.post("/chat", json={"session_id": b["session_id"], "message": "hi"})
    c.post("/evaluate", json={"session_id": b["session_id"]})
    assert "SPIKES" in fake.calls[-1]["messages"][0]["content"]


def test_uploaded_rubric_wins_over_default(env):
    c, fake = env
    h = c.post("/update-case", json={"station_type": "history_taking"}).get_json()
    c.post("/chat", json={"session_id": h["session_id"], "message": "hi"})
    c.post("/evaluate", json={"session_id": h["session_id"], "rubric_text": "MY-RUBRIC"})
    prompt = fake.calls[-1]["messages"][0]["content"]
    assert "MY-RUBRIC" in prompt and "Presenting Complaint" not in prompt


def test_database_endpoints_work_without_database(env, monkeypatch):
    c, _ = env
    monkeypatch.setattr(A, "DATABASE_URL", None)
    assert c.post("/save-result/", json={"session_id": "x"}).status_code == 200
    assert c.post("/students/", json={"number": 1, "grade": 90}).status_code == 200
    assert c.get("/students/").get_json() == {"students": []}
    assert c.get("/results/").get_json() == {"results": []}


def test_bad_news_keeps_fixed_greeting(env):
    c, fake = env
    r = c.post("/update-case", json={"patient": "Ahmad", "language": "en"}).get_json()
    assert r["initial_message"] == A.default_greeting("en")
    assert fake.calls == []  # no model call needed


def test_history_taking_opening_comes_from_the_case(env):
    c, fake = env
    r = c.post("/update-case", json={
        "patient": "Shady", "station_type": "history_taking",
        "language": "en"}).get_json()
    assert "test results" not in r["initial_message"]
    assert len(fake.calls) == 1
    assert "Answer in English" in fake.calls[0]["messages"][0]["content"]
    assert "History taking" in fake.calls[0]["system"]
    # the opening line becomes the first turn of that session
    c.post("/chat", json={"session_id": r["session_id"], "message": "Hi"})
    first = fake.calls[-1]["messages"][0]
    assert first["role"] == "assistant"
    assert first["content"] == r["initial_message"]


def test_history_opening_falls_back_on_error(env, monkeypatch):
    c, fake = env

    def boom(**kw):
        raise RuntimeError("api down")
    monkeypatch.setattr(fake, "create", boom)
    r = c.post("/update-case", json={"station_type": "history_taking",
                                     "language": "ar"}).get_json()
    assert r["initial_message"] == "السلام عليكم دكتور."


class _Resp:
    def __init__(self, text, stop_reason="end_turn"):
        import types
        self.content = [types.SimpleNamespace(text=text)]
        self.stop_reason = stop_reason


GOOD = ('{"scores": {"History": {"score": 2, "max_score": 3, "weight": 1, '
        '"feedback": "ok"}}, "overall_feedback": "fine", "rubric_used": "x"}')


def _start_and_chat(c):
    r = c.post("/update-case", json={"patient": "Ahmad"}).get_json()
    c.post("/chat", json={"session_id": r["session_id"], "message": "hi"})
    return r["session_id"]


def test_evaluation_retries_when_truncated(env, monkeypatch):
    c, fake = env
    sid = _start_and_chat(c)
    replies = iter([_Resp('{"scores": {"Hist', "max_tokens"), _Resp(GOOD)])
    monkeypatch.setattr(fake, "create", lambda **kw: next(replies))
    r = c.post("/evaluate", json={"session_id": sid})
    assert r.status_code == 200
    assert r.get_json()["scores"]["History"]["score"] == 2


def test_evaluation_tolerates_markdown_fences(env, monkeypatch):
    c, fake = env
    sid = _start_and_chat(c)
    monkeypatch.setattr(fake, "create",
                        lambda **kw: _Resp("Here you go:\n```json\n" + GOOD + "\n```"))
    assert c.post("/evaluate", json={"session_id": sid}).status_code == 200


def test_evaluation_failure_is_an_error_not_a_zero_score(env, monkeypatch):
    c, fake = env
    sid = _start_and_chat(c)
    monkeypatch.setattr(fake, "create", lambda **kw: _Resp("not json at all"))
    r = c.post("/evaluate", json={"session_id": sid})
    assert r.status_code == 502
    assert "error" in r.get_json()


def test_evaluation_with_empty_scores_is_retried_then_errors(env, monkeypatch):
    c, fake = env
    sid = _start_and_chat(c)
    calls = []

    def create(**kw):
        calls.append(kw)
        return _Resp('{"scores": {}, "overall_feedback": "", "rubric_used": ""}')
    monkeypatch.setattr(fake, "create", create)
    assert c.post("/evaluate", json={"session_id": sid}).status_code == 502
    assert len(calls) == 2
    assert calls[0]["max_tokens"] >= 4096


def test_hidden_info_must_be_revealed_exactly():
    prompt = A.build_persona_prompt(
        {"patient": "Um Khalid", "hidden_info": "Her brother has diabetes and lost his leg"})
    assert "قلها كما هي بالضبط" in prompt
    assert "لا تخترع حقائق تتعارض" in prompt


def test_patient_answers_are_kept_short():
    for st in ("bad_news", "history_taking"):
        prompt = A.build_persona_prompt({"patient": "x", "station_type": st})
        assert '"نعم" أو "لا"' in prompt
        assert "لا تطرح أكثر من سؤال واحد" in prompt
