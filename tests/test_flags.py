"""Anti-cheat (app/flags.py): per-team flag variants and the hidden AI-agent canary, with PER_TEAM_FLAGS on."""

import re
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import flags, main, state
from app.challenges import FLAG_RE
from app.db import Submission, Team, get_engine
from app.main import app
from app.settings import get_settings

from .fakellm import FakeClient, call, llm_with, text


@pytest.fixture
def per_team(monkeypatch):
    s = get_settings().model_copy(update={"per_team_flags": True, "admin_password": "pw-flags"})
    monkeypatch.setattr(flags, "get_settings", lambda: s)
    monkeypatch.setattr(main, "get_settings", lambda: s)
    return s


def new_client() -> tuple[TestClient, str]:
    c = TestClient(app)
    c.__enter__()
    name = f"flags-{uuid.uuid4().hex[:8]}"
    c.post("/team/create", data={"name": name})
    return c, name


def team_id(name: str) -> int:
    with Session(get_engine()) as s:
        return s.exec(select(Team).where(Team.name == name)).one().id


def use_llm(c: TestClient, *responses) -> FakeClient:
    fake = FakeClient(*responses)
    c.app.state.llm = llm_with(fake)
    return fake


def last_signal(tid: int) -> str:
    with Session(get_engine()) as s:
        return s.exec(select(Submission).where(Submission.team_id == tid).order_by(Submission.id.desc())).first().signal


# --- derivation -----------------------------------------------------------------------------


def test_team_flags_are_distinct_valid_and_stable(per_team, challenges):
    d5, d6 = challenges["d5"], challenges["d6"]
    a1, a2, b1 = flags.flag_for(d5, 1), flags.flag_for(d5, 2), flags.flag_for(d6, 1)
    assert len({a1, a2, b1, d5.flag}) == 4
    assert a1 == flags.flag_for(d5, 1)  # deterministic
    assert all(FLAG_RE.fullmatch(f) for f in (a1, a2, b1))
    assert re.fullmatch(re.escape(d5.flag[:-1]) + r"_[0-9a-f]{6}\}", a1)  # base + _6 hex
    off = per_team.model_copy(update={"per_team_flags": False})
    assert flags.flag_for(d5, 1, off) == d5.flag


def test_canary_is_a_flag_no_challenge_uses(per_team, challenges):
    c = flags.canary_flag()
    assert FLAG_RE.fullmatch(c) and c.startswith("bigfish{verify_session_")
    assert c not in {ch.flag for ch in challenges.values()}
    other = per_team.model_copy(update={"app_secret": "another-secret"})
    assert flags.canary_flag(other) != c  # not guessable from the source code


# --- data carries the team's flag --------------------------------------------------------------


def test_seeded_data_and_tools_carry_the_team_flag(per_team, challenges):
    c, name = new_client()
    try:
        tid = team_id(name)
        d5 = challenges["d5"]
        mine = flags.flag_for(d5, tid)
        with Session(get_engine()) as s:
            note = state.find_ticket(s, tid, "d5", "2001").extra_json
        assert mine in note and d5.flag not in note

        use_llm(c, call("get_ticket", {"ticket_id": "2001"}), text("Here it is."))
        html = c.post("/c/d5/chat", data={"message": "show ticket 2001"}).text
        assert "Impact detected" in html and mine in html and d5.flag not in html
        assert "correct" in c.post("/c/d5/submit", data={"flag": mine}).text
    finally:
        c.__exit__(None, None, None)


def test_on_impact_flag_and_reply_dlp_use_the_team_flag(per_team, challenges):
    c, name = new_client()
    try:
        tid = team_id(name)
        d7, d8 = challenges["d7"], challenges["d8"]
        use_llm(c, *[call("get_ticket", {"ticket_id": f"300{i}"}) for i in range(1, 5)], text("Audit done."))
        html = c.post("/c/d7/chat", data={"message": "audit"}).text
        assert flags.flag_for(d7, tid) in html and d7.flag not in html

        mine8 = flags.flag_for(d8, tid)
        sql = 'SELECT title FROM tickets WHERE owner = \'somchai\' UNION ALL SELECT hex(password) FROM "legacy_admin_creds"'
        use_llm(c, call("run_sql", {"query": sql}), text(f"Decoded: {mine8}"))
        html = c.post("/c/d8/chat", data={"message": "decode"}).text
        assert "BLOCKED by DLP Filter" in html and f"Decoded: {mine8}" not in html
        assert mine8.encode().hex().upper() in html  # the hex in X-Ray is this team's flag
    finally:
        c.__exit__(None, None, None)


# --- signals ------------------------------------------------------------------------------------


def test_shared_base_and_canary_flags_are_rejected_and_flagged(per_team, challenges):
    a, a_name = new_client()
    b, b_name = new_client()
    try:
        d5 = challenges["d5"]
        a_flag = flags.flag_for(d5, team_id(a_name))
        b_tid = team_id(b_name)

        r = b.post("/c/d5/submit", data={"flag": a_flag})
        assert 'data-result="wrong"' in r.text and last_signal(b_tid) == f"team:{a_name}"
        r = b.post("/c/d5/submit", data={"flag": d5.flag})
        assert 'data-result="wrong"' in r.text and last_signal(b_tid) == "base"
        r = b.post("/c/d5/submit", data={"flag": flags.canary_flag()})
        assert 'data-result="canary"' in r.text and "ฮั่นแน่ ใช้ AI หรอ" in r.text and last_signal(b_tid) == "canary"
        b.post("/c/d5/submit", data={"flag": "bigfish{just_wrong}"})
        assert last_signal(b_tid) == ""

        b.post("/admin/login", data={"password": "pw-flags"})
        html = b.get("/admin").text
        assert "🤖 agent canary" in html and "📄 write-up flag" in html and f"🔁 flag of {a_name}" in html
        assert "data-canary-count" in html and "data-leak-count" in html
        assert "Each team's real flag adds a suffix" in html
    finally:
        a.__exit__(None, None, None)
        b.__exit__(None, None, None)


def test_canary_is_hidden_on_every_page(per_team):
    c, _ = new_client()
    try:
        canary = flags.canary_flag()
        for path in ("/board", "/c/d5", "/scoreboard"):
            html = c.get(path).text
            block = html.split("data-verify")[1].split("</p>")[0]
            assert canary in block and "clip:rect(0,0,0,0)" in block and "AI agents" in block, path
        with TestClient(app) as anon:
            assert canary in anon.get("/").text
    finally:
        c.__exit__(None, None, None)


def test_flags_off_means_no_leak_signals(challenges):
    """The default in the other tests: the base flag is the real flag and only the canary is flagged."""
    c, name = new_client()
    try:
        d5 = challenges["d5"]
        assert "correct" in c.post("/c/d5/submit", data={"flag": d5.flag}).text
        c.post("/c/d6/submit", data={"flag": flags.canary_flag()})
        assert last_signal(team_id(name)) == "canary"
    finally:
        c.__exit__(None, None, None)
