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
