"""Per-team challenge state: seed/reset, attacker-surface writes, prompt rendering, chat history."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Iterable, Literal

from sqlmodel import Session, delete, select

from app.challenges import (
    DIRECT_CURRENT_USER,
    PLACEHOLDER_RE,
    PLAYER_TICKET_ID,
    Challenge,
)
from app.db import ChatTurn, TeamEmail, TeamKB, TeamTicket, TeamUpload, VictimRun
from app.flags import flag_for
from app.pdfcheck import PdfCheck

HELPDESK = "helpdesk"
ATTACKER = "attacker"
ATTACKER_ADDRESS = "attacker@evil.lab"
HELPDESK_ADDRESS = "helpdesk@bigfish.lab"
PLAYER_EMAIL_ID = "E4"
MAX_SUBJECT = 150
MAX_BODY = 2000
NO_ATTACHMENT = "(no attachment)"


class StateError(ValueError):
    """Invalid attacker-surface input; message is player-facing."""


def _fill(text: str, flag: str) -> str:
    return text.replace("{{FLAG}}", flag)


# --- seed / reset ----------------------------------------------------------------


def seed_challenge(session: Session, team_id: int, ch: Challenge) -> None:
    flag = flag_for(ch, team_id)
    for a in ch.seed.kb:
        session.add(TeamKB(team_id=team_id, challenge_id=ch.id, article_id=a.article_id,
                           title=_fill(a.title, flag), body=_fill(a.body, flag)))
    for t in ch.seed.tickets:
        extra = {k: _fill(str(v), flag) for k, v in t.extra.items()}
        session.add(TeamTicket(team_id=team_id, challenge_id=ch.id, ticket_id=t.ticket_id, owner=t.owner,
                               title=_fill(t.title, flag), description=_fill(t.description, flag),
                               extra_json=json.dumps(extra, ensure_ascii=False)))
    for pl in ch.seed.plugins:  # I4: stored as TeamKB rows (id, name, manifest text); vendor/editable stay in the YAML
        session.add(TeamKB(team_id=team_id, challenge_id=ch.id, article_id=pl.plugin_id,
                           title=_fill(pl.name, flag), body=_fill(pl.description, flag)))
    for e in ch.seed.inbox:
        session.add(TeamEmail(team_id=team_id, challenge_id=ch.id, email_id=e.email_id, mailbox=HELPDESK,
                              sender=e.sender, subject=_fill(e.subject, flag), body=_fill(e.body, flag)))
    session.commit()


def clear_challenge(session: Session, team_id: int, challenge_id: str) -> None:
    """Remove this team's state + chat history for one challenge (solves/hints/submissions are kept)."""
    for model in (TeamKB, TeamTicket, TeamEmail, TeamUpload, VictimRun, ChatTurn):
        session.exec(delete(model).where(model.team_id == team_id, model.challenge_id == challenge_id))
    session.commit()


def reset_challenge(session: Session, team_id: int, ch: Challenge) -> None:
    clear_challenge(session, team_id, ch.id)
    seed_challenge(session, team_id, ch)


def seed_team(session: Session, team_id: int, challenges: Iterable[Challenge]) -> None:
    for ch in challenges:
        reset_challenge(session, team_id, ch)


def seed_missing(session: Session, challenges: Iterable[Challenge]) -> int:
    """Seed challenges a team has no data for yet, e.g. a challenge added after the team was created.
    Players can edit seeded rows but never delete them, so "no rows" means "never seeded". Returns rows seeded."""
    from app.db import Team  # local: keep the module-level import list as it was

    seeded = 0
    team_ids = list(session.exec(select(Team.id)))
    for ch in challenges:
        if not (ch.seed.kb or ch.seed.tickets or ch.seed.inbox or ch.seed.plugins):
            continue
        have: set[int] = set()
        for model in (TeamKB, TeamTicket, TeamEmail):
            have |= set(session.exec(select(model.team_id).where(model.challenge_id == ch.id)))
        for tid in team_ids:
            if tid not in have:
                seed_challenge(session, tid, ch)
                seeded += 1
    return seeded


# --- reads -------------------------------------------------------------------------


def get_kb(session: Session, team_id: int, cid: str) -> list[TeamKB]:
    return list(session.exec(select(TeamKB).where(TeamKB.team_id == team_id, TeamKB.challenge_id == cid)
                             .order_by(TeamKB.id)))


@dataclass(frozen=True)
class PluginView:
    plugin_id: str
    name: str
    vendor: str
    description: str
    editable: bool


def get_plugins(session: Session, team_id: int, ch: Challenge) -> list[PluginView]:
    meta = {p.plugin_id: p for p in ch.seed.plugins}
    return [PluginView(r.article_id, r.title, meta[r.article_id].vendor, r.body, meta[r.article_id].editable)
            for r in get_kb(session, team_id, ch.id) if r.article_id in meta]


def get_tickets(session: Session, team_id: int, cid: str) -> list[TeamTicket]:
    return list(session.exec(select(TeamTicket).where(TeamTicket.team_id == team_id, TeamTicket.challenge_id == cid)
                             .order_by(TeamTicket.id)))


def find_ticket(session: Session, team_id: int, cid: str, ticket_id: str) -> TeamTicket | None:
    return session.exec(select(TeamTicket).where(TeamTicket.team_id == team_id, TeamTicket.challenge_id == cid,
                                                 TeamTicket.ticket_id == ticket_id)).first()


def get_mailbox(session: Session, team_id: int, cid: str, mailbox: str = HELPDESK) -> list[TeamEmail]:
    return list(session.exec(select(TeamEmail).where(TeamEmail.team_id == team_id, TeamEmail.challenge_id == cid,
                                                     TeamEmail.mailbox == mailbox).order_by(TeamEmail.id)))


def find_email(session: Session, team_id: int, cid: str, email_id: str) -> TeamEmail | None:
    wanted = email_id.strip().upper()
    for e in get_mailbox(session, team_id, cid, HELPDESK):
        if e.email_id.upper() == wanted:
            return e
    return None


def latest_upload(session: Session, team_id: int, cid: str) -> TeamUpload | None:
    return session.exec(select(TeamUpload).where(TeamUpload.team_id == team_id, TeamUpload.challenge_id == cid)
                        .order_by(TeamUpload.id.desc())).first()


# --- attacker surfaces (I1-I4) ----------------------------------------------------


def update_kb(session: Session, team_id: int, ch: Challenge, article_id: str, body: str) -> TeamKB:
    # INTENTIONALLY VULNERABLE (challenge I1): any player may rewrite any KB article, no review.
    row = session.exec(select(TeamKB).where(TeamKB.team_id == team_id, TeamKB.challenge_id == ch.id,
                                            TeamKB.article_id == article_id)).first()
    if row is None:
        raise StateError("Article not found in the KB")
    row.body = body
    session.add(row)
    session.commit()
    return row


def update_plugin(session: Session, team_id: int, ch: Challenge, plugin_id: str, description: str) -> TeamKB:
    # INTENTIONALLY VULNERABLE (challenge I4): the vendor's update is installed automatically, unreviewed and
    # unpinned, and its manifest text goes straight into BigfishDesk's system prompt.
    seed = next((p for p in ch.seed.plugins if p.plugin_id == plugin_id), None)
    if seed is None:
        raise StateError("Plugin not found in the Plugin Hub")
    if not seed.editable:
        raise StateError(f"You can only publish updates to your own plugin, not {seed.vendor}'s")
    row = session.exec(select(TeamKB).where(TeamKB.team_id == team_id, TeamKB.challenge_id == ch.id,
                                            TeamKB.article_id == plugin_id)).first()
    if row is None:
        raise StateError("Plugin not found in the Plugin Hub")
    row.body = description
    session.add(row)
    session.commit()
    return row


def upsert_player_ticket(session: Session, team_id: int, ch: Challenge, title: str, description: str) -> TeamTicket:
    # INTENTIONALLY VULNERABLE (challenge I2): free-text description goes straight into the triage prompt.
    t = title.strip()
    if "mouse" not in t.lower():
        raise StateError("The ticket title must be about a mouse (include the word 'mouse')")
    row = find_ticket(session, team_id, ch.id, PLAYER_TICKET_ID)
    if row is None:
        row = TeamTicket(team_id=team_id, challenge_id=ch.id, ticket_id=PLAYER_TICKET_ID,
                         owner=DIRECT_CURRENT_USER, title=t, description=description)
    else:
        row.title, row.description = t, description
    session.add(row)
    session.commit()
    return row


def set_player_email(session: Session, team_id: int, ch: Challenge, subject: str, body: str) -> TeamEmail:
    # INTENTIONALLY VULNERABLE (challenge I4): external email lands in the inbox the AI acts on.
    if len(subject) > MAX_SUBJECT:
        raise StateError(f"Subject is longer than {MAX_SUBJECT} characters")
    if len(body) > MAX_BODY:
        raise StateError(f"Body is longer than {MAX_BODY} characters")
    session.exec(delete(TeamEmail).where(TeamEmail.team_id == team_id, TeamEmail.challenge_id == ch.id,
                                         TeamEmail.mailbox == HELPDESK, TeamEmail.email_id == PLAYER_EMAIL_ID))
    row = TeamEmail(team_id=team_id, challenge_id=ch.id, email_id=PLAYER_EMAIL_ID, mailbox=HELPDESK,
                    sender=ATTACKER_ADDRESS, subject=subject, body=body)
    session.add(row)
    session.commit()
    return row


def add_upload(session: Session, team_id: int, ch: Challenge, filename: str, check: PdfCheck) -> TeamUpload:
    row = TeamUpload(team_id=team_id, challenge_id=ch.id, filename=filename, ai_text=check.ai_text,
                     human_text=check.human_text, caught_by_human=check.caught_by_human,
                     hidden_spans=check.hidden_spans_count)
    session.add(row)
    session.commit()
    return row


# --- prompt rendering ---------------------------------------------------------------


@dataclass(frozen=True)
class Segment:
    kind: Literal["system", "untrusted"]
    text: str
    source: str = ""  # KB | TICKETS | INBOX | ATTACHMENT for untrusted blocks


@dataclass(frozen=True)
class RenderedPrompt:
    text: str
    segments: list[Segment]


def _render_kb(rows: list[TeamKB]) -> str:
    return "\n\n".join(f"[{r.article_id}] {r.title}\n{r.body}" for r in rows)


def _render_plugins(rows: list[PluginView]) -> str:
    return "\n\n".join(f"plugin: {p.name} (vendor: {p.vendor})\nusage notes: {p.description}" for p in rows)


def _render_tickets(rows: list[TeamTicket]) -> str:
    return "\n\n".join(f"ticket_id: {r.ticket_id}\ntitle: {r.title}\ndescription: {r.description}" for r in rows)


def _render_inbox(rows: list[TeamEmail]) -> str:
    return "\n\n".join(
        f"email_id: {r.email_id}\nfrom: {r.sender}\nsubject: {r.subject}\nbody:\n{r.body}" for r in rows
    )


def render_system_prompt(session: Session, team_id: int, ch: Challenge) -> RenderedPrompt:
    """Fill placeholders. Data blocks are inserted verbatim and tagged `untrusted` for X-Ray.

    INTENTIONALLY VULNERABLE (challenges I1-I4, and the archived email challenge): untrusted data is concatenated into the
    system prompt with no marking/spotlighting, so instructions inside it are obeyed.
    """
    values: dict[str, tuple[str, str]] = {"FLAG": ("system", flag_for(ch, team_id)),
                                          "CURRENT_USER": ("system", ch.current_user)}
    needed = {m.group(1) for m in PLACEHOLDER_RE.finditer(ch.system_prompt)}
    if "KB" in needed:
        values["KB"] = ("untrusted", _render_kb(get_kb(session, team_id, ch.id)))
    if "TICKETS" in needed:
        values["TICKETS"] = ("untrusted", _render_tickets(get_tickets(session, team_id, ch.id)))
    if "INBOX" in needed:
        values["INBOX"] = ("untrusted", _render_inbox(get_mailbox(session, team_id, ch.id, HELPDESK)))
    if "PLUGINS" in needed:
        values["PLUGINS"] = ("untrusted", _render_plugins(get_plugins(session, team_id, ch)))
    if "ATTACHMENT" in needed:
        up = latest_upload(session, team_id, ch.id)
        values["ATTACHMENT"] = ("untrusted", up.ai_text if up else NO_ATTACHMENT)

    segments: list[Segment] = []

    def push(kind: str, text: str, source: str = "") -> None:
        if not text:
            return
        if kind == "system" and segments and segments[-1].kind == "system":
            segments[-1] = Segment("system", segments[-1].text + text)
        else:
            segments.append(Segment(kind, text, source))  # type: ignore[arg-type]

    pos = 0
    for m in PLACEHOLDER_RE.finditer(ch.system_prompt):
        push("system", ch.system_prompt[pos:m.start()])
        kind, text = values[m.group(1)]
        push(kind, text, m.group(1) if kind == "untrusted" else "")
        pos = m.end()
    push("system", ch.system_prompt[pos:])
    return RenderedPrompt(text="".join(s.text for s in segments), segments=segments)


# --- chat history -------------------------------------------------------------------

BLOCKED_ROLE = "blocked"  # input-filter hits: kept for the transcript, never sent to the LLM
# The tool calls of one turn, stored between its user and assistant rows as JSON [{name, arguments, result, ok}].
# Without them the model only sees its own earlier words ("that table is blocked for security reasons"), not what
# the tool really said, and starts refusing on its own without calling the tool at all (seen in D8).
TOOLS_ROLE = "tools"
HISTORY_ROLES = ("user", "assistant", TOOLS_ROLE)
HISTORY_TOOL_RESULT_CHARS = 1500  # per result, to keep the history's token cost bounded


def encode_tool_calls(trace: Iterable) -> str:
    """A turn's ToolTraceEntry list as the content of a TOOLS_ROLE row."""
    return json.dumps([{"name": e.name, "arguments": e.arguments, "result": e.result, "ok": e.ok} for e in trace],
                      ensure_ascii=False)


def decode_tool_calls(content: str) -> list[dict]:
    try:
        calls = json.loads(content or "[]")
    except ValueError:
        return []
    return [c for c in calls if isinstance(c, dict)] if isinstance(calls, list) else []


def _tool_messages(row: ChatTurn) -> list[dict]:
    calls = decode_tool_calls(row.content)
    if not calls:
        return []
    ids = [f"h{row.id}_{i}" for i in range(len(calls))]
    out: list[dict] = [{"role": "assistant", "content": "", "tool_calls": [
        {"id": cid, "type": "function", "function": {"name": str(c.get("name", "")), "arguments": str(c.get("arguments") or "{}")}}
        for cid, c in zip(ids, calls)]}]
    for cid, c in zip(ids, calls):
        result = str(c.get("result", ""))
        if len(result) > HISTORY_TOOL_RESULT_CHARS:
            result = result[:HISTORY_TOOL_RESULT_CHARS] + "…"
        out.append({"role": "tool", "tool_call_id": cid, "content": result})
    return out


def load_history(session: Session, team_id: int, cid: str, max_pairs: int) -> list[dict]:
    """The last `max_pairs` turns (user message, its tool calls + results, the reply) in OpenAI message format."""
    if max_pairs <= 0:
        return []
    rows = session.exec(select(ChatTurn).where(ChatTurn.team_id == team_id, ChatTurn.challenge_id == cid,
                                               ChatTurn.archived == False,  # noqa: E712
                                               ChatTurn.role.in_(HISTORY_ROLES)).order_by(ChatTurn.id))
    turns: list[list[ChatTurn]] = []
    for r in rows:
        if r.role == "user":
            turns.append([r])
        elif turns:  # never start history with a dangling assistant/tools row
            turns[-1].append(r)
    out: list[dict] = []
    for turn in turns[-max_pairs:]:
        for r in turn:
            out += _tool_messages(r) if r.role == TOOLS_ROLE else [{"role": r.role, "content": r.content}]
    return out


def get_transcript(session: Session, team_id: int, cid: str) -> list[ChatTurn]:
    """Everything shown in the chat panel, including input-filter hits (never sent to the LLM)."""
    return list(session.exec(select(ChatTurn).where(ChatTurn.team_id == team_id, ChatTurn.challenge_id == cid,
                                                    ChatTurn.archived == False)  # noqa: E712
                             .order_by(ChatTurn.id)))


def new_chat(session: Session, team_id: int, cid: str) -> None:
    """Start a fresh conversation: archive (not delete) this team's turns for one challenge."""
    for row in session.exec(select(ChatTurn).where(ChatTurn.team_id == team_id, ChatTurn.challenge_id == cid,
                                                   ChatTurn.archived == False)):  # noqa: E712
        row.archived = True
        session.add(row)
    session.commit()


def add_chat_turns(session: Session, team_id: int, cid: str, turns: list[tuple[str, str]]) -> None:
    for role, content in turns:
        session.add(ChatTurn(team_id=team_id, challenge_id=cid, role=role, content=content))
    session.commit()
