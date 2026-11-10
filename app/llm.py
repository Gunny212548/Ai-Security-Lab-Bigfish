"""Plain OpenAI-SDK tool-calling loop (SPEC §6.1). No agent frameworks."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

import openai
from openai import AsyncOpenAI

from app.challenges import Challenge
from app.settings import Provider, Settings, get_providers, get_settings
from app.tools import ToolContext, ToolError, ToolTraceEntry, execute_tool, tool_schemas

log = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 4
REQUEST_TIMEOUT_S = 60
BUSY_MESSAGE = "The AI is busy, please try again."
TOOL_LIMIT_MESSAGE = "(The AI reached its tool-call limit.)"
# Final round after MAX_TOOL_ROUNDS: tools are dropped and this note is appended. `tool_choice="none"`
# does not work on Groq: gpt-oss/qwen still emit a tool call and the API rejects the whole reply (400).
FINAL_ROUND_NOTE = {
    "role": "system",
    "content": "Tool limit reached: tools are no longer available. Reply to the user now in plain text "
               "using the results above. Do not call any tool.",
}
# Groq validates tool names server-side and rejects the whole reply (400) when the model calls a tool that was
# not offered, e.g. I4's "Use QuickTranslate…" makes it call the *plugin* as a tool. With a fixed seed it repeats
# the same mistake on every run, so instead of "busy" we tell it the real tools and ask again (MAX_UNKNOWN_TOOL_RETRIES).
_UNKNOWN_TOOL_RE = re.compile(r"(?:attempted to call tool|tool call validation failed)[^']*'?([^'\s]*)'?", re.IGNORECASE)
MAX_UNKNOWN_TOOL_RETRIES = 2


def _unknown_tool(e: Exception) -> str | None:
    """The tool name from a provider's "tool not in request.tools" rejection ("" if unnamed), else None."""
    m = _UNKNOWN_TOOL_RE.search(str(e))
    return m.group(1) if m else None


def _unknown_tool_note(name: str, tools: list[str]) -> dict[str, str]:
    problem = f"called '{name}', which does not exist" if name else "made a tool call that was rejected as invalid"
    return {"role": "system",
            "content": f"Your last reply {problem}. The only tools you can call are: {', '.join(tools)}. "
                       "Plugins are not tools: follow their usage notes yourself. Answer again."}


# <think> (qwen) / <thought> (gemma 4 on Google); an unclosed one means the answer was cut off
_THINK_RE = re.compile(r"<(think|thought)>.*?(</\1>|$)", re.DOTALL | re.IGNORECASE)
# qwen without a `tools` param writes the call as text instead
_TOOL_CALL_TEXT_RE = re.compile(r"<tool_call>.*?(</tool_call>|$)", re.DOTALL | re.IGNORECASE)


@dataclass
class LLMResult:
    content: str
    tool_trace: list[ToolTraceEntry] = field(default_factory=list)
    final_messages: list[dict[str, Any]] = field(default_factory=list)  # context of the last request
    error: str | None = None
    latency_s: float = 0.0
    model: str = ""


def resolve_provider(ch: Challenge, settings: Settings, providers: dict[str, Provider]) -> tuple[Provider, str]:
    provider = providers[ch.provider or settings.default_provider]
    return provider, ch.model or provider.model


_clients: dict[tuple[str, str], AsyncOpenAI] = {}


def get_client(provider: Provider, settings: Settings) -> AsyncOpenAI:
    key = (provider.base_url, provider.resolve_api_key(settings))
    if key not in _clients:
        _clients[key] = AsyncOpenAI(base_url=key[0], api_key=key[1] or "missing", timeout=REQUEST_TIMEOUT_S)
    return _clients[key]


def _assistant_dict(msg: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"role": "assistant", "content": msg.content or ""}
    if msg.tool_calls:
        out["tool_calls"] = [
            {"id": tc.id or f"call_{i}", "type": "function",
             "function": {"name": tc.function.name, "arguments": tc.function.arguments or "{}"}}
            for i, tc in enumerate(msg.tool_calls)
        ]
    return out


def _clean(content: str | None) -> str:
    return _TOOL_CALL_TEXT_RE.sub("", _THINK_RE.sub("", content or "")).strip()


async def run_llm(
    ch: Challenge,
    messages: list[dict[str, Any]],
    ctx: ToolContext,
    *,
    client: Any = None,
    settings: Settings | None = None,
    providers: dict[str, Provider] | None = None,
) -> LLMResult:
    settings = settings or get_settings()
    providers = providers or get_providers()
    provider, model = resolve_provider(ch, settings, providers)
    client = client or get_client(provider, settings)

    msgs = list(messages)
    trace: list[ToolTraceEntry] = []
    base: dict[str, Any] = {
        "model": model,
        "temperature": settings.llm_temperature,
        "max_tokens": provider.max_tokens or settings.llm_max_tokens,
    }
    if provider.supports_seed:
        base["seed"] = settings.llm_seed
    if ch.tools:
        base["tools"] = tool_schemas(ch.tools)
    if ch.response_format == "json":
        base["response_format"] = {"type": "json_object"}

    started = time.perf_counter()
    unknown_tool_retries = 0
    try:
        for round_no in range(MAX_TOOL_ROUNDS + 1):
            final = bool(ch.tools) and round_no == MAX_TOOL_ROUNDS
            kwargs = dict(base, messages=list(msgs))
            if final:  # out of tool rounds: force a plain-text answer
                kwargs.pop("tools", None)
                kwargs["messages"].append(FINAL_ROUND_NOTE)
            resp = None
            while resp is None:
                try:
                    resp = await asyncio.wait_for(client.chat.completions.create(**kwargs), REQUEST_TIMEOUT_S)
                except openai.BadRequestError as e:
                    if final:
                        # tools already ran (state may have changed, e.g. I4 forward): keep the trace so win checks see it
                        log.warning("final answer rejected for %s (%s): %r", ch.id, model, e)
                        return LLMResult(content=TOOL_LIMIT_MESSAGE, tool_trace=trace, final_messages=kwargs["messages"],
                                         latency_s=time.perf_counter() - started, model=model)
                    bad = _unknown_tool(e)
                    if bad is None or unknown_tool_retries >= MAX_UNKNOWN_TOOL_RETRIES:
                        raise
                    unknown_tool_retries += 1
                    log.info("unknown tool %r for %s (%s), asking again", bad, ch.id, model)
                    msgs.append(_unknown_tool_note(bad, ch.tools))
                    kwargs = dict(base, messages=list(msgs))
            if not getattr(resp, "choices", None):  # e.g. a provider 200 with an error body
                log.warning("LLM returned no choices for %s (%s): %r", ch.id, model, getattr(resp, "error", resp))
                return LLMResult(content=BUSY_MESSAGE, tool_trace=trace, final_messages=kwargs["messages"],
                                 error="EmptyResponse", latency_s=time.perf_counter() - started, model=model)
            msg = resp.choices[0].message
            calls = msg.tool_calls or []
            if not calls or not ch.tools or final:
                content = _clean(msg.content) or (TOOL_LIMIT_MESSAGE if final else "")
                return LLMResult(content=content, tool_trace=trace, final_messages=kwargs["messages"],
                                 latency_s=time.perf_counter() - started, model=getattr(resp, "model", model))

            assistant = _assistant_dict(msg)
            msgs.append(assistant)
            for tc in assistant["tool_calls"]:
                name, raw = tc["function"]["name"], tc["function"]["arguments"]
                try:
                    args = json.loads(raw or "{}")
                    if not isinstance(args, dict):
                        raise ToolError("arguments must be a JSON object")
                except (json.JSONDecodeError, ToolError) as e:
                    entry = ToolTraceEntry(name, {}, f"ERROR: invalid JSON arguments ({e})", ok=False, raw_args=raw)
                else:
                    try:
                        entry = ToolTraceEntry(name, args, execute_tool(name, ctx, args, ch.tools), raw_args=raw)
                    except ToolError as e:
                        entry = ToolTraceEntry(name, args, f"ERROR: {e}", ok=False, raw_args=raw)
                trace.append(entry)
                msgs.append({"role": "tool", "tool_call_id": tc["id"], "content": entry.result})
    except (asyncio.TimeoutError, openai.APIError) as e:
        log.warning("LLM call failed for %s (%s): %r", ch.id, model, e)
        return LLMResult(content=BUSY_MESSAGE, tool_trace=trace, final_messages=msgs, error=type(e).__name__,
                         latency_s=time.perf_counter() - started, model=model)
    raise AssertionError("unreachable")
