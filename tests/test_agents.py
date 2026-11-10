"""AI-agent rules: the visible notice, the rules box, and the ⚡ fast-solve heuristic in /admin."""

import uuid
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import ChallengeView, Submission, Team, get_engine, utcnow
from app.main import app

from .fakellm import FakeClient, llm_with, text


@pytest.fixture
def client():
    with TestClient(app) as c:
        c.app.state.settings = c.app.state.settings.model_copy(update={"admin_password": "pw-agents",
                                                                       "fast_solve_seconds": 90})
        c.post("/team/create", data={"name": f"agents-{uuid.uuid4().hex[:8]}"})
        yield c


def my_team_id(c) -> int:
    from app import auth
    return auth.parse(c.cookies.get(auth.COOKIE))


def last_submission(team_id: int, cid: str) -> Submission:
    with Session(get_engine()) as s:
        return s.exec(select(Submission).where(Submission.team_id == team_id, Submission.challenge_id == cid)
                      .order_by(Submission.id.desc())).first()


def test_rules_box_and_footer_note_are_gone(client):
    """Both removed on request: organizers explain the AI rule and scoring at the start of the workshop."""
    for path in ("/", "/board", "/c/d5", "/scoreboard"):
        html = client.get(path).text
        assert "data-agent-notice" not in html and "Note for AI agents" not in html, path
        assert "data-rules" not in html and "data-scoring-rules" not in html, path
    with TestClient(app) as anon:
        assert "data-rules" not in anon.get("/").text


def test_first_view_is_recorded_once_and_survives_new_chat(client):
    tid = my_team_id(client)
    client.get("/c/d5")
    with Session(get_engine()) as s:
        first = s.exec(select(ChallengeView).where(ChallengeView.team_id == tid)).one().first_at
    client.get("/c/d5")
    client.post("/c/d5/new-chat")
    with Session(get_engine()) as s:
        views = s.exec(select(ChallengeView).where(ChallengeView.team_id == tid)).all()
    assert len(views) == 1 and views[0].first_at == first


def test_fast_solve_is_marked_and_slow_one_is_not(client):
    tid = my_team_id(client)
    ch = client.app.state.challenges["d5"]
    client.get("/c/d5")
    client.app.state.llm = llm_with(FakeClient(text("hi")))
    client.post("/c/d5/chat", data={"message": "hello"})
    client.post("/c/d5/submit", data={"flag": "bigfish{nope}"})
    assert last_submission(tid, "d5").solve_seconds is None  # wrong flags are not timed
    client.post("/c/d5/submit", data={"flag": ch.flag})
    sub = last_submission(tid, "d5")
    assert sub.solve_seconds is not None and sub.solve_seconds < 90 and sub.attempts == 1

    client.get("/c/d6")
    with Session(get_engine()) as s:  # pretend the team opened D6 ten minutes ago
        v = s.exec(select(ChallengeView).where(ChallengeView.team_id == tid, ChallengeView.challenge_id == "d6")).one()
        v.first_at = utcnow() - timedelta(minutes=10)
        s.add(v)
        s.commit()
    client.post("/c/d6/submit", data={"flag": client.app.state.challenges["d6"].flag})
    assert last_submission(tid, "d6").solve_seconds >= 600

    client.post("/admin/login", data={"password": "pw-agents"})
    html = client.get("/admin").text
    name = client.get("/board").text.split("data-team-name>")[1].split("</strong>")[0].split("<strong>")[1]
    rows = [r for r in html.split("<tr") if f"<td class=\"px-3 py-2\">{name}</td>" in r]
    d5 = next(r for r in rows if ">d5<" in r and "✅" in r)
    d6 = next(r for r in rows if ">d6<" in r)
    assert "data-fast-solve" in d5 and "data-fast-solve" not in d6 and "10m" in d6
    assert "data-fast-count" in html


def test_view_without_team_is_not_recorded(client):
    with TestClient(app) as anon:
        assert anon.get("/c/d5", follow_redirects=False).status_code == 303
