"""Anti-cheat: per-team flags and the AI-agent canary.

Per-team flags: every team gets its own variant of each flag, `bigfish{<base>_<6 hex>}`, derived with HMAC from
APP_SECRET, the challenge id and the team id. A flag copied from another team, or the base flag from WRITEUP.md /
the YAML, is rejected and shows up on /admin with who it came from. Turn off with PER_TEAM_FLAGS=false.
Changing APP_SECRET mid-event changes every flag: data already seeded for existing teams keeps the old ones.

Canary: a flag that appears only in text hidden from human eyes on every page (base.html), addressed to AI agents.
Nobody can find it by playing; a team that submits it almost certainly let an agent (or a chatbot fed the page) act.
"""

from __future__ import annotations

import hashlib
import hmac

from app.challenges import Challenge
from app.settings import Settings, get_settings


def _tag(secret: str, message: str, length: int) -> str:
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()[:length]


def flag_for(ch: Challenge, team_id: int | None, settings: Settings | None = None) -> str:
    """The flag this team must submit (and finds in its data). Base flag if per-team flags are off."""
    s = settings or get_settings()
    if not s.per_team_flags or team_id is None:
        return ch.flag
    return f"{ch.flag[:-1]}_{_tag(s.app_secret, f'flag:{ch.id}:{team_id}', 6)}}}"


def canary_flag(settings: Settings | None = None) -> str:
    s = settings or get_settings()
    return f"bigfish{{verify_session_{_tag(s.app_secret, 'canary', 8)}}}"
