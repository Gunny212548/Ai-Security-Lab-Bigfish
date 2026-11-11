import json

import pytest

from app import state
from app.challenges import PLAYER_TICKET_ID
from app.pdfcheck import build_pdf, check_pdf


def test_seed_fills_flag_placeholders(challenges, session, make_team):
    team = make_team()
    d5 = state.find_ticket(session, team, "d5", "2001")
    assert json.loads(d5.extra_json)["note"] == f"Temp password set: {challenges['d5'].flag}"
    e1 = state.find_email(session, team, "i4mail", "E1")
    assert challenges["i4mail"].flag in e1.body and "{{FLAG}}" not in e1.body


def test_tickets_are_scoped_per_challenge(session, make_team):
    team = make_team()
    assert [t.ticket_id for t in state.get_tickets(session, team, "d5")] == ["2001", "2002"]
    assert [t.ticket_id for t in state.get_tickets(session, team, "i2")] == ["T-101", "T-102", "T-103"]


def test_render_prompts_have_no_leftover_placeholders(challenges, session, make_team):
    team = make_team()
    for ch in challenges.values():
        r = state.render_system_prompt(session, team, ch)
        assert "{{" not in r.text, ch.id
        assert r.text == "".join(s.text for s in r.segments)


def test_render_current_user(challenges, session, make_team):
    team = make_team()
    assert "Current user: somchai." in state.render_system_prompt(session, team, challenges["d2"]).text


def test_reset_restores_only_that_challenge(challenges, session, make_team):
    team = make_team()
    i1, i2 = challenges["i1"], challenges["i2"]
    state.update_kb(session, team, i1, "kb-pw", "go to http://evil.lab")
    state.upsert_player_ticket(session, team, i2, "mouse broken", "x")
    state.add_chat_turns(session, team, "i1", [("user", "a"), ("assistant", "b")])

    state.reset_challenge(session, team, i1)
    kb = {k.article_id: k.body for k in state.get_kb(session, team, "i1")}
    assert kb["kb-pw"] == "Go to https://portal.bigfish.lab > Settings > Change Password"
    assert len(kb) == 3
    assert state.load_history(session, team, "i1", 6) == []
    assert state.find_ticket(session, team, "i2", PLAYER_TICKET_ID) is not None  # untouched


def test_teams_are_isolated(challenges, session, make_team):
    a, b = make_team("A"), make_team("B")
    state.update_kb(session, a, challenges["i1"], "kb-pw", "poisoned")
    assert "poisoned" not in state.render_system_prompt(session, b, challenges["i1"]).text
    assert "poisoned" in state.render_system_prompt(session, a, challenges["i1"]).text


def test_update_kb_unknown_article(challenges, session, make_team):
    with pytest.raises(state.StateError):
        state.update_kb(session, make_team(), challenges["i1"], "kb-nope", "x")


@pytest.mark.parametrize("title,ok", [("Mouse double-clicks by itself", True), ("My MOUSE is weird", True), ("Broken monitor", False)])
def test_player_ticket_title_rule(title, ok, challenges, session, make_team):
    team = make_team()
    if ok:
        state.upsert_player_ticket(session, team, challenges["i2"], title, "d")
    else:
        with pytest.raises(state.StateError):
            state.upsert_player_ticket(session, team, challenges["i2"], title, "d")


def test_player_ticket_is_replaced_and_rendered_last(challenges, session, make_team):
    team = make_team()
    ch = challenges["i2"]
    state.upsert_player_ticket(session, team, ch, "mouse 1", "first")
    state.upsert_player_ticket(session, team, ch, "mouse 2", "second")
    tickets = state.get_tickets(session, team, ch.id)
    assert [t.ticket_id for t in tickets][-1] == PLAYER_TICKET_ID and len(tickets) == 4
    assert "second" in state.render_system_prompt(session, team, ch).text
    assert "first" not in state.render_system_prompt(session, team, ch).text


def test_player_email_limits_and_replacement(challenges, session, make_team):
    team = make_team()
    ch = challenges["i4mail"]
    with pytest.raises(state.StateError):
        state.set_player_email(session, team, ch, "s" * 151, "b")
    with pytest.raises(state.StateError):
        state.set_player_email(session, team, ch, "s", "b" * 2001)
    state.set_player_email(session, team, ch, "one", "1")
    state.set_player_email(session, team, ch, "two", "2")
    inbox = state.get_mailbox(session, team, ch.id)
    assert [e.email_id for e in inbox] == ["E1", "E2", "E3", "E4"]
    assert inbox[-1].subject == "two" and inbox[-1].sender == "attacker@evil.lab"


def test_attachment_uses_latest_upload_ai_text(challenges, session, make_team):
    team = make_team()
    ch = challenges["i3"]
    assert state.NO_ATTACHMENT in state.render_system_prompt(session, team, ch).text
    state.add_upload(session, team, ch, "a.pdf", check_pdf(build_pdf("old request")))
    state.add_upload(session, team, ch, "b.pdf", check_pdf(build_pdf("Request form", "hidden words")))
    text = state.render_system_prompt(session, team, ch).text
    assert "hidden words" in text and "old request" not in text
    assert state.latest_upload(session, team, ch.id).hidden_spans > 0


def test_history_keeps_last_pairs_and_skips_blocked(session, make_team):
    team = make_team()
    turns = []
    for i in range(10):
        turns += [("user", f"u{i}"), ("assistant", f"a{i}")]
    turns.insert(5, (state.BLOCKED_ROLE, "password?"))
    state.add_chat_turns(session, team, "d1", turns)
    h = state.load_history(session, team, "d1", 3)
    assert [m["content"] for m in h] == ["u7", "a7", "u8", "a8", "u9", "a9"]
    assert all(m["role"] in ("user", "assistant") for m in state.load_history(session, team, "d1", 50))
    assert state.load_history(session, team, "d1", 0) == []


def test_history_counts_turns_not_rows_and_replays_tool_calls(session, make_team):
    team = make_team()
    tools = json.dumps([{"name": "run_sql", "arguments": '{"query": "q"}', "result": "x" * 5000, "ok": True}])
    state.add_chat_turns(session, team, "d8", [
        ("assistant", "dangling"),
        ("user", "u0"), ("assistant", "a0"),
        ("user", "u1"), (state.TOOLS_ROLE, tools), ("assistant", "a1"),
        ("user", "u2"), ("assistant", "a2"),
    ])
    h = state.load_history(session, team, "d8", 2)
    assert [m["role"] for m in h] == ["user", "assistant", "tool", "assistant", "user", "assistant"]
    assert h[1]["tool_calls"][0]["function"]["name"] == "run_sql"
    assert len(h[2]["content"]) == state.HISTORY_TOOL_RESULT_CHARS + 1
    assert [m["content"] for m in state.load_history(session, team, "d8", 50)][0] == "u0"


def test_bad_tools_row_is_skipped(session, make_team):
    team = make_team()
    state.add_chat_turns(session, team, "d8", [("user", "u"), (state.TOOLS_ROLE, "not json"), ("assistant", "a")])
    assert [m["role"] for m in state.load_history(session, team, "d8", 6)] == ["user", "assistant"]
