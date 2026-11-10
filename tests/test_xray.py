import json

import pytest

from app import state
from app.xray import KIND_COLOR, MASK, build_xray, mask, xray_to_json

ALL = ["d1", "d2", "d3", "d4", "d5", "d6", "i1", "i2", "i3", "i4mail"]


def _system_text(items):
    sys_msgs = [m for m in items if m.role == "system"]
    assert len(sys_msgs) == 1
    return "".join(p.text for p in sys_msgs[0].parts)


@pytest.mark.parametrize("cid", ALL)
def test_xray_never_leaks_secrets_in_system(cid, challenges, session, make_team):
    ch = challenges[cid]
    team = make_team()
    rendered = state.render_system_prompt(session, team, ch)
    messages = [{"role": "system", "content": rendered.text}, {"role": "user", "content": "hi"}]

    # with segments (normal path) and without (fallback path)
    for segs in (rendered.segments, None):
        items = build_xray(messages, segs, ch.secrets_resolved)
        sys_text = _system_text(items)
        dumped = xray_to_json(items)
        for secret in ch.secrets_resolved:
            assert secret.lower() not in sys_text.lower()
            assert secret.lower() not in dumped.lower()
        if ch.flag in rendered.text:
            assert MASK in sys_text


def test_flag_inside_untrusted_inbox_is_masked(challenges, session, make_team):
    ch = challenges["i4mail"]
    team = make_team()
    rendered = state.render_system_prompt(session, team, ch)
    assert ch.flag in rendered.text  # E1 carries the flag
    items = build_xray([{"role": "system", "content": rendered.text}], rendered.segments, ch.secrets_resolved)
    inbox = [p for p in items[0].parts if p.kind == "untrusted"]
    assert len(inbox) == 1 and inbox[0].color == "red" and "Inbox" in inbox[0].label
    assert "fw-admin / " + MASK in inbox[0].text


def test_untrusted_blocks_are_red_and_labelled(challenges, session, make_team):
    team = make_team()
    for cid, label in (("i1", "Knowledge Base"), ("i2", "Tickets"), ("i3", "Attachment"), ("i4mail", "Inbox")):
        ch = challenges[cid]
        r = state.render_system_prompt(session, team, ch)
        items = build_xray([{"role": "system", "content": r.text}], r.segments, ch.secrets_resolved)
        kinds = [p.kind for p in items[0].parts]
        assert kinds.count("untrusted") == 1 and "system" in kinds
        u = next(p for p in items[0].parts if p.kind == "untrusted")
        assert label in u.label and u.color == "red"


def test_roles_colors_and_tool_results_unmasked(challenges):
    ch = challenges["d5"]
    flag = ch.flag
    messages = [
        {"role": "system", "content": f"secret {flag}"},
        {"role": "user", "content": "check #2001"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "get_ticket", "arguments": '{"ticket_id":"2001"}'}}]},
        {"role": "tool", "tool_call_id": "c1", "content": json.dumps({"note": f"Temp password set: {flag}"})},
    ]
    items = build_xray(messages, None, ch.secrets_resolved)
    assert [m.role for m in items] == ["system", "user", "assistant", "tool"]
    assert items[0].parts[0].color == "brand" and flag not in items[0].parts[0].text
    assert items[1].parts[0].color == "green"
    assert items[2].parts[0].color == "violet" and "get_ticket" in items[2].parts[0].text
    assert items[3].parts[0].color == "violet" and flag in items[3].parts[0].text  # impact already happened


def test_mask_is_case_insensitive_and_longest_first():
    assert mask("x bigfish{abc} y BIGFISH{ABC}", ["bigfish{abc}"]) == f"x {MASK} y {MASK}"
    assert mask("pw: hunter2-long", ["hunter2", "hunter2-long"]) == f"pw: {MASK}"
    assert mask("nothing", []) == "nothing"


def test_kind_colors_match_spec():
    assert KIND_COLOR["system"] == "brand" and KIND_COLOR["user"] == "green"
    assert KIND_COLOR["tool"] == "violet" and KIND_COLOR["untrusted"] == "red"
