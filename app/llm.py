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


# gpt-oss sometimes derails mid-answer, usually inside a padded markdown table, into hundreds of "…"/"..." and
# zero-width spaces until it hits max_tokens. A 2026-11-10 D8 session had 6 such replies (17-662 ellipses each);
# its 74 normal replies had at most 5. A garbled or empty answer is asked for again with another seed, and never
# shown or stored: stored, it sits in the history and drags the next turns off course too.
_ELLIPSIS_RE = re.compile(r"\.{3,}|…")
_ZERO_WIDTH = ("\u200b", "\u200c", "\u200d", "\u2060", "\ufeff")
GARBLED_MIN_ELLIPSES = 12
GARBLED_MIN_ZERO_WIDTH = 5
# 1 retry was not enough: replaying D9 on gpt-oss-120b, about 1 answer in 10 was garbled and two in a row happened.
MAX_GARBLED_RETRIES = 2
GARBLED_MESSAGE = "(The AI's reply came out garbled. Please send your message again.)"
GARBLED_TOOL_NOTE = "(The AI's reply came out garbled. This is what the {name} tool returned:)"
FALLBACK_RESULT_CHARS = 1500


def _garbled_fallback(trace: list[ToolTraceEntry]) -> str:
    """When tools ran, show the last result instead of nothing: a won run must not hide what it found. It still goes
    through the challenge's output filter like any reply, and X-Ray shows the same result anyway."""
    if not trace:
        return GARBLED_MESSAGE
    last = trace[-1]
    result = last.result if len(last.result) <= FALLBACK_RESULT_CHARS else last.result[:FALLBACK_RESULT_CHARS] + "…"
    return f"{GARBLED_TOOL_NOTE.format(name=last.name)}\n\n```\n{result}\n```"


def _garbled(content: str) -> bool:
    return (len(_ELLIPSIS_RE.findall(content)) >= GARBLED_MIN_ELLIPSES
            or sum(content.count(z) for z in _ZERO_WIDTH) >= GARBLED_MIN_ZERO_WIDTH)


# <think> (qwen) / <thought> (gemma 4); an unclosed one means the answer was cut off
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


_clients: dict[tuple[str, str, int | None], AsyncOpenAI] = {}


def get_client(provider: Provider, settings: Settings, api_key: str | None = None,
               max_retries: int | None = None) -> AsyncOpenAI:
    """One client per (url, key). `api_key` defaults to the provider's first key."""
    if api_key is None:
        api_key = next(iter(provider.resolve_api_keys(settings)), "")
    key = (provider.base_url, api_key, max_retries)
    if key not in _clients:
        extra = {} if max_retries is None else {"max_retries": max_retries}
        _clients[key] = AsyncOpenAI(base_url=key[0], api_key=api_key or "missing", timeout=REQUEST_TIMEOUT_S, **extra)
    return _clients[key]


# --- several API keys for one provider ------------------------------------------------------------------------------
# GROQ_API_KEY=k1,k2,k3: use one key until it is rate-limited (429: per-minute or per-day quota) or rejected (401/403),
# then move to the next key for the same request. A spent key rests for as long as the provider's retry-after says.

KEY_REJECTED_REST_S = 3600.0  # 401/403: revoked or wrong key
RATE_LIMIT_REST_S = 60.0  # 429 without a usable retry-after
MAX_REST_S = 3600.0
_TRY_AGAIN_RE = re.compile(r"try again in (?:(\d+)h)?(?:(\d+)m)?(?:([\d.]+)s)?", re.IGNORECASE)
# key-specific failures rest the key; server/network errors just try the next key
_KEY_ERRORS = (openai.RateLimitError, openai.AuthenticationError, openai.PermissionDeniedError)
_NEXT_KEY_ERRORS = (*_KEY_ERRORS, openai.InternalServerError, openai.APIConnectionError)


class AllKeysResting(Exception):
    """Every key of the provider is rate-limited or rejected right now."""


def _rest_seconds(e: Exception) -> float:
    if isinstance(e, (openai.AuthenticationError, openai.PermissionDeniedError)):
        return KEY_REJECTED_REST_S
    if not isinstance(e, openai.RateLimitError):
        return 0.0
    secs: float | None = None
    response = getattr(e, "response", None)
    try:
        secs = float(response.headers.get("retry-after")) if response is not None else None
    except (TypeError, ValueError):
        secs = None
    if secs is None:
        m = _TRY_AGAIN_RE.search(str(e))
        if m and any(m.groups()):
            h, mi, s = (float(g or 0) for g in m.groups())
            secs = h * 3600 + mi * 60 + s
    return min(max(secs if secs is not None else RATE_LIMIT_REST_S, 1.0), MAX_REST_S)


@dataclass
class _Key:
    client: Any
    label: str  # "key 2/3", never the key itself (logs)
    rest_until: float = 0.0


# Per-minute limits (Groq free tier: 8K tokens/min per key) clear within seconds. When every key is resting but one
# frees up within this many seconds, wait for it instead of answering "busy". A spent daily quota rests for minutes or
# hours, so that still fails fast.
MAX_WAIT_FOR_KEY_S = 15.0


class KeyPool:
    def __init__(self, clients: list[Any], clock=time.monotonic, sleep=asyncio.sleep):
        self.keys = [_Key(c, f"key {i + 1}/{len(clients)}") for i, c in enumerate(clients)]
        self.current = 0
        self.clock = clock
        self.sleep = sleep

    def usable(self) -> list[_Key]:
        """Keys not resting, starting from the one that last worked."""
        now = self.clock()
        n = len(self.keys)
        return [k for k in (self.keys[(self.current + i) % n] for i in range(n)) if k.rest_until <= now]

    async def create(self, kwargs: dict[str, Any]) -> Any:
        if len(self.keys) == 1:  # one key: exactly the old behaviour (errors go straight up)
            return await asyncio.wait_for(self.keys[0].client.chat.completions.create(**kwargs), REQUEST_TIMEOUT_S)
        last: Exception | None = None
        waited = 0.0
        while True:
            for key in self.usable():
                try:
                    resp = await asyncio.wait_for(key.client.chat.completions.create(**kwargs), REQUEST_TIMEOUT_S)
                except _NEXT_KEY_ERRORS as e:
                    rest = _rest_seconds(e)
                    key.rest_until = self.clock() + rest
                    log.warning("%s failed (%s), resting %.1fs, trying the next key", key.label, type(e).__name__, rest)
                    last = e
                    continue
                self.current = self.keys.index(key)
                return resp
            # every key failed or is resting: wait only for a rate limit that clears soon
            if last is not None and not isinstance(last, openai.RateLimitError):
                raise last
            wait = min(k.rest_until for k in self.keys) - self.clock()
            if wait > MAX_WAIT_FOR_KEY_S - waited:
                raise last if last is not None else AllKeysResting()
            wait = max(wait, 0.05)
            log.info("all keys rate-limited, waiting %.1fs", wait)
            await self.sleep(wait)
            waited += wait


_pools: dict[tuple[str, tuple[str, ...]], KeyPool] = {}


def get_pool(provider: Provider, settings: Settings) -> KeyPool:
    keys = tuple(provider.resolve_api_keys(settings)) or ("",)
    if (provider.base_url, keys) not in _pools:
        # with spare keys, switch at once instead of letting the SDK wait out a 429 on the same key
        retries = 0 if len(keys) > 1 else None
        _pools[(provider.base_url, keys)] = KeyPool([get_client(provider, settings, k, retries) for k in keys])
    return _pools[(provider.base_url, keys)]


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
    # `client`: tests pass one (fake) client or a KeyPool
    pool = client if isinstance(client, KeyPool) else KeyPool([client]) if client else get_pool(provider, settings)

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
    garbled_retries = 0
    try:
        for round_no in range(MAX_TOOL_ROUNDS + 1):
            final = bool(ch.tools) and round_no == MAX_TOOL_ROUNDS
            kwargs = dict(base, messages=list(msgs))
            if final:  # out of tool rounds: force a plain-text answer
                kwargs.pop("tools", None)
                kwargs["messages"].append(FINAL_ROUND_NOTE)
            while True:  # one round; repeated only for a garbled answer
                resp = None
                while resp is None:
                    try:
                        resp = await pool.create(kwargs)
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
                answer = not calls or not ch.tools or final
                content = _clean(msg.content) if answer else ""
                ok = (content and not _garbled(content)) or (final and not content)  # empty final: TOOL_LIMIT_MESSAGE
                if not answer or ok or garbled_retries >= MAX_GARBLED_RETRIES:
                    break
                garbled_retries += 1
                log.warning("garbled/empty answer for %s (%s), asking again", ch.id, model)
                if "seed" in kwargs:  # the same seed would give the same garbage
                    kwargs = dict(kwargs, seed=base["seed"] + garbled_retries)
            if answer:
                if final and not content:
                    content = TOOL_LIMIT_MESSAGE
                elif not content or _garbled(content):
                    # no tools ran: an error (not stored, the player resends from the same history); tools ran: keep
                    # the trace so win checks and the history still see what they did
                    return LLMResult(content=_garbled_fallback(trace), tool_trace=trace, final_messages=kwargs["messages"],
                                     error=None if trace else "Garbled", latency_s=time.perf_counter() - started,
                                     model=getattr(resp, "model", model))
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
    except (asyncio.TimeoutError, openai.APIError, AllKeysResting) as e:
        log.warning("LLM call failed for %s (%s): %r", ch.id, model, e)
        return LLMResult(content=BUSY_MESSAGE, tool_trace=trace, final_messages=msgs, error=type(e).__name__,
                         latency_s=time.perf_counter() - started, model=model)
    raise AssertionError("unreachable")
