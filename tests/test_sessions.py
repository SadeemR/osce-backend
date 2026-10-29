"""Session isolation tests — run with: pytest"""
import os
import sys
import types

import pytest

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as A  # noqa: E402


class FakeMessages:
    """Stands in for the Claude API so tests run offline and free."""

    def __init__(self):
        self.calls = []

    def create(self, **kw):
        self.calls.append(kw)
        last = kw["messages"][-1]["content"]
        if "ممتحن" in last:  # evaluation prompt
            text = ('{"scores": {"Rapport": {"score": 1, "max_score": 2, '
                    '"weight": 1, "feedback": "ok"}}, '
                    '"overall_feedback": "ok", "rubric_used": "x"}')
        else:
            text = f"reply to: {last}"
        return types.SimpleNamespace(content=[types.SimpleNamespace(text=text)])


@pytest.fixture
def env(monkeypatch):
    fake = FakeMessages()
    monkeypatch.setattr(A.client, "messages", fake)
    A.sessions.clear()
    return A.app.test_client(), fake


def start(c):
    a = c.post("/update-case", json={"patient": "Ahmad", "language": "ar"}).get_json()
    b = c.post("/update-case-file", data={"patient": "Sara", "language": "en"}).get_json()
    return a, b


def test_each_case_gets_its_own_session(env):
    c, _ = env
    a, b = start(c)
    assert a["session_id"] != b["session_id"]
    assert a["initial_message"]


def test_conversations_do_not_leak_between_students(env):
    c, fake = env
    a, b = start(c)
    c.post("/chat", json={"session_id": a["session_id"], "message": "A1"})
    c.post("/chat", json={"session_id": b["session_id"], "message": "B1"})
    r = c.post("/chat", json={"session_id": a["session_id"], "message": "A2"}).get_json()

    assert r["conversation_length"] == 5
    sent = [m["content"] for m in fake.calls[-1]["messages"]]
    assert not any("B1" in m for m in sent)
    assert "Ahmad" in fake.calls[-1]["system"]
    assert "Sara" in fake.calls[1]["system"]


def test_invalid_requests(env):
    c, _ = env
    a, _ = start(c)
    assert c.post("/chat", json={"session_id": "nope", "message": "x"}).status_code == 404
    assert c.post("/chat", json={"message": "x"}).status_code == 404
    assert c.post("/chat", json={"session_id": a["session_id"], "message": ""}).status_code == 400


def test_evaluation_uses_only_that_session(env):
    c, fake = env
    a, b = start(c)
    c.post("/chat", json={"session_id": a["session_id"], "message": "A1"})
    c.post("/chat", json={"session_id": b["session_id"], "message": "B1"})
    e = c.post("/evaluate", json={"session_id": a["session_id"]}).get_json()
    prompt = fake.calls[-1]["messages"][0]["content"]
    assert "A1" in prompt and "B1" not in prompt
    assert e["overall_feedback"] == "ok"


def test_evaluation_falls_back_to_client_messages(env):
    c, fake = env
    c.post("/evaluate", json={"session_id": "gone",
                              "messages": [{"sender": "user", "text": "FALLBACK"}]})
    assert "FALLBACK" in fake.calls[-1]["messages"][0]["content"]
    assert c.post("/evaluate", json={"session_id": "gone"}).status_code == 400


def test_sessions_expire(env):
    c, _ = env
    a, _ = start(c)
    A.sessions[a["session_id"]]["last_active"] -= A.SESSION_TTL_SECONDS + 1
    r = c.post("/chat", json={"session_id": a["session_id"], "message": "late"})
    assert r.status_code == 404
