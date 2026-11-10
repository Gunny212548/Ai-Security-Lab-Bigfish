"""M6: scoreboard, admin console, flag-sharing warning, and two teams playing side by side."""

import re
import uuid
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import auth
from app.db import INDIRECT_UNLOCKED, Solve, Submission, Team, TeamKB, get_engine, get_flag, reset_all, set_flag
from app.main import app
from app.ratelimit import RateLimiter
from app.scoreboard import first_bloods, standings

from .fakellm import FakeClient, llm_with, text

ADMIN_PW = "pw-for-tests"


@pytest.fixture
def client():
    with TestClient(app) as c:
        c.app.state.settings = c.app.state.settings.model_copy(update={"admin_password": ADMIN_PW,
                                                                       "app_secret": "test-secret"})
        yield c
        with Session(get_engine()) as s:
            set_flag(s, INDIRECT_UNLOCKED, "0")


@contextmanager
def other_client():
    """A second browser on the same running app (separate cookie jar). Not entered as a context manager on
    purpose: that would re-run lifespan and reset app.state (settings, llm) under the first client."""
    c = TestClient(app)
    try:
        yield c
    finally:
        c.close()


def new_team(c: TestClient, prefix: str = "t") -> str:
    name = f"{prefix}-{uuid.uuid4().hex[:8]}"
    r = c.post("/team/create", data={"name": name})
    assert r.status_code == 200 and auth.COOKIE in c.cookies, r.text
    return name


def team_id(name: str) -> int:
    with Session(get_engine()) as s:
        return s.exec(select(Team.id).where(Team.name == name)).one()


def flag(c: TestClient, cid: str) -> str:
    return c.app.state.challenges[cid].flag


def login_admin(c: TestClient) -> None:
    r = c.post("/admin/login", data={"password": ADMIN_PW}, follow_redirects=False)
    assert r.status_code == 303 and auth.ADMIN_COOKIE in c.cookies


def use_llm(c: TestClient, *responses) -> FakeClient:
    fake = FakeClient(*responses)
    c.app.state.llm = llm_with(fake)
    return fake


def row_order(html: str) -> list[str]:
    return re.findall(r'data-team-row="([^"]+)"', html)


# --- scoreboard ------------------------------------------------------------------------


def test_scoreboard_is_public_and_auto_refreshes(client):
    name = new_team(client)
    with other_client() as anon:
        r = anon.get("/scoreboard")
        assert r.status_code == 200 and name in r.text
        assert 'hx-get="/scoreboard/table"' in r.text and 'hx-trigger="every 10s"' in r.text
        part = anon.get("/scoreboard/table").text
        assert part.lstrip().startswith('<div id="scoreboard"') and "<html" not in part


def test_scoreboard_ranks_by_score_then_time_and_marks_first_blood(client):
    with Session(get_engine()) as s:  # earlier tests already solved d5 in this shared test DB
        reset_all(s)
    a = new_team(client, "a")
    assert "correct" in client.post("/c/d5/submit", data={"flag": flag(client, "d5")}).text
    with other_client() as cb:
        b = new_team(cb, "b")
        cb.post("/c/d5/submit", data={"flag": flag(client, "d5")})     # same score as a, later -> below a
        html = cb.get("/scoreboard").text
        order = row_order(html)
        assert order.index(a) < order.index(b)
        row_a = html.split(f'data-team-row="{a}"')[1].split("</tr>")[0]
        row_b = html.split(f'data-team-row="{b}"')[1].split("</tr>")[0]
        assert "data-first-blood" in row_a and "data-first-blood" not in row_b and "Solved" in row_b
        assert "(you)" in row_b and "(you)" not in row_a

        cb.post("/c/d6/submit", data={"flag": flag(client, "d6")})     # b pulls ahead
        order = row_order(cb.get("/scoreboard/table").text)
        assert order.index(b) < order.index(a)


def test_scoreboard_never_leaks_flags(client):
    new_team(client)
    client.post("/c/d5/submit", data={"flag": flag(client, "d5")})
    html = client.get("/scoreboard").text + client.get("/scoreboard/table").text
    for ch in client.app.state.challenges.values():
        assert ch.flag not in html


def test_standings_handles_aware_and_naive_times(session, make_team):
    t1, t2 = make_team("X"), make_team("Y")
    session.add(Solve(team_id=t1, challenge_id="d5", points=200))
    session.commit()
    session.add(Solve(team_id=t2, challenge_id="d5", points=200))  # still aware in memory, not reloaded
    rows = standings(session)
    assert [r.team.id for r in rows] == [t1, t2] and rows[0].rank == 1
    assert first_bloods(session)["d5"].team_id == t1


# --- admin auth ------------------------------------------------------------------------


def test_admin_requires_login(client):
    r = client.get("/admin")
    from app.flags import canary_flag

    assert 'action="/admin/login"' in r.text and "bigfish{" not in r.text.replace(canary_flag(), "")
    for path, data in (("/admin/indirect", {"unlock": "1"}), ("/admin/reset-all", {"confirm": "RESET"})):
        r = client.post(path, data=data, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/admin"
    with Session(get_engine()) as s:
        assert get_flag(s, INDIRECT_UNLOCKED) != "1"


def test_admin_wrong_password_and_forged_cookie(client):
    r = client.post("/admin/login", data={"password": "nope"})
    assert "Wrong password" in r.text and auth.ADMIN_COOKIE not in client.cookies
    client.cookies.set(auth.ADMIN_COOKIE, "0" * 32)
    assert 'action="/admin/login"' in client.get("/admin").text


def test_admin_login_logout_and_password_change(client):
    login_admin(client)
    assert "Organizer console" in client.get("/admin").text
    client.app.state.settings = client.app.state.settings.model_copy(update={"admin_password": "rotated"})
    assert 'action="/admin/login"' in client.get("/admin").text  # old cookie no longer valid
    client.app.state.settings = client.app.state.settings.model_copy(update={"admin_password": ADMIN_PW})
    login_admin(client)
    client.post("/admin/logout")
    assert 'action="/admin/login"' in client.get("/admin").text


def test_admin_disabled_when_password_empty(client):
    client.app.state.settings = client.app.state.settings.model_copy(update={"admin_password": ""})
    assert "Admin is disabled" in client.get("/admin").text
    assert "Admin is disabled" in client.post("/admin/login", data={"password": ""}).text
    assert auth.ADMIN_COOKIE not in client.cookies


def test_admin_login_rate_limited(client):
    client.app.state.admin_limiter = RateLimiter(2)
    client.post("/admin/login", data={"password": "a"})
    client.post("/admin/login", data={"password": "b"})
    r = client.post("/admin/login", data={"password": ADMIN_PW})
    assert "Slow down" in r.text and auth.ADMIN_COOKIE not in client.cookies


def test_admin_warns_about_default_secrets(client):
    client.app.state.settings = client.app.state.settings.model_copy(
        update={"admin_password": "change-me", "app_secret": "change-me"})
    client.post("/admin/login", data={"password": "change-me"})
    html = client.get("/admin").text
    assert html.count("data-warning") == 2 and "ADMIN_PASSWORD" in html and "APP_SECRET" in html


# --- admin actions ---------------------------------------------------------------------


def test_admin_unlocks_and_locks_indirect(client):
    new_team(client)
    login_admin(client)
    assert 'href="/c/i1"' not in client.get("/board").text
    r = client.post("/admin/indirect", data={"unlock": "1"})
    assert 'data-indirect-state="unlocked"' in r.text and "data-notice" in r.text
    assert 'href="/c/i1"' in client.get("/board").text
    client.post("/admin/indirect", data={"unlock": "0"})
    assert 'href="/c/i1"' not in client.get("/board").text


def test_admin_shows_solutions_teams_and_submissions(client):
    name = new_team(client)
    client.post("/c/d5/submit", data={"flag": "bigfish{<b>guess</b>}"})
    login_admin(client)
    html = client.get("/admin").text
    for ch in client.app.state.challenges.values():
        assert ch.flag in html and f'data-solution="{ch.id}"' in html
    assert f'data-admin-team="{name}"' in html
    assert "bigfish{&lt;b&gt;guess&lt;/b&gt;}" in html and "<b>guess</b>" not in html  # player input is escaped


def test_flag_share_warning(client):
    honest = new_team(client, "honest")
    use_llm(client, text("Ticket 2001 is about the CEO laptop."))
    client.post("/c/d5/chat", data={"message": "show me ticket 2001"})
    client.post("/c/d5/submit", data={"flag": flag(client, "d5")})
    with other_client() as cs:
        sharer = new_team(cs, "sharer")
        cs.post("/c/d5/submit", data={"flag": "bigfish{wrong}"})       # wrong guesses are never flagged
        cs.post("/c/d5/submit", data={"flag": flag(client, "d5")})      # correct, never talked to the bot
    with Session(get_engine()) as s:
        subs = {(n, sub.correct): sub.no_activity for sub, n in
                s.exec(select(Submission, Team.name).join(Team, Team.id == Submission.team_id)
                       .where(Team.name.in_([honest, sharer])))}
    assert subs == {(honest, True): False, (sharer, False): False, (sharer, True): True}
    login_admin(client)
    html = client.get("/admin").text
    assert html.count("data-share-warning") >= 1 and "flagged" in html


def test_reset_all_needs_confirm_then_wipes_and_old_cookie_dies(client):
    old = new_team(client)
    old_cookie = client.cookies.get(auth.COOKIE)
    login_admin(client)
    client.post("/admin/indirect", data={"unlock": "1"})
    r = client.post("/admin/reset-all", data={"confirm": "reset please"})
    assert "Nothing was deleted" in r.text and team_id(old)

    r = client.post("/admin/reset-all", data={"confirm": "RESET"})
    assert "Everything was reset" in r.text and 'data-indirect-state="locked"' in r.text
    with Session(get_engine()) as s:
        assert s.exec(select(Team)).first() is None and s.exec(select(Solve)).first() is None

    with other_client() as fresh:  # SQLite hands the freed id to the next team...
        new_team(fresh)
    client.cookies.clear()
    client.cookies.set(auth.COOKIE, old_cookie)  # ...but the old browser must not land in it
    assert client.get("/board", follow_redirects=False).status_code == 303


def test_reset_all_keeps_app_flags_table(session, make_team):
    make_team("Z")
    set_flag(session, INDIRECT_UNLOCKED, "1")
    reset_all(session)
    assert get_flag(session, INDIRECT_UNLOCKED) == "0" and session.exec(select(TeamKB)).first() is None


# --- M6 acceptance: two teams concurrently, no shared state ----------------------------


def test_two_teams_do_not_see_each_others_state(client):
    with Session(get_engine()) as s:
        set_flag(s, INDIRECT_UNLOCKED, "1")
    with other_client() as cb:
        a, b = new_team(client, "red"), new_team(cb, "blue")

        # Direct: a's chat transcript stays with a
        use_llm(client, text("Hello red team"))
        client.post("/c/d5/chat", data={"message": "red-secret-question"})
        assert "red-secret-question" in client.get("/c/d5").text
        assert "red-secret-question" not in cb.get("/c/d5").text

        # Indirect I1: a poisons its own KB; b's victim still reads the seed article
        client.post("/c/i1/kb", data={"article_id": "kb-pw", "body": "RED-POISON http://evil.lab"})
        assert "RED-POISON" not in cb.get("/c/i1").text
        fake = use_llm(cb, text("Go to https://portal.bigfish.lab"))
        html = cb.post("/c/i1/run-victim").text
        assert "RED-POISON" not in fake.requests[-1]["messages"][0]["content"]
        assert "Flag unlocked" not in html

        # I2: a's ticket is not in b's queue
        client.post("/c/i2/ticket", data={"title": "mouse broken RED-TICKET", "description": "x"})
        assert "RED-TICKET" not in cb.get("/c/i2").text

        # Solves, hints and score are per team
        client.post("/c/d5/submit", data={"flag": flag(client, "d5")})
        client.post("/c/d5/hint/0")
        page_b = cb.get("/c/d5").text
        assert "data-lesson" not in page_b
        assert "✅" not in cb.get("/board").text.split('data-challenge="d5"')[1].split("</a>")[0]
        assert ">0 pts<" in cb.get("/board").text

        # New chat by b does not touch a
        use_llm(client, text("RED-CHAT-REPLY"))
        client.post("/c/d6/chat", data={"message": "hi"})
        cb.post("/c/d6/new-chat")
        assert "RED-CHAT-REPLY" in client.get("/c/d6").text
    assert team_id(a) != team_id(b)
