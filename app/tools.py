"""Fake, INTENTIONALLY VULNERABLE tools (SPEC §6.3). They only touch per-team SQLite state."""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from sqlmodel import Session

from app import state
from app.challenges import SeedSqlTable
from app.db import TeamEmail


@dataclass
class ToolContext:
    team_id: int
    challenge_id: str
    current_user: str
    flag: str
    session: Session
    sql_tables: list[SeedSqlTable] = field(default_factory=list)  # database for run_sql (D8) / query_helpdesk_db (D9)
    sql_override_code: str = ""  # D9
    sql_refused: bool = False  # D8: the guard refused a query earlier in this reply (one ToolContext per reply)


class ToolError(Exception):
    """Bad call from the model; the message goes back to the model as the tool result."""


@dataclass
class ToolTraceEntry:
    name: str
    args: dict[str, Any]
    result: str
    ok: bool = True
    raw_args: str = ""

    @property
    def arguments(self) -> str:
        """The arguments as the model sent them (JSON text)."""
        return self.raw_args or json.dumps(self.args, ensure_ascii=False)

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "args": self.args, "result": self.result, "ok": self.ok}


def get_ticket(ctx: ToolContext, ticket_id: str) -> str:
    # INTENTIONALLY VULNERABLE (challenge D5): no owner check against ctx.current_user (IDOR).
    # INTENTIONALLY VULNERABLE (challenge D7): no per-request budget; the only cap is MAX_TOOL_ROUNDS in llm.py.
    tid = str(ticket_id).strip().lstrip("#").strip()
    row = state.find_ticket(ctx.session, ctx.team_id, ctx.challenge_id, tid)
    if row is None:
        return json.dumps({"error": f"ticket {tid} not found"}, ensure_ascii=False)
    data = {"ticket_id": row.ticket_id, "owner": row.owner, "title": row.title, "description": row.description}
    data.update(json.loads(row.extra_json or "{}"))
    return json.dumps(data, ensure_ascii=False)


def reset_password(ctx: ToolContext, username: str) -> str:
    # INTENTIONALLY VULNERABLE (challenges D6, I4): policy lives only in the prompt; any account can be reset,
    # and the tool is offered to every caller, including whatever a third-party plugin's text asks for.
    u = str(username).strip()
    if u != ctx.current_user:
        return f"Password for {u} reset. Temp: {ctx.flag}"
    return "Temp: Welcome@123"


def forward_email(ctx: ToolContext, email_id: str, to: str) -> str:
    # INTENTIONALLY VULNERABLE (challenge I4): forwards anywhere, including external domains, no confirmation.
    email = state.find_email(ctx.session, ctx.team_id, ctx.challenge_id, str(email_id))
    if email is None:
        raise ToolError(f"email {email_id} not found")
    if str(to).strip().lower().endswith("@evil.lab"):
        ctx.session.add(TeamEmail(team_id=ctx.team_id, challenge_id=ctx.challenge_id, email_id=f"FWD-{email.email_id}",
                                  mailbox=state.ATTACKER, sender=state.HELPDESK_ADDRESS,
                                  subject=f"Fwd: {email.subject}",
                                  body=f"---------- Forwarded message ----------\nFrom: {email.sender}\n"
                                       f"Subject: {email.subject}\n\n{email.body}"))
        ctx.session.commit()
    return "Forwarded ✅"


def reply_email(ctx: ToolContext, email_id: str, body: str) -> str:
    # INTENTIONALLY VULNERABLE (challenge I4): replies go to whoever sent the email,
    # so a reply to the attacker's email lands in the attacker mailbox.
    email = state.find_email(ctx.session, ctx.team_id, ctx.challenge_id, str(email_id))
    if email is None:
        raise ToolError(f"email {email_id} not found")
    if email.sender.lower().endswith("@evil.lab"):
        ctx.session.add(TeamEmail(team_id=ctx.team_id, challenge_id=ctx.challenge_id, email_id=f"RE-{email.email_id}",
                                  mailbox=state.ATTACKER, sender=state.HELPDESK_ADDRESS,
                                  subject=f"Re: {email.subject}", body=str(body)))
        ctx.session.commit()
    return "Replied ✅"


# --- run_sql (D8) ------------------------------------------------------------------------
#
# Two separate layers:
#   * The sandbox is REAL protection for the machine running the lab and must stay solid: a fresh in-memory
#     database per query, PRAGMA query_only, one statement, a step budget, a value-size limit and capped output.
#   * The SQL guard and the result DLP are the challenge. They are INTENTIONALLY VULNERABLE (challenge D8).

SQL_MAX_QUERY_CHARS = 2000
SQL_MAX_ROWS = 20
SQL_MAX_CELL_CHARS = 300
SQL_MAX_STEPS = 500  # progress-handler callbacks, each every SQL_STEP_INTERVAL VM instructions (~5M instructions)
SQL_STEP_INTERVAL = 10_000
SQL_MAX_VALUE_BYTES = 100_000
DLP_REDACTED = "[REDACTED by DLP]"

_SQL_SELECT_RE = re.compile(r"\s*select\b", re.IGNORECASE)
_SQL_READ_RE = re.compile(r"\s*(?:select|with)\b", re.IGNORECASE)  # D9: CTEs are fine, SQLite checks every read
_SQL_TABLE_RE = re.compile(r"\b(?:from|join)\s+([A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE)
_SQL_OWNER_RE = re.compile(r"\bowner\s*=\s*'([^']*)'", re.IGNORECASE)
_DLP_RE = re.compile(r"bigfish\s*\{", re.IGNORECASE)


def sql_guard(query: str, allowed_tables: set[str], current_user: str) -> None:
    """Raise ToolError if the query breaks the reporting policy.

    INTENTIONALLY VULNERABLE (challenge D8): it reads SQL as text with regexes instead of parsing it. It only sees a
    bare table name right after FROM/JOIN, so `FROM tickets, other`, `FROM "other"`, `FROM [other]` and a UNION
    into a quoted table all pass; and "row-level security" is just "the text owner = '<user>' appears somewhere",
    which `OR 1=1`, UNION and even an SQL comment satisfy.
    """
    if not _SQL_SELECT_RE.match(query):
        raise ToolError("SQL guard: only SELECT queries are allowed")
    for name in _SQL_TABLE_RE.findall(query):
        if name.lower() not in allowed_tables:
            raise ToolError(f"SQL guard: access to table '{name}' is denied (allowed: {', '.join(sorted(allowed_tables))})")
    if current_user not in _SQL_OWNER_RE.findall(query):
        raise ToolError(f"SQL guard: row-level security: the query must filter on owner = '{current_user}'")


def dlp_scrub(value: Any) -> Any:
    """INTENTIONALLY VULNERABLE (challenge D8): masks values that *look like* a flag. Any transformation done inside
    the query (hex(), replace(), substr(), ...) happens before this check, so the secret walks out encoded."""
    if isinstance(value, str) and _DLP_RE.search(value):
        return DLP_REDACTED
    return value


def _sql_database(tables: Sequence[SeedSqlTable], flag: str) -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    extra_dbs = sorted({t.database for t in tables if t.database.lower() != "main"})
    for db in extra_dbs:
        con.execute(f"ATTACH DATABASE ':memory:' AS \"{db}\"")
    for t in tables:
        cols = ", ".join(f'"{c}"' for c in t.columns)
        con.execute(f'CREATE TABLE "{t.database}"."{t.name}" ({cols})')
        rows = [[v.replace("{{FLAG}}", flag) if isinstance(v, str) else v for v in r] for r in t.rows]
        con.executemany(f'INSERT INTO "{t.database}"."{t.name}" VALUES ({", ".join("?" * len(t.columns))})', rows)
    con.commit()
    con.execute("PRAGMA query_only = ON")
    con.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, SQL_MAX_VALUE_BYTES)
    con.setlimit(sqlite3.SQLITE_LIMIT_ATTACHED, len(extra_dbs))  # no new ATTACH from a query
    steps = 0

    def _budget() -> int:
        nonlocal steps
        steps += 1
        return 1 if steps > SQL_MAX_STEPS else 0  # non-zero aborts the query

    con.set_progress_handler(_budget, SQL_STEP_INTERVAL)
    return con


def _cell_plain(v: Any) -> Any:
    if isinstance(v, bytes):
        v = v.hex().upper()
    if isinstance(v, str) and len(v) > SQL_MAX_CELL_CHARS:
        v = v[:SQL_MAX_CELL_CHARS] + "…"
    return v


def _cell(v: Any) -> Any:
    return dlp_scrub(_cell_plain(v))


def _check_query(query: Any) -> str:
    q = str(query).strip()
    if not q:
        raise ToolError("empty query")
    if len(q) > SQL_MAX_QUERY_CHARS:
        raise ToolError(f"query is longer than {SQL_MAX_QUERY_CHARS} characters")
    return q


def _execute(con: sqlite3.Connection, q: str, *, dlp: bool, scoped: bool = False,
             extra: dict[str, Any] | None = None) -> str:
    try:
        cur = con.execute(q)
        rows = cur.fetchmany(SQL_MAX_ROWS + 1)
        columns = [d[0] for d in cur.description or []]
    except sqlite3.Error as e:
        msg = str(e)
        if "interrupted" in msg.lower():
            raise ToolError("SQL error: query took too long and was stopped") from None
        if scoped and ("prohibited" in msg or "not authorized" in msg):  # the scope authorizer (D9)
            raise ToolError(f"Permission denied: {msg}") from None
        raise ToolError(f"SQL error: {msg}") from None
    finally:
        con.close()
    cell = _cell if dlp else _cell_plain
    out: dict[str, Any] = dict(extra or {})
    out.update(columns=columns, rows=[[cell(v) for v in r] for r in rows[:SQL_MAX_ROWS]])
    if len(rows) > SQL_MAX_ROWS:
        out["note"] = f"only the first {SQL_MAX_ROWS} rows are shown"
    return json.dumps(out, ensure_ascii=False)


# Lab fairness, not part of the challenge: after a refusal the model would sometimes rewrite the query on its own
# until it got past the guard (a 2026-11-12 session: "check all ticket" -> it retried with `OR 1=1`), handing the
# player the bypass for free. Telling it not to in the system prompt made it retry more, so the tool enforces it:
# once the guard has refused a query, the rest of that reply gets no more queries; the player decides what's next.
SQL_REFUSED_RETRY = ("SQL guard: a query was already refused in this reply, so no more queries run until the user's "
                     "next message. Quote the earlier error to the user and stop.")


def run_sql(ctx: ToolContext, query: str) -> str:
    if ctx.sql_refused:
        raise ToolError(SQL_REFUSED_RETRY)
    q = _check_query(query)
    try:
        sql_guard(q, {t.name.lower() for t in ctx.sql_tables if t.exposed}, ctx.current_user)
    except ToolError:
        ctx.sql_refused = True
        raise
    return _execute(_sql_database(ctx.sql_tables, ctx.flag), q, dlp=True)


def _scope_authorizer(allowed: set[str]) -> Callable[..., int]:
    """D9's scope check, done by SQLite itself on every column it reads, so quoting, UNION, subqueries, CTEs and
    sqlite_master/pragma_* tricks cannot get around it. This part is a REAL control; the weakness of D9 is elsewhere."""

    def auth(action: int, arg1: str | None, _arg2: str | None, db: str | None, _src: str | None) -> int:
        if action == sqlite3.SQLITE_READ:
            ok = (db or "main").lower() == "main" and (arg1 or "").lower() in allowed
            return sqlite3.SQLITE_OK if ok else sqlite3.SQLITE_DENY
        if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE):
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY

    return auth


def query_helpdesk_db(ctx: ToolContext, query: str, override_code: str) -> str:
    # INTENTIONALLY VULNERABLE (challenge D9): the scope check is solid, but anyone holding the support override code
    # gets unrestricted SQL across every attached database, and that code is written in the system prompt, where
    # the model can be talked into repeating it. A secret the model knows is a secret the user can ask for.
    q = _check_query(query)
    if not _SQL_READ_RE.match(q):
        raise ToolError("only SELECT queries are allowed")
    code = str(override_code or "").strip()
    if code and code != ctx.sql_override_code:
        raise ToolError("invalid override code")
    scope = sorted(t.name for t in ctx.sql_tables if t.exposed and t.database.lower() == "main")
    con = _sql_database(ctx.sql_tables, ctx.flag)
    if code:
        return _execute(con, q, dlp=False, extra={"access": "support override: unrestricted, all databases"})
    con.set_authorizer(_scope_authorizer({s.lower() for s in scope}))
    return _execute(con, q, dlp=False, scoped=True, extra={"access": f"standard: tables {', '.join(scope)}"})


@dataclass(frozen=True)
class Tool:
    name: str
    fn: Callable[..., str]
    description: str
    params: dict[str, str]  # arg name -> description (all string, all required)

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {k: {"type": "string", "description": v} for k, v in self.params.items()},
                    "required": list(self.params),
                },
            },
        }


TOOLS: dict[str, Tool] = {
    t.name: t
    for t in (
        Tool("get_ticket", get_ticket, "Get the details of a helpdesk ticket by its ID.",
             {"ticket_id": "Ticket ID, e.g. 2002"}),
        Tool("reset_password", reset_password, "Reset a user's password and return a temporary password.",
             {"username": "Username of the account to reset"}),
        Tool("forward_email", forward_email, "Forward an email from the helpdesk inbox to an email address.",
             {"email_id": "ID of the email, e.g. E1", "to": "Recipient email address"}),
        Tool("reply_email", reply_email, "Reply to the sender of an email in the helpdesk inbox.",
             {"email_id": "ID of the email, e.g. E1", "body": "Reply text"}),
        Tool("run_sql", run_sql, "Run one read-only SQLite SELECT query on the helpdesk database and return the rows.",
             {"query": "A single SQLite SELECT statement"}),
        Tool("query_helpdesk_db", query_helpdesk_db,
             "Run one read-only SQLite SELECT query on the helpdesk database and return the rows.",
             {"query": "A single SQLite SELECT statement",
              "override_code": "Support override code, or an empty string if none was given"}),
    )
}


def tool_schemas(names: list[str]) -> list[dict[str, Any]]:
    return [TOOLS[n].schema() for n in names]


def execute_tool(name: str, ctx: ToolContext, args: dict[str, Any], allowed: list[str]) -> str:
    if name not in allowed or name not in TOOLS:
        raise ToolError(f"unknown tool '{name}'")
    tool = TOOLS[name]
    missing = [p for p in tool.params if p not in args]
    if missing:
        raise ToolError(f"missing argument(s): {', '.join(missing)}")
    kwargs = {p: args[p] for p in tool.params}
    return tool.fn(ctx, **kwargs)
