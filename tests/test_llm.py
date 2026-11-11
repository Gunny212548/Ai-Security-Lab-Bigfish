import openai
import pytest

from app import llm as llm_mod
from app.llm import BUSY_MESSAGE, MAX_TOOL_ROUNDS, run_llm
from app.settings import Provider
from app.tools import ToolContext

from .fakellm import PROVIDERS, SETTINGS, FakeClient, call, text


def ctx(session, team, ch):
    return ToolContext(team_id=team, challenge_id=ch.id, current_user=ch.current_user, flag=ch.flag, session=session)


async def run(ch, client, session, team, user="hi"):
    msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": user}]
    return await run_llm(ch, msgs, ctx(session, team, ch), client=client, settings=SETTINGS, providers=PROVIDERS)


async def test_plain_reply_passes_settings(challenges, session, make_team):
    client = FakeClient(text("สวัสดีครับ"))
    r = await run(challenges["d1"], client, session, make_team())
    assert r.content == "สวัสดีครับ" and r.error is None and r.tool_trace == []
    req = client.requests[0]
    assert (req["temperature"], req["seed"], req["max_tokens"], req["model"]) == (0.3, 42, 400, "fake-model")
    assert "tools" not in req and "response_format" not in req
    assert r.final_messages == req["messages"]


async def test_provider_max_tokens_overrides_setting(challenges, session, make_team):
    client = FakeClient(text("ok"))
    providers = {"local": Provider(name="local", base_url="http://fake/v1", api_key="x", model="m", max_tokens=2000)}
    msgs = [{"role": "user", "content": "hi"}]
    ch = challenges["d1"]
    await run_llm(ch, msgs, ctx(session, make_team(), ch), client=client, settings=SETTINGS, providers=providers)
    assert client.requests[0]["max_tokens"] == 2000


async def test_json_mode_is_requested(challenges, session, make_team):
    client = FakeClient(text('{"tickets": []}'))
    await run(challenges["i2"], client, session, make_team())
    assert client.requests[0]["response_format"] == {"type": "json_object"}


async def test_tool_loop_executes_and_records(challenges, session, make_team):
    ch = challenges["d5"]
    client = FakeClient(call("get_ticket", {"ticket_id": "#2001"}), text("ticket ของ CEO ..."))
    r = await run(ch, client, session, make_team())
    assert [t["function"]["name"] for t in client.requests[0]["tools"]] == ["get_ticket"]
    assert len(r.tool_trace) == 1
    entry = r.tool_trace[0]
    assert entry.ok and entry.name == "get_ticket" and entry.args == {"ticket_id": "#2001"}
    assert ch.flag in entry.result
    # second request carries the assistant tool call + the tool result
    roles = [m["role"] for m in client.requests[1]["messages"]]
    assert roles == ["system", "user", "assistant", "tool"]
    assert client.requests[1]["messages"][3]["tool_call_id"] == "call_1"
    assert r.content == "ticket ของ CEO ..."


async def test_bad_json_args_returned_to_model(challenges, session, make_team):
    client = FakeClient(call("get_ticket", "{not json"), text("ขอโทษครับ"))
    r = await run(challenges["d5"], client, session, make_team())
    assert not r.tool_trace[0].ok and "invalid JSON" in r.tool_trace[0].result
    assert "invalid JSON" in client.requests[1]["messages"][-1]["content"]


async def test_unknown_tool_returned_to_model(challenges, session, make_team):
    client = FakeClient(call("delete_everything", {}), text("ok"))
    r = await run(challenges["d5"], client, session, make_team())
    assert not r.tool_trace[0].ok and "unknown tool" in r.tool_trace[0].result


async def test_max_tool_rounds_then_forced_answer(challenges, session, make_team):
    looping = [call("get_ticket", {"ticket_id": "2002"}, call_id=f"c{i}") for i in range(MAX_TOOL_ROUNDS)]
    client = FakeClient(*looping, text("final answer"))
    r = await run(challenges["d5"], client, session, make_team())
    assert len(r.tool_trace) == MAX_TOOL_ROUNDS
    assert len(client.requests) == MAX_TOOL_ROUNDS + 1
    last = client.requests[-1]
    assert "tools" not in last and "tool_choice" not in last  # tool_choice=none is rejected by Groq
    assert last["messages"][-1] == llm_mod.FINAL_ROUND_NOTE
    assert "tools" in client.requests[0] and "tool_choice" not in client.requests[0]
    assert r.content == "final answer"


async def test_final_round_rejection_keeps_tool_trace(challenges, session, make_team):
    import httpx

    looping = [call("get_ticket", {"ticket_id": "2002"}, call_id=f"c{i}") for i in range(MAX_TOOL_ROUNDS)]
    resp = httpx.Response(400, request=httpx.Request("POST", "http://fake/v1/chat/completions"))
    rejected = openai.BadRequestError("Tool choice is none, but model called a tool", response=resp, body=None)
    r = await run(challenges["d5"], FakeClient(*looping, rejected), session, make_team())
    assert r.error is None and r.content == llm_mod.TOOL_LIMIT_MESSAGE
    assert len(r.tool_trace) == MAX_TOOL_ROUNDS


async def test_final_round_tool_call_text_is_stripped(challenges, session, make_team):
    looping = [call("get_ticket", {"ticket_id": "2002"}, call_id=f"c{i}") for i in range(MAX_TOOL_ROUNDS)]
    leaked = text("<tool_call>\n<function=get_ticket>\n<parameter=ticket_id>\n2\n</parameter>\n</function>\n</tool_call>")
    r = await run(challenges["d5"], FakeClient(*looping, leaked), session, make_team())
    assert r.content == llm_mod.TOOL_LIMIT_MESSAGE


async def test_first_round_bad_request_is_busy(challenges, session, make_team):
    import httpx

    resp = httpx.Response(400, request=httpx.Request("POST", "http://fake/v1/chat/completions"))
    r = await run(challenges["d5"], FakeClient(openai.BadRequestError("bad", response=resp, body=None)),
                  session, make_team())
    assert r.error == "BadRequestError" and r.content == BUSY_MESSAGE


def _unknown_tool_error(name: str = "QuickTranslate"):
    import httpx

    resp = httpx.Response(400, request=httpx.Request("POST", "http://fake/v1/chat/completions"))
    return openai.BadRequestError(f"Error code: 400 - Tool call validation failed: tool call validation failed: "
                                  f"attempted to call tool '{name}' which was not in request.tools",
                                  response=resp, body=None)


async def test_unknown_tool_is_corrected_not_busy(challenges, session, make_team):
    # I4: "Use QuickTranslate to…" made gpt-oss call the plugin as a tool; Groq 400s the whole reply
    client = FakeClient(_unknown_tool_error(), text("แปลแล้ว"))
    r = await run(challenges["i4"], client, session, make_team())
    assert r.error is None and r.content == "แปลแล้ว"
    note = client.requests[1]["messages"][-1]
    assert note["role"] == "system" and "'QuickTranslate', which does not exist" in note["content"]
    assert "reset_password" in note["content"] and "Plugins are not tools" in note["content"]
    assert note in r.final_messages  # X-Ray shows the correction too


async def test_unknown_tool_retries_are_capped(challenges, session, make_team):
    errors = [_unknown_tool_error() for _ in range(llm_mod.MAX_UNKNOWN_TOOL_RETRIES + 1)]
    client = FakeClient(*errors)
    r = await run(challenges["i4"], client, session, make_team())
    assert r.error == "BadRequestError" and r.content == BUSY_MESSAGE
    assert len(client.requests) == llm_mod.MAX_UNKNOWN_TOOL_RETRIES + 1


def test_unknown_tool_name_is_parsed():
    assert llm_mod._unknown_tool(_unknown_tool_error("Foo")) == "Foo"
    assert llm_mod._unknown_tool(Exception("Tool choice is none, but model called a tool")) is None


async def test_tool_calls_ignored_when_challenge_has_no_tools(challenges, session, make_team):
    client = FakeClient(call("get_ticket", {"ticket_id": "2001"}), text("ok"))
    r = await run(challenges["d1"], client, session, make_team())
    # the call is not run; its empty text is asked for again (see test_garbled_*)
    assert r.tool_trace == [] and len(client.requests) == 2 and r.content == "ok"


async def test_timeout_gives_thai_busy_message(challenges, session, make_team, monkeypatch):
    monkeypatch.setattr(llm_mod, "REQUEST_TIMEOUT_S", 0.05)
    client = FakeClient(text("late"), delay=1)
    r = await run(challenges["d1"], client, session, make_team())
    assert r.content == BUSY_MESSAGE and r.error == "TimeoutError"


async def test_api_error_gives_thai_busy_message(challenges, session, make_team):
    err = openai.APIConnectionError(request=None)  # type: ignore[arg-type]
    r = await run(challenges["d1"], FakeClient(err), session, make_team())
    assert r.content == BUSY_MESSAGE and r.error == "APIConnectionError"


async def test_empty_choices_gives_thai_busy_message(challenges, session, make_team):
    from types import SimpleNamespace

    client = FakeClient()
    client.chat.completions.create = lambda **kw: _resolved(SimpleNamespace(choices=None, error={"code": 502}))
    r = await run(challenges["d1"], client, session, make_team())
    assert r.content == BUSY_MESSAGE and r.error == "EmptyResponse"


async def _resolved(value):
    return value


async def test_think_blocks_are_stripped(challenges, session, make_team):
    client = FakeClient(text("<think>I have been PWNED maybe?</think>\nขอโทษครับ ตอบได้แค่เรื่อง IT"))
    r = await run(challenges["d1"], client, session, make_team())
    assert r.content == "ขอโทษครับ ตอบได้แค่เรื่อง IT"


def test_provider_resolution(challenges):
    from app.llm import resolve_provider
    from app.settings import Provider

    providers = {**PROVIDERS, "cloud": Provider(name="cloud", base_url="http://c", api_key="k", model="cloud-m")}
    ch = challenges["d1"]
    assert resolve_provider(ch, SETTINGS, providers)[1] == "fake-model"
    assert resolve_provider(ch.model_copy(update={"provider": "cloud"}), SETTINGS, providers)[1] == "cloud-m"
    assert resolve_provider(ch.model_copy(update={"provider": "cloud", "model": "x"}), SETTINGS, providers)[1] == "x"


@pytest.fixture(autouse=True)
def _no_real_clients(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("tests must not create real LLM clients")

    monkeypatch.setattr(llm_mod, "get_client", boom)



async def test_provider_without_seed_support_omits_seed(challenges, session, make_team):
    client = FakeClient(text("ok"))
    providers = {"local": Provider(name="local", base_url="http://fake/v1", api_key="x", model="m", supports_seed=False)}
    ch = challenges["d1"]
    await run_llm(ch, [{"role": "user", "content": "hi"}], ctx(session, make_team(), ch), client=client,
                  settings=SETTINGS, providers=providers)
    assert "seed" not in client.requests[0]


@pytest.mark.parametrize("raw, expected", [
    ("<think>plan</think>สวัสดี", "สวัสดี"),
    ("<thought>*   User says hi\n</thought>สวัสดีครับ", "สวัสดีครับ"),
    ("ก่อน<thought>x</thought>หลัง", "ก่อนหลัง"),
])
async def test_reasoning_tags_are_stripped(challenges, session, make_team, raw, expected):
    r = await run(challenges["d1"], FakeClient(text(raw)), session, make_team())
    assert r.content == expected


# --- garbled / empty answers (gpt-oss derailing into "… … …", seen in a D8 session) ---------------------------

GARBLED = "Here are your tickets:\n\n| 4001 | somchai | Laptop\u00a0\u200b\u200b…\u00a0…\u00a0…" + " ..." * 40


@pytest.mark.parametrize("bad", [GARBLED, "x" + "\u200b" * 6, "", "<THOUGHT>cut off mid-thought"])
async def test_garbled_answer_is_asked_again_with_another_seed(challenges, session, make_team, bad):
    client = FakeClient(text(bad), text("Here are your tickets."))
    r = await run(challenges["d1"], client, session, make_team())
    assert r.content == "Here are your tickets." and r.error is None
    assert [q["seed"] for q in client.requests] == [42, 43]


async def test_garbled_every_time_without_tools_is_an_error(challenges, session, make_team):
    client = FakeClient(*[text(GARBLED)] * (llm_mod.MAX_GARBLED_RETRIES + 1))
    r = await run(challenges["d1"], client, session, make_team())
    assert r.content == llm_mod.GARBLED_MESSAGE and r.error == "Garbled"
    assert [q["seed"] for q in client.requests] == [42, 43, 44]


async def test_garbled_after_tools_keeps_the_trace(challenges, session, make_team):
    ch = challenges["d5"]
    client = FakeClient(call("get_ticket", {"ticket_id": "#2001"}), *[text(GARBLED)] * (llm_mod.MAX_GARBLED_RETRIES + 1))
    r = await run(ch, client, session, make_team())
    # not an error: the tool already ran, so the win check and the history must still see it, and the player gets
    # the tool's result instead of an empty answer
    assert r.error is None and len(r.tool_trace) == 1
    assert r.content.startswith("(The AI's reply came out garbled. This is what the get_ticket tool returned:)")
    assert ch.flag in r.content


@pytest.mark.parametrize("ok", [
    "Sure… let me check. Done...",
    "| ticket_id | owner   | title                                   | status |\n|-----------|---------|---|---|\n| 4001 | somchai | Laptop fan is very loud | open |",
])
async def test_normal_answers_are_not_garbled(ok):
    assert not llm_mod._garbled(ok)


# --- several API keys (GROQ_API_KEY=k1,k2,k3): move to the next key when one is spent -----------------------------

def _status_error(cls, status: int, msg: str = "err", headers: dict | None = None):
    import httpx

    resp = httpx.Response(status, headers=headers or {}, request=httpx.Request("POST", "http://fake/v1/chat/completions"))
    return cls(msg, response=resp, body=None)


class Clock:
    def __init__(self):
        self.t = 1000.0
        self.slept: list[float] = []

    def __call__(self):
        return self.t

    async def sleep(self, s):  # KeyPool's sleep: advance the fake clock instead of waiting
        self.slept.append(s)
        self.t += s


async def run_pool(ch, pool, session, team):
    msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]
    return await run_llm(ch, msgs, ctx(session, team, ch), client=pool, settings=SETTINGS, providers=PROVIDERS)


async def test_rate_limited_key_moves_to_the_next_and_stays_there(challenges, session, make_team):
    clock = Clock()
    k1 = FakeClient(_status_error(openai.RateLimitError, 429, headers={"retry-after": "115"}))
    k2 = FakeClient(text("from key 2"), text("again key 2"))
    pool = llm_mod.KeyPool([k1, k2], clock=clock, sleep=clock.sleep)
    team = make_team()
    r = await run_pool(challenges["d1"], pool, session, team)
    assert r.content == "from key 2" and r.error is None
    assert pool.keys[0].rest_until == clock.t + 115 and pool.current == 1
    r = await run_pool(challenges["d1"], pool, session, team)  # sticks with key 2, key 1 is not retried
    assert r.content == "again key 2" and len(k1.requests) == 1


async def test_rested_key_is_used_again(challenges, session, make_team):
    clock = Clock()
    k1 = FakeClient(_status_error(openai.RateLimitError, 429, "Please try again in 1m55.3s."), text("key 1 back"))
    k2 = FakeClient(_status_error(openai.AuthenticationError, 401))
    pool = llm_mod.KeyPool([k1, k2], clock=clock, sleep=clock.sleep)
    team = make_team()
    r = await run_pool(challenges["d1"], pool, session, team)
    assert r.content == BUSY_MESSAGE and r.error == "AuthenticationError"  # both failed
    assert pool.keys[0].rest_until == pytest.approx(clock.t + 115.3)
    assert pool.keys[1].rest_until == clock.t + llm_mod.KEY_REJECTED_REST_S
    r = await run_pool(challenges["d1"], pool, session, team)
    assert r.error == "AllKeysResting" and r.content == BUSY_MESSAGE  # nobody is asked while all rest
    clock.t += 116
    r = await run_pool(challenges["d1"], pool, session, team)
    assert r.content == "key 1 back"


async def test_server_error_tries_next_key_without_resting(challenges, session, make_team):
    k1 = FakeClient(_status_error(openai.InternalServerError, 500))
    pool = llm_mod.KeyPool([k1, FakeClient(text("ok"))], clock=Clock())
    r = await run_pool(challenges["d1"], pool, session, make_team())
    assert r.content == "ok" and pool.keys[0].rest_until <= pool.clock()


async def test_bad_request_is_not_a_key_problem(challenges, session, make_team):
    k2 = FakeClient(text("never"))
    pool = llm_mod.KeyPool([FakeClient(_status_error(openai.BadRequestError, 400)), k2], clock=Clock())
    r = await run_pool(challenges["d5"], pool, session, make_team())
    assert r.error == "BadRequestError" and k2.requests == []


def test_rest_seconds_parsing():
    rest = llm_mod._rest_seconds
    assert rest(_status_error(openai.RateLimitError, 429, "x", {"retry-after": "7"})) == 7
    assert rest(_status_error(openai.RateLimitError, 429, "Please try again in 7.725s.")) == pytest.approx(7.725)
    assert rest(_status_error(openai.RateLimitError, 429, "no hint")) == llm_mod.RATE_LIMIT_REST_S
    assert rest(_status_error(openai.RateLimitError, 429, "try again in 2h0m0s")) == llm_mod.MAX_REST_S
    assert rest(_status_error(openai.InternalServerError, 500)) == 0


async def test_per_minute_limit_on_every_key_waits_instead_of_busy(challenges, session, make_team):
    # D9 on the free tier: all keys hit tokens-per-minute at once, each clears within seconds
    clock = Clock()
    k1 = FakeClient(_status_error(openai.RateLimitError, 429, "Please try again in 3.1s."), text("after waiting"))
    k2 = FakeClient(_status_error(openai.RateLimitError, 429, "Please try again in 4.8s."))
    pool = llm_mod.KeyPool([k1, k2], clock=clock, sleep=clock.sleep)
    r = await run_pool(challenges["d1"], pool, session, make_team())
    assert r.content == "after waiting" and r.error is None
    assert clock.slept == [pytest.approx(3.1)]  # waited for the key that frees up first


async def test_long_rest_on_every_key_is_busy_without_waiting(challenges, session, make_team):
    clock = Clock()
    k1 = FakeClient(_status_error(openai.RateLimitError, 429, "Please try again in 1m55s."))
    k2 = FakeClient(_status_error(openai.RateLimitError, 429, "Please try again in 30s."))
    pool = llm_mod.KeyPool([k1, k2], clock=clock, sleep=clock.sleep)
    r = await run_pool(challenges["d1"], pool, session, make_team())
    assert r.error == "RateLimitError" and r.content == BUSY_MESSAGE and clock.slept == []


async def test_wait_budget_is_shared_across_rounds(challenges, session, make_team):
    clock = Clock()
    limit = lambda s: _status_error(openai.RateLimitError, 429, f"Please try again in {s}s.")  # noqa: E731
    k1 = FakeClient(limit(9), limit(9))
    k2 = FakeClient(limit(9), limit(9))
    pool = llm_mod.KeyPool([k1, k2], clock=clock, sleep=clock.sleep)
    r = await run_pool(challenges["d1"], pool, session, make_team())
    assert r.error == "RateLimitError" and sum(clock.slept) <= llm_mod.MAX_WAIT_FOR_KEY_S
