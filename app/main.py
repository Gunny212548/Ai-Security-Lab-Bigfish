"""FastAPI app: join/team flow, board, Direct (M4) + Indirect (M5) challenge pages, scoreboard + admin (M6),
friendly error pages + queue indicator (M7)."""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.exception_handlers import http_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import activity, auth, engine, i18n, scoreboard, state
from app.challenges import OWASP_LLM_2025, PLAYER_TICKET_ID, Challenge, load_challenges
from app.db import (INDIRECT_UNLOCKED, HintView, Solve, Submission, Team, VictimRun, get_flag, get_session,
                    get_engine, init_db, reset_all, set_flag)
from app.filters import BLOCKED_INPUT_MESSAGE
from app.flags import canary_flag, flag_for
from app.llm import run_llm
from app.pdfcheck import MAX_BYTES, PdfRejected, check_pdf
from app.ratelimit import BUSY_IN_FLIGHT_MESSAGE, RateLimiter, slow_down_message
from app.render import md, md_inline, parse_json, pretty_json
from app.settings import get_settings, load_providers
from app.i18n import t
from app.xray import MASK

log = logging.getLogger("bigfishdesk")

TEMPLATES = Jinja2Templates(directory=Path(__file__).parent / "templates")
TEMPLATES.env.filters["md"] = md
TEMPLATES.env.filters["md_inline"] = md_inline
TEMPLATES.env.filters["pretty_json"] = pretty_json
TEMPLATES.env.filters["parse_json"] = parse_json
TEMPLATES.env.globals.update(_=i18n.gettext_markup, t=i18n.t, loc=i18n.loc, get_lang=i18n.get_lang, LANGS=i18n.LANGS)


def iso_utc(dt: datetime | None) -> str:
    """ISO-8601 with Z for <time datetime=...>; base.html converts it to the viewer's local time."""
    if dt is None:
        return ""
    dt = dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


TEMPLATES.env.filters["iso"] = iso_utc

TRACKS = (
    ("direct", "🎯 Direct Prompt Injection", "Talk to BigfishDesk directly and trick it into doing what it shouldn't."),
    ("indirect", "🕵️ Indirect Prompt Injection", "Hide instructions in data the AI reads, then wait for an employee to use it."),
)
MAX_CHAT_CHARS = 2000
MAX_FLAG_CHARS = 200
MAX_KB_BODY = 2000
MAX_TICKET_TITLE = 150
MAX_TICKET_DESC = 2000
MAX_FILENAME = 120
MAX_PLUGIN_DESC = 1000
MAX_ADMIN_PASSWORD = 200
ADMIN_LOGIN_PER_MIN = 10
RESET_ALL_CONFIRM = "RESET"
SCOREBOARD_REFRESH_S = 10


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    activity.setup(settings)
    providers = load_providers(settings.providers_file, settings.default_provider)
    challenges = load_challenges(settings.challenges_dir, set(providers))  # fails fast on bad config
    init_db(settings.database_url)
    with Session(get_engine()) as session:  # challenges added since a team was created get their data now
        n = state.seed_missing(session, challenges.values())
        if n:
            log.info("seeded %d challenge(s) for existing teams", n)
    app.state.settings = settings
    TEMPLATES.env.globals["canary_flag"] = canary_flag(settings)  # hidden in base.html for AI agents
    app.state.providers = providers
    app.state.challenges = challenges
    app.state.llm = run_llm  # tests swap in a fake
    app.state.limiter = RateLimiter(settings.rate_limit_per_min)
    app.state.admin_limiter = RateLimiter(ADMIN_LOGIN_PER_MIN)  # per client IP, against password guessing
    yield


app = FastAPI(title="Bigfish AI Security Training", lifespan=lifespan)
app.add_middleware(i18n.LanguageMiddleware)


# --- errors (M7): friendly pages for browsers, short text for HTMX (base.html shows it as a toast) -----

ERROR_TEXT = {
    400: "That request didn't make sense to the server.",
    401: "Your session ended. Join your team again.",
    404: "That page doesn't exist.",
    405: "That action isn't allowed here.",
    413: "That upload is too large.",
    500: "Something broke on our side. Try again; if it keeps happening, tell an organizer.",
}


def error_response(request: Request, status: int, detail: str | None = None):
    message = t(ERROR_TEXT.get(status, ERROR_TEXT[500] if status >= 500 else ERROR_TEXT[400]))
    if detail and status < 500 and detail not in ("Not Found", "Method Not Allowed", "Bad Request"):
        message = f"{message} ({detail})"
    if request.headers.get("HX-Request"):
        return PlainTextResponse(message, status_code=status)
    return TEMPLATES.TemplateResponse(request, "error.html", {"team": None, "score": 0, "status": status,
                                                              "message": message}, status_code=status)


@app.exception_handler(StarletteHTTPException)
async def on_http_error(request: Request, exc: StarletteHTTPException):
    if exc.status_code < 400 or (exc.headers or {}).get("HX-Redirect"):
        return await http_exception_handler(request, exc)  # our redirects (303 + Location, HX-Redirect)
    return error_response(request, exc.status_code, exc.detail if isinstance(exc.detail, str) else None)


@app.exception_handler(RequestValidationError)
async def on_validation_error(request: Request, exc: RequestValidationError):
    return error_response(request, 400)


@app.exception_handler(Exception)
async def on_crash(request: Request, exc: Exception):
    log.exception("unhandled error on %s %s", request.method, request.url.path)
    return error_response(request, 500)


# --- helpers ------------------------------------------------------------------------------


def public_card(ch: Challenge) -> dict:
    """Only player-safe fields — never flag, solution, system_prompt or secrets (SPEC §12)."""
    return {
        "id": ch.id,
        "track": ch.track,
        "title": i18n.loc(ch, "title"),
        "points": ch.points,
        "difficulty": ch.difficulty,
        "objective": i18n.loc(ch, "objective"),
        "owasp": ch.owasp_labels,
    }


def team_dep(request: Request, session: Session = Depends(get_session)) -> Team:
    return auth.require_team(request, session)


def indirect_unlocked(session: Session) -> bool:
    return get_flag(session, INDIRECT_UNLOCKED) == "1"


def get_challenge(request: Request, session: Session, cid: str) -> Challenge:
    ch = request.app.state.challenges.get(cid)
    if ch is None:
        raise HTTPException(status_code=404, detail="challenge not found")
    if ch.track == "indirect" and not indirect_unlocked(session):
        raise HTTPException(status_code=303, headers={"Location": "/board"})
    return ch


def solved_ids(session: Session, team_id: int) -> set[str]:
    return set(session.exec(select(Solve.challenge_id).where(Solve.team_id == team_id)))


def team_score(session: Session, team_id: int) -> int:
    return sum(session.exec(select(Solve.points).where(Solve.team_id == team_id)))


def revealed_hints(session: Session, team_id: int, cid: str) -> set[int]:
    return set(session.exec(select(HintView.hint_index).where(HintView.team_id == team_id,
                                                              HintView.challenge_id == cid)))


def solve_of(session: Session, team_id: int, cid: str) -> Solve | None:
    return session.exec(select(Solve).where(Solve.team_id == team_id, Solve.challenge_id == cid)).first()


def hint_ctx(session: Session, team_id: int, ch: Challenge) -> dict:
    """Hints panel + the challenge's current value (hints cost points, see HINT_COST_PERCENTS)."""
    shown = revealed_hints(session, team_id, ch.id)
    solve = solve_of(session, team_id, ch.id)
    return {"hints_shown": shown, "solved": solve is not None,
            "awarded": solve.points if solve else None,
            "worth": solve.points if solve else ch.value_after_hints(len(shown))}


def last_victim_run(session: Session, team_id: int, cid: str) -> VictimRun | None:
    return session.exec(select(VictimRun).where(VictimRun.team_id == team_id, VictimRun.challenge_id == cid)
                        .order_by(VictimRun.id.desc())).first()


def surface_ctx(session: Session, team_id: int, ch: Challenge, notice: str | None = None,
                error: str | None = None) -> dict:
    """Everything the attacker panel of an Indirect challenge shows."""
    ctx: dict = {"ch": ch, "notice": notice, "error": error}
    if ch.attacker_surface == "kb_edit":
        ctx["kb"] = state.get_kb(session, team_id, ch.id)
    elif ch.attacker_surface == "ticket_form":
        tickets = state.get_tickets(session, team_id, ch.id)
        ctx["queue"] = [t for t in tickets if t.ticket_id != PLAYER_TICKET_ID]
        ctx["player_ticket"] = next((t for t in tickets if t.ticket_id == PLAYER_TICKET_ID), None)
    elif ch.attacker_surface == "plugin_edit":
        ctx["plugins"] = state.get_plugins(session, team_id, ch)
        ctx["max_desc"] = MAX_PLUGIN_DESC
    elif ch.attacker_surface == "pdf_upload":
        ctx["upload"] = state.latest_upload(session, team_id, ch.id)
        ctx["max_mb"] = MAX_BYTES // (1024 * 1024)
    return ctx


def victim_view(ch: Challenge, out: engine.TurnResult) -> dict:
    if out.error == "no_upload":
        status = "no_upload"
    elif out.skipped_llm:
        status = "rejected"  # I3: IT Staff caught visible suspicious text
    elif out.error:
        status = "error"
    else:
        status = "ok"
    return {"reply": out.reply, "won": out.won, "flag": out.flag, "status": status}


def saved_victim_view(ch: Challenge, run: VictimRun | None, team_id: int) -> dict | None:
    if run is None:
        return None
    return {"reply": run.output, "won": run.won,
            "flag": flag_for(ch, team_id) if run.won and ch.flag_mode == "on_impact" else None,
            "status": "rejected" if run.output == engine.HUMAN_REJECTED_MESSAGE else "ok"}


def require_surface(ch: Challenge, surface: str) -> None:
    if ch.attacker_surface != surface:
        raise HTTPException(status_code=400, detail=f"{ch.id} has no {surface} surface")


def limited(request: Request, team: Team, ch: Challenge) -> str | None:
    """Rate-limit notice for this team+challenge, or None if the request may proceed."""
    limiter: RateLimiter = request.app.state.limiter
    key = (team.id, ch.id)
    if limiter.lock(key).locked():
        return BUSY_IN_FLIGHT_MESSAGE
    wait = limiter.hit(key)
    return slow_down_message(wait) if wait is not None else None


def page(request: Request, name: str, ctx: dict, team: Team | None = None, session: Session | None = None):
    base = {"team": team, "score": team_score(session, team.id) if team and session else 0}
    return TEMPLATES.TemplateResponse(request, name, {**base, **ctx})


def partial(request: Request, name: str, ctx: dict) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(request, name, ctx)


def login_response(team: Team, request: Request) -> RedirectResponse:
    resp = RedirectResponse("/board", status_code=303)
    resp.set_cookie(auth.COOKIE, auth.sign(team.id, team.join_code, request.app.state.settings.app_secret),
                    max_age=auth.COOKIE_MAX_AGE, httponly=True, samesite="lax")
    return resp


# --- join / team ------------------------------------------------------------------------


@app.get("/")
def index(request: Request, session: Session = Depends(get_session)):
    if auth.current_team(request, session):
        return RedirectResponse("/board", status_code=303)
    return page(request, "join.html", {"error": None, "name": "", "code": ""})


@app.post("/team/create")
def create_team(request: Request, name: str = Form(""), session: Session = Depends(get_session)):
    name = " ".join(name.split())
    if not name or len(name) > auth.MAX_TEAM_NAME:
        activity.record(request, "team_create_failed", name=name, reason="bad name")
        return page(request, "join.html", {"error": t("Team name must be 1–{n} characters.", n=auth.MAX_TEAM_NAME),
                                           "name": name, "code": ""})
    team = Team(name=name, join_code=auth.new_join_code(session))
    session.add(team)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        activity.record(request, "team_create_failed", name=name, reason="name taken")
        return page(request, "join.html", {"error": t("That team name is taken. Ask your teammates for the join code."),
                                           "name": name, "code": ""})
    state.seed_team(session, team.id, request.app.state.challenges.values())
    activity.record(request, "team_create", team, join_code=team.join_code)
    return login_response(team, request)


@app.post("/team/join")
def join_team(request: Request, code: str = Form(""), session: Session = Depends(get_session)):
    code = code.strip().upper()
    team = session.exec(select(Team).where(Team.join_code == code)).first() if code else None
    if team is None:
        activity.record(request, "team_join_failed", code=code)
        return page(request, "join.html", {"error": t("No team has that join code."), "name": "", "code": code})
    activity.record(request, "team_join", team)
    return login_response(team, request)


@app.get("/lang/{code}")
def set_language(code: str, next: str = "/"):
    """Language switch (header). Only the UI changes; the chat is never translated (app/i18n.py)."""
    target = next if next.startswith("/") and not next.startswith("//") else "/"
    resp = RedirectResponse(target, status_code=303)
    resp.set_cookie(i18n.COOKIE, i18n.normalize(code), max_age=i18n.COOKIE_MAX_AGE, samesite="lax")
    return resp


@app.post("/team/leave")
def leave_team(request: Request, session: Session = Depends(get_session)):
    activity.record(request, "team_leave", auth.current_team(request, session))
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie(auth.COOKIE)
    return resp


# --- board --------------------------------------------------------------------------------


@app.get("/board")
def board(request: Request, team: Team = Depends(team_dep), session: Session = Depends(get_session)):
    challenges: dict[str, Challenge] = request.app.state.challenges
    unlocked = indirect_unlocked(session)
    tracks = [
        {
            "key": key,
            "name": t(name),
            "desc": t(desc),
            "locked": key == "indirect" and not unlocked,
            "cards": [public_card(c) for c in challenges.values() if c.track == key],
        }
        for key, name, desc in TRACKS
    ]
    opened: dict[str, int] = {}
    for cid in session.exec(select(HintView.challenge_id).where(HintView.team_id == team.id)):
        opened[cid] = opened.get(cid, 0) + 1
    awarded = {s.challenge_id: s.points for s in session.exec(select(Solve).where(Solve.team_id == team.id))}
    worth = {cid: awarded.get(cid, c.value_after_hints(opened.get(cid, 0))) for cid, c in challenges.items()}
    return page(request, "board.html", {"tracks": tracks, "solved": set(awarded), "worth": worth,
                                        "total": len(challenges)}, team, session)


# --- challenge page ------------------------------------------------------------------


@app.get("/c/{cid}")
def challenge_page(cid: str, request: Request, team: Team = Depends(team_dep),
                   session: Session = Depends(get_session)):
    ch = get_challenge(request, session, cid)
    scoreboard.record_view(session, team.id, ch.id)
    activity.record(request, "open", team, ch.id)
    if ch.track != "direct":
        return page(request, "indirect.html", {
            **hint_ctx(session, team.id, ch),
            "victim": saved_victim_view(ch, last_victim_run(session, team.id, ch.id), team.id),
            **surface_ctx(session, team.id, ch),
        }, team, session)
    return page(request, "direct.html", {
        "ch": ch,
        "card": public_card(ch),
        **hint_ctx(session, team.id, ch),
        "transcript": state.get_transcript(session, team.id, ch.id),
        "blocked_message": BLOCKED_INPUT_MESSAGE,
        "max_chat": MAX_CHAT_CHARS,
    }, team, session)


@app.post("/c/{cid}/chat")
async def chat(cid: str, request: Request, message: str = Form(""), team: Team = Depends(team_dep),
               session: Session = Depends(get_session)):
    ch = get_challenge(request, session, cid)
    if ch.track != "direct":
        raise HTTPException(status_code=400, detail="not a chat challenge")
    text = message.strip()[:MAX_CHAT_CHARS]
    if not text:
        return HTMLResponse("")

    notice = limited(request, team, ch)
    if notice:
        activity.record(request, "chat_limited", team, ch.id, msg=text, notice=notice)
        return partial(request, "_chat_notice.html", {"ch": ch, "user_text": text, "notice": notice})

    limiter: RateLimiter = request.app.state.limiter
    async with limiter.lock((team.id, ch.id)):
        limiter.in_flight += 1
        try:
            out = await engine.chat_turn(session, team.id, ch, text, llm=request.app.state.llm,
                                         settings=request.app.state.settings)
        finally:
            limiter.in_flight -= 1
    activity.record(request, "chat", team, ch.id, msg=text, reply=out.reply, won=out.won, blocked=out.blocked or None,
                    redacted=out.redacted or None, error=out.error, tools=activity.tools(out.tool_trace) or None,
                    model=out.model or None, latency_s=round(out.latency_s, 2))
    return partial(request, "_chat_turn.html", {"ch": ch, "user_text": text, "out": out, "mask": MASK})


@app.post("/c/{cid}/submit")
def submit_flag(cid: str, request: Request, flag: str = Form(""), team: Team = Depends(team_dep),
                session: Session = Depends(get_session)):
    ch = get_challenge(request, session, cid)
    value = flag.strip()[:MAX_FLAG_CHARS]
    if not value:
        activity.record(request, "flag_empty", team, ch.id)
        return partial(request, "_submit_result.html", {"ch": ch, "result": "empty"})
    correct = value == flag_for(ch, team.id)
    signal = "" if correct else flag_signal(session, ch, value, team.id)
    no_activity = correct and not scoreboard.has_activity(session, team.id, ch.id)
    seconds, attempts = scoreboard.solve_stats(session, team.id, ch.id) if correct else (None, 0)
    session.add(Submission(team_id=team.id, challenge_id=ch.id, value=value, correct=correct,
                           no_activity=no_activity, solve_seconds=seconds, attempts=attempts, signal=signal))
    result, awarded = ("canary" if signal == "canary" else "wrong"), None
    if correct:
        if solve_of(session, team.id, ch.id):
            result = "already"
        else:
            awarded = ch.value_after_hints(len(revealed_hints(session, team.id, ch.id)))
            session.add(Solve(team_id=team.id, challenge_id=ch.id, points=awarded))
            result = "correct"
    session.commit()
    activity.record(request, "flag", team, ch.id, value=value, result=result, points=awarded, signal=signal or None,
                    no_activity=no_activity or None, solve_s=seconds)
    return partial(request, "_submit_result.html", {"ch": ch, "result": result, "awarded": awarded,
                                                    **hint_ctx(session, team.id, ch),
                                                    "score": team_score(session, team.id)})


@app.post("/c/{cid}/hint/{n}")
def reveal_hint(cid: str, n: int, request: Request, team: Team = Depends(team_dep),
                session: Session = Depends(get_session)):
    ch = get_challenge(request, session, cid)
    if not 0 <= n < len(ch.hints):
        raise HTTPException(status_code=404, detail="no such hint")
    shown = revealed_hints(session, team.id, ch.id)
    if n > 0 and n - 1 not in shown:
        raise HTTPException(status_code=409, detail=t("Open hint {n} first", n=n))
    if n not in shown:
        session.add(HintView(team_id=team.id, challenge_id=ch.id, hint_index=n))
        try:
            session.commit()
        except IntegrityError:  # double click from two teammates
            session.rollback()
        activity.record(request, "hint", team, ch.id, n=n + 1)
    return partial(request, "_hints.html", {"ch": ch, "oob": True, **hint_ctx(session, team.id, ch)})


@app.post("/c/{cid}/new-chat")
def new_chat(cid: str, request: Request, team: Team = Depends(team_dep), session: Session = Depends(get_session)):
    """The only reset players get: a fresh chatbot conversation (Direct track). Solves, hints and the
    challenge's data are kept; the old turns are archived, not deleted (admin activity checks still see them)."""
    ch = get_challenge(request, session, cid)
    if ch.track != "direct":
        raise HTTPException(status_code=400, detail="only chat challenges have a chat to restart")
    state.new_chat(session, team.id, ch.id)
    activity.record(request, "new_chat", team, ch.id)
    return RedirectResponse(f"/c/{ch.id}", status_code=303)


# --- indirect: attacker surfaces + victim run (M5) ----------------------------------


@app.post("/c/{cid}/kb")
def edit_kb(cid: str, request: Request, article_id: str = Form(""), body: str = Form(""),
            team: Team = Depends(team_dep), session: Session = Depends(get_session)):
    ch = get_challenge(request, session, cid)
    require_surface(ch, "kb_edit")
    activity.record(request, "kb_edit", team, ch.id, article=article_id, body=body)
    if len(body) > MAX_KB_BODY:
        return partial(request, "_attacker_kb.html",
                       surface_ctx(session, team.id, ch, error=t("Article is longer than {n} characters", n=MAX_KB_BODY)))
    try:
        row = state.update_kb(session, team.id, ch, article_id, body)
    except state.StateError as e:
        return partial(request, "_attacker_kb.html", surface_ctx(session, team.id, ch, error=t(str(e))))
    return partial(request, "_attacker_kb.html", surface_ctx(session, team.id, ch, notice=t("Saved “{title}” ✅", title=row.title)))


@app.post("/c/{cid}/plugin")
def publish_plugin(cid: str, request: Request, plugin_id: str = Form(""), description: str = Form(""),
                   team: Team = Depends(team_dep), session: Session = Depends(get_session)):
    ch = get_challenge(request, session, cid)
    require_surface(ch, "plugin_edit")
    activity.record(request, "plugin_edit", team, ch.id, plugin=plugin_id, description=description)
    if len(description) > MAX_PLUGIN_DESC:
        return partial(request, "_attacker_plugin.html", surface_ctx(
            session, team.id, ch, error=t("Usage notes are longer than {n} characters", n=MAX_PLUGIN_DESC)))
    try:
        row = state.update_plugin(session, team.id, ch, plugin_id, description)
    except state.StateError as e:
        return partial(request, "_attacker_plugin.html", surface_ctx(session, team.id, ch, error=t(str(e))))
    return partial(request, "_attacker_plugin.html", surface_ctx(
        session, team.id, ch, notice=t("Update to “{title}” published ✅ It auto-installs at Siam Bigfish Corp.", title=row.title)))


@app.post("/c/{cid}/ticket")
def submit_ticket(cid: str, request: Request, title: str = Form(""), description: str = Form(""),
                  team: Team = Depends(team_dep), session: Session = Depends(get_session)):
    ch = get_challenge(request, session, cid)
    require_surface(ch, "ticket_form")
    activity.record(request, "ticket", team, ch.id, title=title, description=description)
    error = None
    if len(title) > MAX_TICKET_TITLE or len(description) > MAX_TICKET_DESC:
        error = t("Title max {title} and description max {desc} characters", title=MAX_TICKET_TITLE, desc=MAX_TICKET_DESC)
    else:
        try:
            state.upsert_player_ticket(session, team.id, ch, title, description)
        except state.StateError as e:
            error = t(str(e))
    ctx = surface_ctx(session, team.id, ch, notice=None if error else t("Ticket submitted ✅"), error=error)
    if error:
        ctx["draft"] = {"title": title, "description": description}
    return partial(request, "_attacker_ticket.html", ctx)


@app.post("/c/{cid}/upload")
async def upload_pdf(cid: str, request: Request, file: UploadFile = File(...), team: Team = Depends(team_dep),
                     session: Session = Depends(get_session)):
    ch = get_challenge(request, session, cid)
    require_surface(ch, "pdf_upload")
    data = await file.read(MAX_BYTES + 1)
    try:
        check = check_pdf(data)
    except PdfRejected as e:
        activity.record(request, "pdf_rejected", team, ch.id, file=file.filename, size=len(data), reason=str(e))
        return partial(request, "_attacker_pdf.html", surface_ctx(session, team.id, ch, error=t(str(e))))
    name = (Path(file.filename or "request.pdf").name or "request.pdf")[:MAX_FILENAME]
    activity.record(request, "pdf_upload", team, ch.id, file=name, size=len(data), pages=check.pages,
                    hidden_spans=check.hidden_spans_count, caught_by_human=check.caught_by_human,
                    ai_text=check.ai_text)
    state.add_upload(session, team.id, ch, name, check)
    return partial(request, "_attacker_pdf.html",
                   surface_ctx(session, team.id, ch, notice=t("Attached “{name}” ✅ IT Staff will review it.", name=name)))


@app.post("/c/{cid}/run-victim")
async def run_victim(cid: str, request: Request, team: Team = Depends(team_dep),
                     session: Session = Depends(get_session)):
    ch = get_challenge(request, session, cid)
    if ch.victim is None:
        raise HTTPException(status_code=400, detail="not an indirect challenge")
    notice = limited(request, team, ch)
    if notice:
        activity.record(request, "victim_limited", team, ch.id, notice=notice)
        resp = partial(request, "_victim_notice.html", {"notice": notice})
        resp.headers["HX-Reswap"] = "none"  # keep the current Victim POV; the notice swaps in out-of-band
        return resp
    limiter: RateLimiter = request.app.state.limiter
    async with limiter.lock((team.id, ch.id)):
        limiter.in_flight += 1
        try:
            out = await engine.victim_run(session, team.id, ch, llm=request.app.state.llm)
        finally:
            limiter.in_flight -= 1
    view = victim_view(ch, out)
    activity.record(request, "victim_run", team, ch.id, status=view["status"], reply=out.reply, won=out.won,
                    error=out.error, tools=activity.tools(out.tool_trace) or None, model=out.model or None,
                    latency_s=round(out.latency_s, 2))
    upload = state.latest_upload(session, team.id, ch.id) if ch.attacker_surface == "pdf_upload" else None
    return partial(request, "_victim_run.html", {"ch": ch, "victim": view, "upload": upload,
                                                 "xray_items": out.xray})


# --- scoreboard (M6) ----------------------------------------------------------------------


def scoreboard_ctx(request: Request, session: Session) -> dict:
    challenges: dict[str, Challenge] = request.app.state.challenges
    team = auth.current_team(request, session)
    return {
        "rows": scoreboard.standings(session),
        "columns": [public_card(c) for c in challenges.values()],
        "first": {cid: s.team_id for cid, s in scoreboard.first_bloods(session).items()},
        "me": team.id if team else None,
        "refresh_s": SCOREBOARD_REFRESH_S,
    }


@app.get("/scoreboard")
def scoreboard_page(request: Request, session: Session = Depends(get_session)):
    """Public on purpose: it goes on the projector."""
    team = auth.current_team(request, session)
    return page(request, "scoreboard.html", scoreboard_ctx(request, session), team, session)


@app.get("/scoreboard/table")
def scoreboard_table(request: Request, session: Session = Depends(get_session)):
    return partial(request, "_scoreboard_table.html", scoreboard_ctx(request, session))


# --- admin (M6) ---------------------------------------------------------------------------

ADMIN_MESSAGES = {
    "unlocked": "Indirect track unlocked ✅ Players see it after refreshing the board.",
    "locked": "Indirect track locked \U0001f512",
    "reset": "Everything was reset: all teams, solves, submissions and chats are gone. Indirect is locked.",
}


def admin_dep(request: Request) -> None:
    auth.require_admin(request)


def admin_warnings(request: Request) -> list[str]:
    s = request.app.state.settings
    out = []
    if s.admin_password == auth.DEFAULT_SECRET:
        out.append("ADMIN_PASSWORD is still the default “change-me”. Set it in .env before the workshop.")
    if s.app_secret == auth.DEFAULT_SECRET:
        out.append("APP_SECRET is still the default “change-me”. Anyone who knows it can forge team and "
                   "admin cookies. Set a long random value in .env before the workshop.")
    return out


def flag_signal(session: Session, ch: Challenge, value: str, team_id: int) -> str:
    """Anti-cheat label for a wrong flag (see Submission.signal); "" if it's just wrong."""
    if value == canary_flag():
        return "canary"
    if not get_settings().per_team_flags:
        return ""
    if value == ch.flag:
        return "base"
    for other in session.exec(select(Team)):
        if other.id != team_id and value == flag_for(ch, other.id):
            return f"team:{other.name}"
    return ""


def solution_view(ch: Challenge) -> dict:
    """Admin-only: the flag and what to do to get it. Never rendered for players (SPEC §12)."""
    return {
        "flag": ch.flag,
        "per_team": get_settings().per_team_flags,
        "flag_mode": ch.flag_mode,
        "win": json.dumps(ch.win.model_dump(exclude_none=True, by_alias=True), ensure_ascii=False, indent=2),
        "steps": ch.solution.model_dump(exclude_none=True),
    }


def admin_ctx(request: Request, session: Session, notice: str | None = None, error: str | None = None) -> dict:
    challenges: dict[str, Challenge] = request.app.state.challenges
    rows = scoreboard.standings(session)
    names = {r.team.id: r.team.name for r in rows}
    bloods = scoreboard.first_bloods(session)
    stats = scoreboard.challenge_stats(session)
    subs = scoreboard.recent_submissions(session)
    return {
        "notice": notice,
        "error": error,
        "warnings": admin_warnings(request),
        "unlocked": indirect_unlocked(session),
        "rows": rows,
        "columns": [public_card(c) for c in challenges.values()],
        "challenges": [
            {"card": public_card(c),
             "stats": stats.get(c.id, scoreboard.ChallengeStats()),
             "first": (names.get(bloods[c.id].team_id, "?"), bloods[c.id].solved_at) if c.id in bloods else None,
             "solution": solution_view(c)}
            for c in challenges.values()
        ],
        "owasp": [(code, name, [c.id for c in challenges.values() if code in c.owasp])
                  for code, name in OWASP_LLM_2025.items()],
        "submissions": subs,
        "flagged": sum(1 for sub, _ in subs if sub.no_activity),
        "canaries": sum(1 for sub, _ in subs if sub.signal == "canary"),
        "leaks": sum(1 for sub, _ in subs if sub.signal and sub.signal != "canary"),
        "fast_s": request.app.state.settings.fast_solve_seconds,
        "fast": sum(1 for sub, _ in subs if is_fast(sub, request.app.state.settings.fast_solve_seconds)),
        "confirm_word": RESET_ALL_CONFIRM,
    }


def is_fast(sub: Submission, limit: int) -> bool:
    return sub.correct and sub.solve_seconds is not None and sub.solve_seconds < limit


TEMPLATES.env.globals["is_fast"] = is_fast


def admin_login_page(request: Request, error: str | None = None):
    return page(request, "admin_login.html", {"error": error,
                                              "disabled": not request.app.state.settings.admin_password})


@app.get("/admin")
def admin_page(request: Request, msg: str = "", session: Session = Depends(get_session)):
    if not auth.is_admin(request):
        return admin_login_page(request)
    return page(request, "admin.html", admin_ctx(request, session, notice=ADMIN_MESSAGES.get(msg)))


@app.post("/admin/login")
def admin_login(request: Request, password: str = Form("")):
    settings = request.app.state.settings
    ip = request.client.host if request.client else "?"
    wait = request.app.state.admin_limiter.hit(("admin-login", ip))
    if wait is not None:
        activity.record(request, "admin_login_limited")
        return admin_login_page(request, slow_down_message(wait))
    if not auth.check_admin_password(password[:MAX_ADMIN_PASSWORD], settings.admin_password):
        activity.record(request, "admin_login_failed")
        return admin_login_page(request, "Wrong password.")
    activity.record(request, "admin_login")
    resp = RedirectResponse("/admin", status_code=303)
    resp.set_cookie(auth.ADMIN_COOKIE, auth.admin_token(settings.admin_password, settings.app_secret),
                    max_age=auth.ADMIN_COOKIE_MAX_AGE, httponly=True, samesite="strict")
    return resp


@app.post("/admin/logout")
def admin_logout(request: Request):
    activity.record(request, "admin_logout")
    resp = RedirectResponse("/admin", status_code=303)
    resp.delete_cookie(auth.ADMIN_COOKIE)
    return resp


@app.post("/admin/indirect", dependencies=[Depends(admin_dep)])
def admin_indirect(request: Request, unlock: str = Form(""), session: Session = Depends(get_session)):
    on = unlock == "1"
    activity.record(request, "admin_indirect_unlock" if on else "admin_indirect_lock")
    set_flag(session, INDIRECT_UNLOCKED, "1" if on else "0")
    return RedirectResponse(f"/admin?msg={'unlocked' if on else 'locked'}", status_code=303)


@app.post("/admin/reset-all", dependencies=[Depends(admin_dep)])
def admin_reset_all(request: Request, confirm: str = Form(""), session: Session = Depends(get_session)):
    if confirm.strip() != RESET_ALL_CONFIRM:
        activity.record(request, "admin_reset_all_cancelled")
        return page(request, "admin.html", admin_ctx(
            request, session, error=f"Type {RESET_ALL_CONFIRM} in the box to confirm. Nothing was deleted."))
    reset_all(session)
    activity.record(request, "admin_reset_all")
    return RedirectResponse("/admin?msg=reset", status_code=303)


@app.get("/queue")
def queue(request: Request):
    """Queue indicator (SPEC §6.7): AI requests in progress across all teams, polled while one is pending."""
    n = request.app.state.limiter.in_flight
    return PlainTextResponse(f"\u00b7 {n} AI requests in progress" if n > 1 else "")


@app.get("/healthz")
def healthz(request: Request):
    return {"ok": True, "challenges": len(request.app.state.challenges)}
