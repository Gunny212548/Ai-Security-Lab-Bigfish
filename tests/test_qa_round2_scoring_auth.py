"""QA round 2: flags & scoring, teams & auth, admin, scoreboard, rate limiting, concurrency, activity log.

Focus: edge cases around the round-1 fixes (next_team_id / TEAM_ID_FLOOR, record_view on POST routes, fast-solve and
no-activity markers, repeated correct submissions) plus new checks in the same areas.

Tests marked xfail(strict=True) reproduce open bugs (R2-BUG-xx in the QA report). They turn into failures once the
bug is fixed, so whoever fixes it removes the marker.
"""

import io
import logging
import threading
import uuid
from datetime import timedelta

import pytest
from sqlmodel import Session, select

from app import activity, auth, flags, main
from app.db import (TEAM_ID_FLOOR, ChallengeView, Solve, Submission, Team, get_engine, get_flag, next_team_id,
                    reset_all, utcnow)
from app.main import app
from app.ratelimit import RateLimiter

from .fakellm import text
from .test_qa_scoring_auth import (ADMIN_PW, SECRET, base_flag, browser, client, login_admin, new_team,  # noqa: F401
                                   per_team, submissions, team_row, use_llm)


def views(tid: int, cid: str) -> list[ChallengeView]:
    with Session(get_engine()) as s:
        return list(s.exec(select(ChallengeView).where(ChallengeView.team_id == tid,
                                                       ChallengeView.challenge_id == cid)))


def activity_lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "bigfishdesk.activity"]


def fresh_db() -> None:
    with Session(get_engine()) as s:
        reset_all(s)


# --- record_view on POST routes (round-1 fix) -------------------------------------------------------------------


def test_r2_unknown_challenge_posts_are_404_and_store_nothing(client):
    name = new_team(client)
    tid = team_row(name).id
    for path, data in (("/c/zz/submit", {"flag": "x"}), ("/c/zz/chat", {"message": "hi"}), ("/c/zz/run-victim", None),
                       ("/c/zz/hint/0", None), ("/c/zz/new-chat", None), ("/c/D5/submit", {"flag": "x"})):
        r = client.post(path, **({"data": data} if data else {}))
        assert r.status_code == 404, path
    with Session(get_engine()) as s:
        assert s.exec(select(ChallengeView).where(ChallengeView.team_id == tid)).all() == []
        assert s.exec(select(Submission).where(Submission.team_id == tid)).all() == []


def test_r2_clock_starts_at_first_view_and_is_not_restarted(client):
    name = new_team(client)
    tid = team_row(name).id
    client.get("/c/i1")
    with Session(get_engine()) as s:                                                # pretend it was 10 min ago
        v = s.exec(select(ChallengeView).where(ChallengeView.team_id == tid, ChallengeView.challenge_id == "i1")).one()
        v.first_at = utcnow() - timedelta(minutes=10)
        s.add(v)
        s.commit()
    use_llm(client, text("victim reply"))
    client.post("/c/i1/run-victim")                                                 # must not restart the clock
    client.post("/c/i1/submit", data={"flag": base_flag("i1")})
    sub = submissions(tid, "i1")[-1]
    assert sub.correct and sub.solve_seconds >= 600 and len(views(tid, "i1")) == 1
    login_admin(client)
    row = client.get("/admin").text.split(f'data-submission="{sub.id}"')[1].split("</tr>")[0]
    assert "data-fast-solve" not in row and "10m" in row


def test_r2_rate_limited_and_empty_requests_do_not_store_activity(client):
    client.app.state.limiter = RateLimiter(1)
    name = new_team(client)
    tid = team_row(name).id
    use_llm(client, text("one"))
    client.post("/c/d6/chat", data={"message": "   "})                             # empty: nothing at all
    assert views(tid, "d6") == []
    client.post("/c/d6/chat", data={"message": "1"})
    r = client.post("/c/d6/chat", data={"message": "2"})
    assert "Slow down" in r.text
    assert len(views(tid, "d6")) == 1
    client.post("/c/d6/submit", data={"flag": base_flag("d6")})
    sub = submissions(tid, "d6")[-1]
    assert sub.correct and sub.attempts == 1 and not sub.no_activity               # the limited msg is not counted


# --- repeated correct submissions ---------------------------------------------------------------------------------


def test_r2_repeat_correct_submissions_never_add_points(client):
    name = new_team(client)
    tid = team_row(name).id
    client.post("/c/d7/hint/0")
    assert 'data-result="correct"' in client.post("/c/d7/submit", data={"flag": base_flag("d7")}).text
    client.post("/c/d7/hint/1")                                                     # after the solve: free
    client.post("/c/d7/hint/2")
    for _ in range(3):
        assert 'data-result="already"' in client.post("/c/d7/submit", data={"flag": base_flag("d7")}).text
    ch = app.state.challenges["d7"]
    with Session(get_engine()) as s:
        solves = s.exec(select(Solve).where(Solve.team_id == tid)).all()
    assert [(x.challenge_id, x.points) for x in solves] == [("d7", ch.value_after_hints(1))]
    board = client.get("/board").text
    assert f">{ch.value_after_hints(1)} pts<" in board


def test_r2_admin_counters_count_each_solve_once(client):
    fresh_db()
    new_team(client)
    for _ in range(3):                                   # teammates paste the same flag (no chat: ⚠, instant: ⚡)
        client.post("/c/d5/submit", data={"flag": base_flag("d5")})
    login_admin(client)
    html = client.get("/admin").text
    assert "1 fast." in html and "1 flagged." in html


# --- submissions table / anti-cheat signals -----------------------------------------------------------------------


def test_r2_anti_cheat_signals_survive_a_flood_of_wrong_submissions(client, per_team):
    fresh_db()
    name = new_team(client, "cheater")
    tid = team_row(name).id
    client.post("/c/d5/submit", data={"flag": base_flag("d5")})                       # 📄 write-up flag
    client.post("/c/d6/submit", data={"flag": flags.canary_flag()})                   # 🤖 canary
    client.post("/c/d7/submit", data={"flag": flags.flag_for(app.state.challenges["d7"], tid)})  # ⚠ + ⚡
    login_admin(client)
    before = client.get("/admin").text
    assert all(m in before for m in ('data-leak="base"', "data-canary", "data-share-warning", "data-leak-count"))
    for i in range(200):                                                              # no "Slow down" ever
        r = client.post("/c/d9/submit", data={"flag": f"bigfish{{junk_{i}}}"})
        assert 'data-result="wrong"' in r.text
    assert len(submissions(tid, "d9")) == 200
    after = client.get("/admin").text
    for marker in ('data-leak="base"', "data-canary", "data-share-warning", "data-leak-count", "data-canary-count",
                   "data-flagged-count"):
        assert marker in after, marker


def test_r2_submission_values_are_escaped_on_admin(client):
    new_team(client)
    payload = '"><img src=x onerror=alert(1)><script>alert(2)</script>'
    client.post("/c/d5/submit", data={"flag": payload})
    login_admin(client)
    html = client.get("/admin").text
    assert "<img src=x" not in html and "<script>alert(2)" not in html
    assert "&lt;img src=x onerror=alert(1)&gt;" in html


def test_r2_flag_of_a_team_from_before_reset_is_plain_wrong_and_not_a_leak(client, per_team):
    fresh_db()
    old = new_team(client, "am")
    old_flag = flags.flag_for(app.state.challenges["d5"], team_row(old).id)
    fresh_db()
    with browser() as b:
        new = new_team(b, "pm")
        assert 'data-result="wrong"' in b.post("/c/d5/submit", data={"flag": old_flag}).text
        assert submissions(team_row(new).id, "d5")[-1].signal == ""


# --- team ids across Reset all (round-1 fix) ----------------------------------------------------------------------


def test_r2_ids_after_reset_count_up_and_old_cookies_and_codes_die(client):
    fresh_db()
    a = new_team(client, "am")
    old = team_row(a)
    old_cookie = client.cookies.get(auth.COOKIE)
    fresh_db()
    fresh_db()                                                        # twice in a row keeps the floor
    with Session(get_engine()) as s:
        assert int(get_flag(s, TEAM_ID_FLOOR)) == old.id
        assert next_team_id(s) == old.id + 1
    ids = []
    with browser() as b:
        for _ in range(3):
            b.cookies.clear()
            ids.append(team_row(new_team(b, "pm")).id)
    assert ids == [old.id + 1, old.id + 2, old.id + 3]
    with browser() as c:
        c.cookies.set(auth.COOKIE, old_cookie)
        assert c.get("/board", follow_redirects=False).status_code == 303
        r = c.post("/team/join", data={"code": old.join_code})
        assert "No team has that join code." in r.text


def test_r2_two_different_teams_created_at_once_right_after_reset(client, monkeypatch):
    """Both get next_team_id() == floor + 1; the loser of the PK clash must retry, not show "name taken"."""
    fresh_db()
    new_team(client)
    fresh_db()
    with Session(get_engine()) as s:
        floor = int(get_flag(s, TEAM_ID_FLOOR))
    orig = main.next_team_id
    barrier = threading.Barrier(2)

    def synced(session):
        out = orig(session)
        try:
            barrier.wait(2)
        except threading.BrokenBarrierError:
            pass
        return out

    monkeypatch.setattr(main, "next_team_id", synced)
    names = [f"race-{uuid.uuid4().hex[:6]}", f"race-{uuid.uuid4().hex[:6]}"]
    results: dict = {}

    def go(n):
        with browser(raise_errors=False) as c:
            r = c.post("/team/create", data={"name": n})
            results[n] = (r.status_code, "taken" in r.text, auth.COOKIE in c.cookies)

    threads = [threading.Thread(target=go, args=(n,)) for n in names]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert all(v == (200, False, True) for v in results.values()), results
    ids = sorted(team_row(n).id for n in names)
    assert ids[0] == floor + 1 and ids[1] > ids[0]


def test_r2_team_name_taken_after_reset_is_free_again(client):
    fresh_db()
    name = f"reuse-{uuid.uuid4().hex[:6]}"
    new_team(client, name=name)
    fresh_db()
    with browser() as b:
        new_team(b, name=name)


# --- scoreboard / admin -------------------------------------------------------------------------------------------


def test_r2_scoreboard_and_admin_after_reset_are_empty_and_render(client):
    new_team(client)
    client.post("/c/d5/submit", data={"flag": base_flag("d5")})
    fresh_db()
    with browser() as anon:
        assert "data-empty" in anon.get("/scoreboard/table").text
    login_admin(client)
    html = client.get("/admin").text
    assert "No teams yet." in html and "No submissions yet." in html


def test_r2_admin_reset_all_logs_out_teams(client):
    name = new_team(client)
    with browser() as admin:
        login_admin(admin)
        r = admin.post("/admin/reset-all", data={"confirm": "  RESET  "}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/admin?msg=reset"
        assert "Everything was reset" in admin.get("/admin?msg=reset").text
    assert client.get("/board", follow_redirects=False).status_code == 303
    r = client.post("/c/d5/submit", data={"flag": base_flag("d5")}, headers={"HX-Request": "true"})
    assert r.status_code == 401
    with Session(get_engine()) as s:
        assert s.exec(select(Team).where(Team.name == name)).first() is None


def test_r2_long_admin_password_can_log_in(client):
    pw = "p" * 250
    client.app.state.settings = client.app.state.settings.model_copy(update={"admin_password": pw})
    r = client.post("/admin/login", data={"password": pw}, follow_redirects=False)
    assert r.status_code == 303 and auth.ADMIN_COOKIE in client.cookies


def test_r2_admin_login_limit_is_shared_by_everyone_behind_one_ip(client):
    """Needs triage: the admin-login limit is per client IP. Behind Docker Desktop / a reverse proxy every player has
    the same IP (README's log example: 172.18.0.1), so one player typing 10 wrong passwords locks the organizer out
    of /admin for a minute, again and again."""
    client.app.state.admin_limiter = RateLimiter(main.ADMIN_LOGIN_PER_MIN)
    with browser() as player:
        for _ in range(main.ADMIN_LOGIN_PER_MIN):
            player.post("/admin/login", data={"password": "guess"})
    with browser() as organizer:
        r = organizer.post("/admin/login", data={"password": ADMIN_PW}, follow_redirects=False)
        assert r.status_code == 200 and "Slow down" in r.text and auth.ADMIN_COOKIE not in organizer.cookies


def test_r2_admin_warnings_for_default_secrets(client):
    login_admin(client)
    assert "data-warning" not in client.get("/admin").text
    client.app.state.settings = client.app.state.settings.model_copy(update={"app_secret": auth.DEFAULT_SECRET})
    client.cookies.clear()
    login_admin(client)
    assert "APP_SECRET is still the default" in client.get("/admin").text


# --- activity log -------------------------------------------------------------------------------------------------


def test_r2_activity_log_never_contains_the_admin_password(client, caplog):
    caplog.set_level(logging.INFO, logger="bigfishdesk.activity")
    client.post("/admin/login", data={"password": "almost-" + ADMIN_PW})
    client.post("/admin/login", data={"password": ADMIN_PW})
    client.post("/admin/reset-all", data={"confirm": "nope"})
    out = "\n".join(activity_lines(caplog))
    assert ADMIN_PW not in out and SECRET not in out
    assert [line.split(" | ")[3] for line in activity_lines(caplog)] == [
        "admin_login_failed", "admin_login", "admin_reset_all_cancelled"]


def test_r2_activity_log_odd_input_stays_one_line(client, caplog):
    caplog.set_level(logging.INFO, logger="bigfishdesk.activity")
    new_team(client, name="ทีมแดง 🐟")
    use_llm(client, text("ok\r\nline2 x"))
    odd = "สวัสดี 👋\x00\x1b[31mRED\r\nnext line퟿"
    client.post("/c/d5/chat", data={"message": odd})
    client.post("/c/d5/submit", data={"flag": odd})
    client.post("/team/join", data={"code": "\x1b[2J"})
    lines = activity_lines(caplog)
    assert len(lines) == 4
    for line in lines:
        assert "\n" not in line and "\r" not in line and "\x1b" not in line and "\x00" not in line
    assert "ทีมแดง 🐟#" in lines[1] and "สวัสดี 👋" in lines[1]


def test_r2_console_handler_survives_a_non_utf8_console():
    buf = io.BytesIO()
    stream = io.TextIOWrapper(buf, encoding="cp1252", errors="strict")
    h = activity._ConsoleHandler(stream)
    h.setFormatter(logging.Formatter("%(message)s"))
    rec = logging.LogRecord("bigfishdesk.activity", logging.INFO, __file__, 1, "ทีม 🐟 \udc80", None, None)
    h.emit(rec)
    stream.flush()
    assert b"\\u0e17" in buf.getvalue() and b"\\U0001f41f" in buf.getvalue()


def test_r2_team_name_cannot_forge_activity_log_columns(client, caplog):
    caplog.set_level(logging.INFO, logger="bigfishdesk.activity")
    evil = f"x{uuid.uuid4().hex[:4]} | Blue Team#2 | d5 | flag\x1b[2J"
    new_team(client, name=evil)
    client.post("/c/d5/submit", data={"flag": "bigfish{nope}"})
    lines = activity_lines(caplog)
    for line in lines:
        assert "\x1b" not in line
    assert [line.split(" | ")[3] for line in lines] == ["team_create", "flag"]


# --- concurrency --------------------------------------------------------------------------------------------------


def test_r2_concurrent_wrong_and_correct_submissions_from_teammates(client):
    name = new_team(client)
    cookie = client.cookies.get(auth.COOKIE)
    tid = team_row(name).id
    values = [base_flag("d6") if i % 3 == 0 else f"wrong-{i}" for i in range(12)]
    codes: list = []
    barrier = threading.Barrier(len(values))

    def go(v):
        with browser(raise_errors=False) as c:
            c.cookies.set(auth.COOKIE, cookie)
            barrier.wait(5)
            codes.append(c.post("/c/d6/submit", data={"flag": v}).status_code)

    threads = [threading.Thread(target=go, args=(v,)) for v in values]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert codes == [200] * len(values)
    with Session(get_engine()) as s:
        assert len(s.exec(select(Solve).where(Solve.team_id == tid, Solve.challenge_id == "d6")).all()) == 1
    assert len(submissions(tid, "d6")) == len(values)


def test_r2_concurrent_first_views_record_one_row(client):
    name = new_team(client)
    cookie = client.cookies.get(auth.COOKIE)
    tid = team_row(name).id
    codes: list = []
    barrier = threading.Barrier(6)

    def go():
        with browser(raise_errors=False) as c:
            c.cookies.set(auth.COOKIE, cookie)
            barrier.wait(5)
            codes.append(c.get("/c/d9").status_code)

    threads = [threading.Thread(target=go) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert codes == [200] * 6 and len(views(tid, "d9")) == 1


def test_r2_rate_limit_window_slides(client, monkeypatch):
    now = [1000.0]
    client.app.state.limiter = RateLimiter(2, clock=lambda: now[0])
    new_team(client)
    use_llm(client, text("a"), text("b"), text("BOTREPLYC"))
    client.post("/c/d5/chat", data={"message": "1"})
    now[0] += 30
    client.post("/c/d5/chat", data={"message": "2"})
    r = client.post("/c/d5/chat", data={"message": "3"})
    assert "Slow down, try again in 30 seconds" in r.text
    now[0] += 30                                                     # first hit is now 60 s old
    r = client.post("/c/d5/chat", data={"message": "4"})
    assert "Slow down" not in r.text and "BOTREPLYC" in r.text


def test_r2_activity_log_lines_are_bounded(client, caplog):
    caplog.set_level(logging.INFO, logger="bigfishdesk.activity")
    big = "A" * 900_000
    with browser() as anon:                                          # no team, no cookie needed
        anon.post("/team/create", data={"name": big})
        anon.post("/team/join", data={"code": big})
    new_team(client)
    client.post("/c/i1/kb", data={"article_id": "KB-1", "body": big})
    lines = activity_lines(caplog)
    assert len(lines) == 4
    assert max(len(line) for line in lines) < 20_000, [len(line) for line in lines]


def test_r2_second_request_while_one_is_in_flight_is_refused(client):
    """One event loop (as in production): a teammate's request while the AI is still answering gets "wait"."""
    import asyncio

    import httpx

    from .fakellm import FakeClient, llm_with

    new_team(client)
    cookie = client.cookies.get(auth.COOKIE)
    fake = FakeClient(text("slow one"), text("d6 reply"), delay=0.5)
    client.app.state.llm = llm_with(fake)

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t", cookies={auth.COOKIE: cookie}) as c:
            first = asyncio.create_task(c.post("/c/d5/chat", data={"message": "1"}))
            await asyncio.sleep(0.2)
            busy = await c.post("/c/d5/chat", data={"message": "2"})
            other = await c.post("/c/d6/chat", data={"message": "3"})  # another challenge is not blocked
            return (await first).text, busy.text, other.text

    first, busy, other = asyncio.run(run())
    assert "slow one" in first and "wait for the current reply" in busy and "d6 reply" in other
    assert app.state.limiter.in_flight == 0


