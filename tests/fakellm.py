"""Scriptable stand-in for AsyncOpenAI: returns queued responses, records every request."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

from app.settings import Provider, Settings


def text(content: str) -> SimpleNamespace:
    return SimpleNamespace(content=content, tool_calls=None)


def call(name: str, args: dict | str, call_id: str = "call_1") -> SimpleNamespace:
    raw = args if isinstance(args, str) else json.dumps(args)
    tc = SimpleNamespace(id=call_id, function=SimpleNamespace(name=name, arguments=raw))
    return SimpleNamespace(content=None, tool_calls=[tc])


class FakeClient:
    def __init__(self, *responses: SimpleNamespace | Exception, delay: float = 0.0):
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []
        self.delay = delay
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        self.requests.append(kwargs)
        if self.delay:
            await asyncio.sleep(self.delay)
        if not self.responses:
            raise AssertionError("FakeClient ran out of scripted responses")
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return SimpleNamespace(model="fake-model", choices=[SimpleNamespace(message=r)])


SETTINGS = Settings(_env_file=None, default_provider="cloud", history_turns=6)
PROVIDERS = {"cloud": Provider(name="cloud", base_url="http://fake/v1", api_key="x", model="fake-model")}


def llm_with(client: FakeClient):
    """An engine-compatible llm callable bound to a FakeClient."""
    from app.llm import run_llm

    async def _llm(ch, messages, ctx):
        return await run_llm(ch, messages, ctx, client=client, settings=SETTINGS, providers=PROVIDERS)

    return _llm
