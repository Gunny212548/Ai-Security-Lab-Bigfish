"""Activity log: one line per player/admin action, for organizers (`docker compose logs -f app`).

    2026-11-13 10:22:01+0700 | 172.18.0.1 | Red#3 | d7 | chat | msg="ignore previous..." reply="..." won=false

Lines go to stdout (Docker logs) and, when ACTIVITY_LOG_FILE is set, to a rotating file under data/ so the
history survives `docker compose up --build` (which recreates the container and its Docker logs).
"""

from __future__ import annotations

import json
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from fastapi import Request

from app.settings import Settings

log = logging.getLogger("bigfishdesk.activity")

FORMAT = "%(asctime)s %(levelname)s %(name)s | %(message)s"
DATEFMT = "%Y-%m-%d %H:%M:%S%z"
FILE_MAX_BYTES = 20 * 1024 * 1024
FILE_BACKUPS = 10
MAX_TOOL_RESULT = 500  # SQL rows etc. can be long; the player's own text is never cut
_MARK = "_bigfishdesk_handler"


class _ConsoleHandler(logging.StreamHandler):
    """Docker stdout is UTF-8; a Windows console may not be: escape what it can't print instead of failing."""

    def format(self, record: logging.LogRecord) -> str:
        msg = super().format(record)
        enc = getattr(self.stream, "encoding", None) or "utf-8"
        return msg.encode(enc, "backslashreplace").decode(enc)


def setup(settings: Settings) -> None:
    """Send the app's loggers (bigfishdesk.*, app.*) to stdout and the activity file. Safe to call twice."""
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    fmt = logging.Formatter(FORMAT, DATEFMT)
    handlers: list[logging.Handler] = [_ConsoleHandler(sys.stdout)]
    if settings.activity_log_file:
        path = Path(settings.activity_log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(RotatingFileHandler(path, maxBytes=FILE_MAX_BYTES, backupCount=FILE_BACKUPS,
                                            encoding="utf-8"))
    for name in ("bigfishdesk", "app"):
        logger = logging.getLogger(name)
        logger.setLevel(level)
        for h in [h for h in logger.handlers if getattr(h, _MARK, False)]:
            logger.removeHandler(h)
            h.close()
        for h in handlers:
            h.setFormatter(fmt)
            setattr(h, _MARK, True)
            logger.addHandler(h)


def client_ip(request: Request) -> str:
    host = request.client.host if request.client else "?"
    fwd = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    return f"{fwd} via {host}" if fwd and fwd != host else host


def _value(v: Any) -> str:
    if isinstance(v, bool) or v is None:
        return json.dumps(v)
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)  # quotes + escaped newlines: one event = one line
    return json.dumps(v, ensure_ascii=False, default=str)


def tools(trace) -> list[dict]:
    """Tool calls the AI made this turn, for the log."""
    return [{"name": e.name, "args": e.args, "ok": e.ok,
             "result": e.result if len(e.result) <= MAX_TOOL_RESULT else e.result[:MAX_TOOL_RESULT] + "…"}
            for e in trace]


def record(request: Request, event: str, team=None, cid: str | None = None, **fields: Any) -> None:
    who = f"{team.name}#{team.id}" if team is not None else "-"
    parts = [client_ip(request), who, cid or "-", event]
    detail = " ".join(f"{k}={_value(v)}" for k, v in fields.items() if v is not None)
    log.info(" | ".join(parts) + (f" | {detail}" if detail else ""))
