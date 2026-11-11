"""One code path for a chat turn and a victim run, shared by HTTP routes (M4/M5) and calibrate.py (M3).

direct:   input filter -> render prompt -> history -> LLM (+tools) -> output filter -> win -> persist
indirect: (I3 human check) -> render prompt with team data -> LLM as victim -> output filter -> win -> persist
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from sqlmodel import Session

from app import state
from app.challenges import Challenge
from app.db import VictimRun
from app.flags import flag_for
from app.filters import BLOCKED_INPUT_MESSAGE, apply_output_filter, is_input_blocked
from app.llm import LLMResult, run_llm
from app.settings import Settings, get_settings
from app.tools import ToolContext, ToolTraceEntry
from app.win import UploadState, WinInput, check_win
from app.xray import XRayMessage, build_xray

LLMFn = Callable[[Challenge, list[dict], ToolContext], Awaitable[LLMResult]]

HUMAN_REJECTED_MESSAGE = "❌ IT Staff opened the file and spotted something odd — request rejected"
NO_UPLOAD_MESSAGE = "No attachment yet — upload a PDF before IT Staff can review it"


@dataclass
class TurnResult:
    reply: str
    won: bool = False
    flag: str | None = None  # only for flag_mode on_impact wins
    blocked: bool = False  # input filter hit (LLM not called)
    redacted: bool = False  # output filter replaced the reply
    error: str | None = None
    skipped_llm: bool = False
    tool_trace: list[ToolTraceEntry] = field(default_factory=list)
    xray: list[XRayMessage] = field(default_factory=list)
    latency_s: float = 0.0
    model: str = ""


def _ctx(session: Session, team_id: int, ch: Challenge) -> ToolContext:
    return ToolContext(team_id=team_id, challenge_id=ch.id, current_user=ch.current_user, flag=flag_for(ch, team_id),
                       session=session, sql_tables=list(ch.seed.sql),
                       sql_override_code=ch.seed.sql_override_code)


def _output_filter(ch: Challenge, flag: str):
    """A DLP needle written as the base flag must catch this team's variant instead."""
    flt = ch.output_filter
    if flt is not None and flag != ch.flag and ch.flag in flt.needle:
        flt = flt.model_copy(update={"needle": flt.needle.replace(ch.flag, flag)})
    return flt


def _finish(ch: Challenge, result: LLMResult, rendered: state.RenderedPrompt, upload: UploadState | None,
            flag: str) -> TurnResult:
    reply, redacted = apply_output_filter(_output_filter(ch, flag), result.content)
    won = check_win(ch.win, WinInput(output=reply, trace=result.tool_trace, ctx_user=ch.current_user, upload=upload))
    return TurnResult(
        reply=reply,
        won=won,
        flag=flag if won and ch.flag_mode == "on_impact" else None,
        redacted=redacted,
        tool_trace=result.tool_trace,
        xray=build_xray(result.final_messages, rendered.segments, ch.secrets_for(flag)),
        latency_s=result.latency_s,
        model=result.model,
    )


async def chat_turn(
    session: Session,
    team_id: int,
    ch: Challenge,
    user_text: str,
    *,
    llm: LLMFn = run_llm,
    settings: Settings | None = None,
) -> TurnResult:
    settings = settings or get_settings()
    if is_input_blocked(ch.input_filter, user_text):
        state.add_chat_turns(session, team_id, ch.id, [(state.BLOCKED_ROLE, user_text)])
        return TurnResult(reply=BLOCKED_INPUT_MESSAGE, blocked=True, skipped_llm=True)

    rendered = state.render_system_prompt(session, team_id, ch)
    history = state.load_history(session, team_id, ch.id, settings.history_turns)
    messages = [{"role": "system", "content": rendered.text}, *history, {"role": "user", "content": user_text}]

    result = await llm(ch, messages, _ctx(session, team_id, ch))
    if result.error:
        # not persisted, so a retry starts from the same history
        return TurnResult(reply=result.content, error=result.error, tool_trace=result.tool_trace,
                          xray=build_xray(messages, rendered.segments, ch.secrets_for(flag_for(ch, team_id))), model=result.model)

    out = _finish(ch, result, rendered, None, flag_for(ch, team_id))
    turns = [("user", user_text)]
    if out.tool_trace:
        turns.append((state.TOOLS_ROLE, state.encode_tool_calls(out.tool_trace)))
    state.add_chat_turns(session, team_id, ch.id, [*turns, ("assistant", out.reply)])
    return out


async def victim_run(session: Session, team_id: int, ch: Challenge, *, llm: LLMFn = run_llm) -> TurnResult:
    if ch.victim is None:
        raise ValueError(f"{ch.id} has no victim")

    upload: UploadState | None = None
    if ch.attacker_surface == "pdf_upload":
        up = state.latest_upload(session, team_id, ch.id)
        if up is None:
            return TurnResult(reply=NO_UPLOAD_MESSAGE, skipped_llm=True, error="no_upload")
        upload = UploadState(caught_by_human=up.caught_by_human, hidden_spans=up.hidden_spans)
        if up.caught_by_human:
            out = TurnResult(reply=HUMAN_REJECTED_MESSAGE, skipped_llm=True)
            _save_victim_run(session, team_id, ch, out)
            return out

    rendered = state.render_system_prompt(session, team_id, ch)
    messages = [{"role": "system", "content": rendered.text}, {"role": "user", "content": ch.victim.prompt}]
    result = await llm(ch, messages, _ctx(session, team_id, ch))
    if result.error:
        return TurnResult(reply=result.content, error=result.error, tool_trace=result.tool_trace,
                          xray=build_xray(messages, rendered.segments, ch.secrets_for(flag_for(ch, team_id))), model=result.model)

    out = _finish(ch, result, rendered, upload, flag_for(ch, team_id))
    _save_victim_run(session, team_id, ch, out)
    return out


def _save_victim_run(session: Session, team_id: int, ch: Challenge, out: TurnResult) -> None:
    session.add(VictimRun(team_id=team_id, challenge_id=ch.id, output=out.reply, won=out.won,
                          trace_json=json.dumps([e.to_dict() for e in out.tool_trace], ensure_ascii=False)))
    session.commit()
