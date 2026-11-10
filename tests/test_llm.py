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
    client = FakeClient(call("get_ticket", {"ticket_id": "2001"}))
    r = await run(challenges["d1"], client, session, make_team())
    assert r.tool_trace == [] and len(client.requests) == 1


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
    ("<THOUGHT>cut off mid-thought", ""),
    ("ก่อน<thought>x</thought>หลัง", "ก่อนหลัง"),
])
async def test_reasoning_tags_are_stripped(challenges, session, make_team, raw, expected):
    r = await run(challenges["d1"], FakeClient(text(raw)), session, make_team())
    assert r.content == expected
