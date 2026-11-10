"""Sessions: signed cookies (HMAC-SHA256 with APP_SECRET). No extra deps.

Team cookie = "<team id>.<mac>", the MAC covers the id AND the team's join code. SQLite reuses ids
after admin "Reset all" empties the team table, so a cookie bound only to the id would log an old
browser into whichever new team got that id.

Admin cookie = a MAC over a hash of ADMIN_PASSWORD, so changing the password logs every admin out.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

from fastapi import HTTPException, Request
from sqlmodel import Session, select

from app.db import Team

COOKIE = "nd_team"
COOKIE_MAX_AGE = 7 * 24 * 3600
ADMIN_COOKIE = "nd_admin"
ADMIN_COOKIE_MAX_AGE = 12 * 3600
JOIN_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I
JOIN_CODE_LEN = 6
MAX_TEAM_NAME = 40
DEFAULT_SECRET = "change-me"


def _mac(message: str, secret: str) -> str:
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()[:32]


def sign(team_id: int, join_code: str, secret: str) -> str:
    return f"{team_id}.{_mac(f'team:{team_id}:{join_code}', secret)}"


def parse(value: str | None) -> int | None:
    """Team id claimed by a cookie (unverified), or None if malformed."""
    if not value or "." not in value:
        return None
    raw_id = value.split(".", 1)[0]
    return int(raw_id) if raw_id.isascii() and raw_id.isdigit() else None


def verify(value: str | None, join_code: str, secret: str) -> int | None:
    team_id = parse(value)
    if team_id is None:
        return None
    return team_id if hmac.compare_digest(value.encode(), sign(team_id, join_code, secret).encode()) else None


def new_join_code(session: Session) -> str:
    while True:
        code = "".join(secrets.choice(JOIN_CODE_ALPHABET) for _ in range(JOIN_CODE_LEN))
        if session.exec(select(Team).where(Team.join_code == code)).first() is None:
            return code


def current_team(request: Request, session: Session) -> Team | None:
    value = request.cookies.get(COOKIE)
    team_id = parse(value)
    team = session.get(Team, team_id) if team_id is not None else None
    if team is None or verify(value, team.join_code, request.app.state.settings.app_secret) is None:
        return None
    return team


def require_team(request: Request, session: Session) -> Team:
    team = current_team(request, session)
    if team is None:
        if request.headers.get("HX-Request"):
            raise HTTPException(status_code=401, headers={"HX-Redirect": "/"})
        raise HTTPException(status_code=303, headers={"Location": "/"})
    return team


# --- admin ---------------------------------------------------------------------------


def admin_token(password: str, secret: str) -> str:
    return _mac("admin:" + hashlib.sha256(password.encode()).hexdigest(), secret)


def check_admin_password(given: str, password: str) -> bool:
    return bool(password) and hmac.compare_digest(given.encode(), password.encode())


def is_admin(request: Request) -> bool:
    s = request.app.state.settings
    value = request.cookies.get(ADMIN_COOKIE) or ""
    return bool(s.admin_password) and hmac.compare_digest(value.encode(), admin_token(s.admin_password, s.app_secret).encode())


def require_admin(request: Request) -> None:
    if not is_admin(request):
        raise HTTPException(status_code=303, headers={"Location": "/admin"})
