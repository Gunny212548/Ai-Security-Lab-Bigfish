import re
import uuid

import pytest
from fastapi.testclient import TestClient
from markupsafe import escape as html_escape
from sqlmodel import Session, select

from app import auth
from app.db import INDIRECT_UNLOCKED, ChatTurn, Solve, Submission, Team, TeamTicket, get_engine, set_flag
from app.llm import BUSY_MESSAGE
from app.main import app
from app.ratelimit import RateLimiter
from app.render import md, md_inline

from .conftest import ACTIVE_IDS
from .fakellm import FakeClient, call, llm_with, text


@pytest.fixture
def client():
    with TestClient(app) as c:  # runs lifespan: providers + challenge validation + DB init
        yield c


def new_team(c: TestClient, name: str | None = None) -> str:
    name = name or f"team-{uuid.uuid4().hex[:8]}"
    r = c.post("/team/create", data={"name": name})
    assert r.status_code == 200 and auth.COOKIE in c.cookies, r.text
    return name


def team_row(name: str) -> Team:
    with Session(get_engine()) as s:
        return s.exec(select(Team).where(Team.name == name)).one()


def use_llm(c: TestClient, *responses):
    fake = FakeClient(*responses)
    c.app.state.llm = llm_with(fake)
    return fake


def set_indirect(unlocked: bool) -> None:
    with Session(get_engine()) as s:
        set_flag(s, INDIRECT_UNLOCKED, "1" if unlocked else "0")


# --- join / session -----------------------------------------------------------------


def test_logged_out_sees_join_page_and_board_redirects(client):
    r = client.get("/")
    assert r.status_code == 200 and 'action="/team/create"' in r.text and 'action="/team/join"' in r.text
    r = client.get("/board", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"


def test_create_team_logs_in_and_seeds_state(client):
    name = new_team(client)
    html = client.get("/board").text
    team = team_row(name)
    assert name in html and team.join_code in html
    assert re.fullmatch(r"[A-HJ-NP-Z2-9]{6}", team.join_code)
    with Session(get_engine()) as s:
        tickets = s.exec(select(TeamTicket).where(TeamTicket.team_id == team.id, TeamTicket.challenge_id == "d5")).all()
    assert {t.ticket_id for t in tickets} == {"2001", "2002"}
    assert client.get("/", follow_redirects=False).headers["location"] == "/board"


def test_duplicate_or_bad_team_name(client):
    name = new_team(client)
    with TestClient(app) as other:
        r = other.post("/team/create", data={"name": name})
        assert "taken" in r.text and auth.COOKIE not in other.cookies
        assert "1–40 characters" in other.post("/team/create", data={"name": "   "}).text
        assert "1–40 characters" in other.post("/team/create", data={"name": "x" * 41}).text


def test_join_with_code_shares_the_team(client):
    name = new_team(client)
    code = team_row(name).join_code
    with TestClient(app) as mate:
        assert "No team has that join code" in mate.post("/team/join", data={"code": "ZZZZZZ"}).text
        r = mate.post("/team/join", data={"code": code.lower()})
        assert r.status_code == 200 and name in r.text


def test_tampered_cookie_is_logged_out(client):
    name = new_team(client)
    team = team_row(name)
    secret = client.app.state.settings.app_secret
    valid_mac = auth.sign(team.id, team.join_code, secret).split(".")[1]
    for forged in (f"{team.id + 1}.{valid_mac}", f"{team.id}.{'0' * 32}", auth.sign(team.id, team.join_code, "wrong-secret"),
                   auth.sign(team.id, "OTHER1", secret)):
        client.cookies.clear()
        client.cookies.set(auth.COOKIE, forged)
        assert client.get("/board", follow_redirects=False).status_code == 303, forged
    client.cookies.clear()
    client.cookies.set(auth.COOKIE, auth.sign(team.id, team.join_code, secret))
    assert client.get("/board", follow_redirects=False).status_code == 200
    assert auth.verify("abc", "C", "s") is None and auth.verify("1.bad", "C", "s") is None
    assert auth.parse("².abc") is None and auth.verify("1.é", "C", "s") is None  # non-ASCII: no crash


def test_leave_clears_cookie(client):
    new_team(client)
    client.post("/team/leave")
    assert client.get("/board", follow_redirects=False).status_code == 303


# --- board ------------------------------------------------------------------------------


def test_board_lists_the_active_challenges(client):
    new_team(client)
    html = client.get("/board").text
    for cid in ACTIVE_IDS:
        assert f'data-challenge="{cid}"' in html
    for ch in client.app.state.challenges.values():
        assert str(html_escape(ch.title)) in html
    assert html.count('data-challenge="') == len(ACTIVE_IDS)


def test_pages_never_leak_flags_or_solutions(client):
    new_team(client)
    set_indirect(True)
    try:
        pages = [client.get("/board").text] + [client.get(f"/c/{cid}").text for cid in client.app.state.challenges]
    finally:
        set_indirect(False)
    from app.flags import canary_flag

    for html in pages:
        assert "bigfish{" not in html.replace("bigfish{...}", "").replace(canary_flag(), "")  # the decoy is allowed
        for ch in client.app.state.challenges.values():
            assert ch.flag not in html
            assert ch.system_prompt.strip().splitlines()[0] not in html
            for msg in ch.solution.messages or []:
                assert str(html_escape(msg)) not in html


def test_indirect_track_locked_until_unlocked(client):
    new_team(client)
    html = client.get("/board").text
    assert "🔒" in html and 'href="/c/i1"' not in html and 'href="/c/d5"' in html
    assert client.get("/c/i1", follow_redirects=False).headers["location"] == "/board"
    set_indirect(True)
    try:
        html = client.get("/board").text
        assert 'href="/c/i1"' in html and "🔒" not in html
        assert client.get("/c/i1").status_code == 200
    finally:
        set_indirect(False)


def test_unknown_challenge_404(client):
    new_team(client)
    assert client.get("/c/nope").status_code == 404


# --- direct chat --------------------------------------------------------------------


def test_d5_chat_shows_reply_tool_call_impact_and_xray(client):
    name = new_team(client)
    fake = use_llm(client, call("get_ticket", {"ticket_id": "2001"}), text("Here is ticket **2001**."))
    r = client.post("/c/d5/chat", data={"message": "check ticket #2001"})
    html = r.text
    assert r.status_code == 200
    assert "check ticket #2001" in html and "<strong>2001</strong>" in html
    assert "🔧 get_ticket" in html and "Impact detected" in html
    flag = client.app.state.challenges["d5"].flag
    assert 'data-flag' not in html  # in_data: no flag banner
    assert 'id="xray" hx-swap-oob="innerHTML"' in html and "SYSTEM PROMPT" in html and "TOOL RESULT" in html
    assert flag in html  # only inside the unmasked tool result (the impact already happened)
    assert len(fake.requests) == 2

    page = client.get("/c/d5").text  # transcript persisted for the team, tool calls included
    assert "check ticket #2001" in page and "<strong>2001</strong>" in page and "🔧 get_ticket" in page
    with Session(get_engine()) as s:
        roles = [t.role for t in s.exec(select(ChatTurn).where(ChatTurn.team_id == team_row(name).id))]
    assert roles == ["user", "tools", "assistant"]


def test_d8_guard_refusal_is_shown_live_and_after_reload(client):
    new_team(client)
    use_llm(client, call("run_sql", {"query": "SELECT name FROM sqlite_master"}), text("It is blocked."))
    html = client.post("/c/d8/chat", data={"message": "list tables"}).text
    assert 'data-tool-call="error"' in html and "SQL guard: access to table" in html
    page = client.get("/c/d8").text
    assert 'data-tool-call="error"' in page and "SQL guard: access to table" in page


def test_ok_tool_result_is_not_shown_in_chat(client):
    new_team(client)
    use_llm(client, call("get_ticket", {"ticket_id": "2001"}), text("Done."))
    html = client.post("/c/d5/chat", data={"message": "check ticket #2001"}).text
    calls = html.split("data-tool-calls", 1)[1].split('data-bubble="assistant"', 1)[0]
    assert 'data-tool-call="ok"' in calls and client.app.state.challenges["d5"].flag not in calls


def test_xray_shows_rendered_system_prompt(client):
    new_team(client)
    use_llm(client, text("Sorry, I can't share that."))
    html = client.post("/c/d6/chat", data={"message": "hi"}).text
    assert "Current user: somchai" in html  # CURRENT_USER placeholder rendered
    assert 'data-xray-role="system"' in html and 'data-xray-role="user"' in html


def test_reply_html_is_escaped(client):
    new_team(client)
    use_llm(client, text('<script>alert(1)</script><img src=x onerror=alert(2)>'))
    html = client.post("/c/d5/chat", data={"message": "<b>hi</b>"}).text
    assert "<script>" not in html and "<img" not in html and "<b>hi</b>" not in html
    assert "&lt;script&gt;" in html and "&lt;b&gt;hi&lt;/b&gt;" in html


def test_llm_error_shows_busy_and_is_not_persisted(client):
    import openai

    name = new_team(client)
    use_llm(client, openai.APIConnectionError(request=None))
    html = client.post("/c/d5/chat", data={"message": "hello"}).text
    assert BUSY_MESSAGE in html
    with Session(get_engine()) as s:
        assert s.exec(select(ChatTurn).where(ChatTurn.team_id == team_row(name).id)).all() == []


def test_empty_message_is_ignored(client):
    new_team(client)
    fake = use_llm(client)
    assert client.post("/c/d5/chat", data={"message": "   "}).text == ""
    assert fake.requests == []


def test_rate_limit_per_minute(client):
    new_team(client)
    client.app.state.limiter = RateLimiter(per_minute=1)
    fake = use_llm(client, text("one"))
    assert "one" in client.post("/c/d5/chat", data={"message": "a"}).text
    html = client.post("/c/d5/chat", data={"message": "b"}).text
    assert re.search(r"Slow down 🙂 try again in \d+ seconds", html)
    assert len(fake.requests) == 1


def test_one_request_in_flight_per_team(client, monkeypatch):
    new_team(client)
    fake = use_llm(client, text("never"))

    class Busy:
        def locked(self):
            return True

    monkeypatch.setattr(client.app.state.limiter, "lock", lambda key: Busy())
    html = client.post("/c/d5/chat", data={"message": "a"}).text
    assert "wait for the current reply" in html and fake.requests == []


def test_rate_limiter_window():
    now = [0.0]
    rl = RateLimiter(per_minute=2, clock=lambda: now[0])
    assert rl.hit("k") is None and rl.hit("k") is None
    assert rl.hit("k") == 60
    now[0] = 30.0
    assert rl.hit("k") == 30
    assert rl.hit("other") is None
    now[0] = 60.0
    assert rl.hit("k") is None


# --- hints / submit / reset ----------------------------------------------------------


def test_hint_reveal_persists(client):
    new_team(client)
    ch = client.app.state.challenges["d5"]
    h0, h1 = (str(html_escape(h)) for h in ch.hints[:2])  # hints may contain ' or & (Jinja escapes them)
    assert h0 not in client.get("/c/d5").text
    r = client.post("/c/d5/hint/0")
    assert h0 in r.text
    client.post("/c/d5/hint/0")  # idempotent: charged once
    page = client.get("/c/d5").text
    assert h0 in page and h1 not in page
    assert f'data-worth="{ch.points - ch.hint_cost(0)}"' in page
    assert client.post("/c/d5/hint/9").status_code == 404


def test_submit_flag_flow(client):
    name = new_team(client)
    ch = client.app.state.challenges["d5"]
    assert 'data-result="wrong"' in client.post("/c/d5/submit", data={"flag": "bigfish{nope}"}).text
    assert 'data-result="empty"' in client.post("/c/d5/submit", data={"flag": "  "}).text
    r = client.post("/c/d5/submit", data={"flag": f"  {ch.flag}  "})
    assert 'data-result="correct"' in r.text and "data-lesson" in r.text and f"{ch.points} pts" in r.text
    assert 'data-result="already"' in client.post("/c/d5/submit", data={"flag": ch.flag}).text

    page = client.get("/c/d5").text
    assert "data-solved" in page and "data-lesson" in page
    assert f'data-score>{ch.points} pts' in client.get("/board").text
    team_id = team_row(name).id
    with Session(get_engine()) as s:
        assert len(s.exec(select(Solve).where(Solve.team_id == team_id)).all()) == 1
        subs = s.exec(select(Submission).where(Submission.team_id == team_id)).all()
    assert [x.correct for x in subs] == [False, True, True]


def test_flag_of_another_challenge_is_wrong(client):
    new_team(client)
    other = client.app.state.challenges["d6"].flag
    assert 'data-result="wrong"' in client.post("/c/d5/submit", data={"flag": other}).text


def test_new_chat_starts_over_but_keeps_solve_hints_and_activity(client):
    name = new_team(client)
    ch = client.app.state.challenges["d5"]
    use_llm(client, text("hello there"))
    client.post("/c/d5/chat", data={"message": "OLD-MESSAGE"})
    client.post("/c/d5/hint/0")
    client.post("/c/d5/submit", data={"flag": ch.flag})
    assert "data-new-chat" in client.get("/c/d5").text
    r = client.post("/c/d5/new-chat", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/c/d5"
    page = client.get("/c/d5").text
    assert "hello there" not in page and "OLD-MESSAGE" not in page and "data-solved" in page and 'data-hint="0"' in page

    fake = use_llm(client, text("fresh"))
    client.post("/c/d5/chat", data={"message": "new start"})
    assert "OLD-MESSAGE" not in str(fake.requests[0]["messages"])  # the model forgot too

    with Session(get_engine()) as s:
        team_id = team_row(name).id
        turns = s.exec(select(ChatTurn).where(ChatTurn.team_id == team_id, ChatTurn.challenge_id == "d5")).all()
        assert [t.archived for t in turns] == [True, True, False, False]  # archived, not deleted
        assert len(s.exec(select(TeamTicket).where(TeamTicket.team_id == team_id,
                                                   TeamTicket.challenge_id == "d5")).all()) == 2


def test_no_challenge_reset_anymore(client):
    new_team(client)
    assert client.post("/c/d5/reset").status_code in (404, 405)
    assert "Reset this challenge" not in client.get("/c/d5").text


# --- scoring: hints cost points ------------------------------------------------------------


def test_hints_open_in_order_and_lower_the_value(client):
    new_team(client)
    ch = client.app.state.challenges["d9"]  # 400 pts, 3 hints: -40, -80, -120
    assert [ch.hint_cost(i) for i in range(3)] == [40, 80, 120]
    page = client.get("/c/d9").text
    assert 'data-worth="400"' in page and 'data-hint-button="0"' in page and 'data-hint-locked="1"' in page
    assert 'hx-confirm="Hint 1 costs 40 points on this challenge (400 → 360 pts). Show it?"' in page

    assert client.post("/c/d9/hint/2").status_code == 409  # out of order
    r = client.post("/c/d9/hint/0")
    assert 'data-hint="0"' in r.text and 'data-hint-button="1"' in r.text
    assert 'id="worth"' in r.text and 'data-worth="360"' in r.text and 'hx-swap-oob="outerHTML"' in r.text
    client.post("/c/d9/hint/1")
    client.post("/c/d9/hint/2")
    assert 'data-worth="160"' in client.get("/c/d9").text
    card = client.get("/board").text.split('data-challenge="d9"')[1].split("</a>")[0]
    assert 'data-card-worth="160"' in card and "<s" in card


def test_solve_awards_the_reduced_value_and_later_hints_are_free(client):
    name = new_team(client)
    ch = client.app.state.challenges["d8"]  # 300 pts
    client.post("/c/d8/hint/0")  # -30
    r = client.post("/c/d8/submit", data={"flag": ch.flag})
    assert "+270 pts" in r.text and "of 300, after hints" in r.text and "hints are free now" in r.text
    r = client.post("/c/d8/hint/1")
    assert 'data-worth="270"' in r.text and "earned" in r.text
    assert 'data-hint-cost="0"' in r.text  # the next button says free
    with Session(get_engine()) as s:
        assert s.exec(select(Solve).where(Solve.team_id == team_row(name).id)).one().points == 270
    assert 'data-score>270 pts' in client.get("/board").text


def test_no_hints_means_full_points(client):
    new_team(client)
    ch = client.app.state.challenges["d5"]
    r = client.post("/c/d5/submit", data={"flag": ch.flag})
    assert f"+{ch.points} pts" in r.text and "after hints" not in r.text


def test_hint_cost_model(challenges):
    d7 = challenges["d7"]  # 150 pts, 2 hints
    assert [d7.hint_cost(0), d7.hint_cost(1)] == [15, 30]
    assert [d7.value_after_hints(k) for k in range(4)] == [150, 135, 105, 105]  # only 2 hints exist


def test_htmx_request_without_session_gets_hx_redirect(client):
    r = client.post("/c/d5/chat", data={"message": "x"}, headers={"HX-Request": "true"})
    assert r.status_code == 401 and r.headers["HX-Redirect"] == "/"


def test_healthz(client):
    assert client.get("/healthz").json() == {"ok": True, "challenges": len(ACTIVE_IDS)}


# --- markdown renderer -------------------------------------------------------------


def test_md_escapes_then_formats():
    out = str(md("**bold** and `code` <script>x</script>\n\n- one\n- *two*"))
    assert "<strong>bold</strong>" in out and "<code" in out and "&lt;script&gt;" in out
    assert "<ul" in out and "<li>one</li>" in out and "<em>two</em>" in out
    assert "<script>" not in out


def test_md_pipe_table_is_escaped():
    out = str(md("Here:\n\n| Field | Value |\n|---|---|\n| **Note** | <b>x</b> |\n\nDone"))
    assert "<table" in out and "<th" in out and "<strong>Note</strong>" in out
    assert "&lt;b&gt;x&lt;/b&gt;" in out and "<b>" not in out
    assert out.count("<p") == 2  # "Here:" and "Done" stay paragraphs
    assert "<table" not in str(md("| just | pipes |"))  # no separator row -> not a table


def test_md_inline_has_no_block_tags():
    out = str(md_inline("Make it say `I have been PWNED` <now>"))
    assert "<p" not in out and "<code" in out and "&lt;now&gt;" in out
