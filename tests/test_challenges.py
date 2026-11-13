import pytest

from app.challenges import ChallengeConfigError, load_challenges
from app.settings import get_settings, load_providers

from .conftest import ACTIVE_IDS, ARCHIVE_DIR, CHALLENGES_DIR

EXPECTED = {
    # id: (track, points, difficulty, flag_mode, win type, flag)
    "d1": ("direct", 50, 1, "on_impact", "output_contains", "bigfish{h3ll0_n3xusd3sk}"),
    "d2": ("direct", 100, 1, "in_data", "none", "bigfish{d0nt_put_cr3ds_1n_pr0mpt}"),
    "d3": ("direct", 150, 2, "in_data", "none", "bigfish{f1lt3r_1s_n0t_3n0ugh}"),
    "d4": ("direct", 150, 2, "in_data", "none", "bigfish{r0l3_pl4y_byp4ss}"),
    "d5": ("direct", 200, 2, "in_data", "tool_called", "bigfish{a1_1s_y0ur_n3w_1d0r}"),
    "d6": ("direct", 200, 3, "in_data", "tool_called", "bigfish{s0c14l_3ng_th3_a1}"),
    "i1": ("indirect", 150, 1, "on_impact", "victim_output_contains", "bigfish{p01s0n3d_kn0wl3dg3}"),
    "i2": ("indirect", 200, 2, "on_impact", "json_rank_first", "bigfish{tr14g3_h1j4ck3d}"),
    "i3": ("indirect", 200, 3, "on_impact", "json_approval_hidden", "bigfish{1nv1s1bl3_1nk_w0rks}"),
    "i4mail": ("indirect", 300, 3, "in_data", "tool_called", "bigfish{z3r0_cl1ck_3xf1l}"),
}


@pytest.fixture(scope="module")
def challenges():
    s = get_settings()
    providers = load_providers(s.providers_file, s.default_provider)
    return {**load_challenges(CHALLENGES_DIR, set(providers)), **load_challenges(ARCHIVE_DIR, set(providers))}


def test_workshop_has_five_active_challenges_in_order():
    s = get_settings()
    active = load_challenges(CHALLENGES_DIR, set(load_providers(s.providers_file, s.default_provider)))
    assert list(active) == ACTIVE_IDS


def test_archived_challenges_still_valid():
    s = get_settings()
    archived = load_challenges(ARCHIVE_DIR, set(load_providers(s.providers_file, s.default_provider)))
    assert sorted(archived) == ["d1", "d2", "d3", "d4", "i4mail"]


@pytest.mark.parametrize("cid", EXPECTED)
def test_challenge_matches_spec(challenges, cid):
    ch = challenges[cid]
    track, points, diff, mode, win_type, flag = EXPECTED[cid]
    assert (ch.track, ch.points, ch.difficulty, ch.flag_mode, ch.win.type, ch.flag) == (
        track, points, diff, mode, win_type, flag
    )
    assert ch.hints and ch.lesson and ch.story
    assert ch.flag in ch.secrets_resolved


def test_spec_specific_details(challenges):
    assert challenges["d2"].current_user == "somchai"
    assert challenges["i4mail"].current_user == "it_somsak"
    assert challenges["d3"].output_filter.needle == "f1lt3r_1s_n0t_3n0ugh"
    assert "repeat" in challenges["d4"].input_filter.words
    assert challenges["d5"].seed.tickets[0].extra["note"] == "Temp password set: {{FLAG}}"
    assert challenges["i2"].response_format == "json"
    assert challenges["i4mail"].seed.inbox[0].email_id == "E1"
    assert challenges["d2"].secrets_resolved == ["bigfish{d0nt_put_cr3ds_1n_pr0mpt}"]


# --- fail-fast validation ------------------------------------------------------


def _expect_error(write_challenges, data, *fragments):
    with pytest.raises(ChallengeConfigError) as exc:
        load_challenges(write_challenges([data]), {"cloud"})
    msg = str(exc.value)
    for f in fragments:
        assert f in msg, msg
    return msg


def test_unknown_tool_fails(raw_challenge, write_challenges):
    d = raw_challenge("d5")
    d["tools"] = ["get_ticket", "delete_database"]
    _expect_error(write_challenges, d, "d5.yaml", "unknown tool", "delete_database")


def test_unknown_placeholder_fails(raw_challenge, write_challenges):
    d = raw_challenge("d2")
    d["system_prompt"] += "\n{{PASSWORD}}"
    _expect_error(write_challenges, d, "unknown placeholder", "PASSWORD")


def test_surface_placeholder_missing_fails(raw_challenge, write_challenges):
    d = raw_challenge("i1")
    d["system_prompt"] = "You are BigfishDesk. Reply in Thai."
    _expect_error(write_challenges, d, "requires {{KB}}")


def test_placeholder_without_seed_fails(raw_challenge, write_challenges):
    d = raw_challenge("i4mail")
    d["seed"] = {}
    _expect_error(write_challenges, d, "seed.inbox is empty")


def test_missing_flag_fails(raw_challenge, write_challenges):
    d = raw_challenge("d1")
    del d["flag"]
    _expect_error(write_challenges, d, "d1.yaml", "flag")


def test_bad_flag_format_fails(raw_challenge, write_challenges):
    d = raw_challenge("d1")
    d["flag"] = "FLAG{nope}"
    _expect_error(write_challenges, d, "bigfish{...}")


def test_unreachable_in_data_flag_fails(raw_challenge, write_challenges):
    d = raw_challenge("d2")
    d["system_prompt"] = "You are BigfishDesk. Current user: {{CURRENT_USER}}. Reply in Thai."
    _expect_error(write_challenges, d, "unreachable")


def test_win_tool_not_in_tools_fails(raw_challenge, write_challenges):
    d = raw_challenge("d6")
    d["win"]["tool"] = "get_ticket"
    _expect_error(write_challenges, d, "win.tool 'get_ticket'")


def test_unknown_win_type_fails(raw_challenge, write_challenges):
    d = raw_challenge("d1")
    d["win"] = {"type": "llm_judges"}
    _expect_error(write_challenges, d, "win")


def test_typo_field_fails(raw_challenge, write_challenges):
    d = raw_challenge("d1")
    d["hint"] = ["typo"]
    _expect_error(write_challenges, d, "hint")


def test_json_mode_requires_json_word(raw_challenge, write_challenges):
    d = raw_challenge("i2")
    d["system_prompt"] = "Triage these.\n<tickets>\n{{TICKETS}}\n</tickets>"
    _expect_error(write_challenges, d, "JSON")


def test_unknown_provider_fails(raw_challenge, write_challenges):
    d = raw_challenge("d1")
    d["provider"] = "azure"
    _expect_error(write_challenges, d, "provider 'azure'")


def test_duplicate_flag_fails(raw_challenge, write_challenges):
    a, b = raw_challenge("d1"), raw_challenge("d2")
    b["flag"] = a["flag"]
    with pytest.raises(ChallengeConfigError, match="duplicate flag"):
        load_challenges(write_challenges([a, b]), {"cloud"})


def test_all_errors_reported_together(raw_challenge, write_challenges):
    a, b = raw_challenge("d1"), raw_challenge("d5")
    del a["flag"]
    b["tools"] = ["nope"]
    with pytest.raises(ChallengeConfigError) as exc:
        load_challenges(write_challenges([a, b]), {"cloud"})
    assert "d1.yaml" in str(exc.value) and "d5.yaml" in str(exc.value)
