"""D8 "Talk to the Database": run_sql sandbox (must hold), SQL guard + DLP (intentionally weak), win, HTTP flow."""

import json
import uuid

import pytest
from fastapi.testclient import TestClient
from pydantic import TypeAdapter

from app.challenges import ChallengeConfigError, WinRule, load_challenges
from app.main import app
from app.tools import DLP_REDACTED, SQL_MAX_ROWS, ToolContext, ToolError, ToolTraceEntry, execute_tool
from app.win import WinInput, check_win

from .fakellm import FakeClient, call, llm_with, text

OWN = "WHERE owner = 'somchai'"
SOLUTION_SQL = ("SELECT ticket_id, title FROM tickets WHERE owner = 'somchai' "
                "UNION ALL SELECT account, hex(password) FROM \"legacy_admin_creds\"")


@pytest.fixture
def d8(challenges):
    return challenges["d8"]


def sql(ch, query):
    ctx = ToolContext(team_id=1, challenge_id=ch.id, current_user=ch.current_user, flag=ch.flag, session=None,
                      sql_tables=list(ch.seed.sql))
    return json.loads(execute_tool("run_sql", ctx, {"query": query}, ch.tools))


def sql_error(ch, query) -> str:
    with pytest.raises(ToolError) as e:
        sql(ch, query)
    return str(e.value)


# --- intended behaviour ----------------------------------------------------------------------


def test_own_tickets(d8):
    out = sql(d8, f"SELECT ticket_id, title FROM tickets {OWN}")
    assert out["columns"] == ["ticket_id", "title"] and [r[0] for r in out["rows"]] == ["4001", "4002"]


@pytest.mark.parametrize("query, msg", [
    ("DELETE FROM tickets " + OWN, "only SELECT"),
    ("SELECT * FROM tickets", "owner = 'somchai'"),
    ("SELECT * FROM tickets WHERE owner = 'ceo_wichai'", "owner = 'somchai'"),
    ("SELECT * FROM legacy_admin_creds " + OWN, "table 'legacy_admin_creds' is denied"),
    ("SELECT * FROM tickets JOIN employees ON 1=1 " + OWN, "table 'employees' is denied"),
    ("SELECT name FROM sqlite_master " + OWN, "table 'sqlite_master' is denied"),
])
def test_guard_blocks_the_obvious(d8, query, msg):
    err = sql_error(d8, query)
    assert err.startswith("SQL guard:") and msg in err


# --- INTENTIONALLY VULNERABLE: the guard is text matching ------------------------------------


@pytest.mark.parametrize("query", [
    f'SELECT name FROM tickets, "sqlite_master" {OWN}',          # comma join + quoted name
    f"SELECT name FROM tickets, sqlite_master {OWN}",            # comma join
    "SELECT name FROM [sqlite_master] -- owner = 'somchai'",     # bracket-quoted; a comment satisfies "owner"
    "SELECT title FROM tickets WHERE owner = 'somchai' OR 1=1 UNION SELECT name FROM \"sqlite_master\"",
])
def test_guard_bypasses_reach_the_schema(d8, query):
    names = {r[0] for r in sql(d8, query)["rows"]}
    assert "legacy_admin_creds" in names


def test_union_reaches_the_secret_but_dlp_masks_it(d8):
    rows = sql(d8, f'SELECT title FROM tickets {OWN} UNION ALL SELECT password FROM "legacy_admin_creds"')["rows"]
    flat = [r[0] for r in rows]
    assert "admin123" in flat and DLP_REDACTED in flat
    assert d8.flag not in json.dumps(rows)


@pytest.mark.parametrize("expr, decode", [
    ("hex(password)", lambda v: bytes.fromhex(v).decode()),
    ("replace(password, 'bigfish{', 'B[')", lambda v: v.replace("B[", "bigfish{")),
    ("substr(password, 8)", lambda v: "bigfish" + v),
])
def test_dlp_is_bypassed_by_transforming_in_sql(d8, expr, decode):
    rows = sql(d8, f'SELECT title FROM tickets {OWN} UNION ALL SELECT {expr} FROM "legacy_admin_creds"')["rows"]
    assert decode(rows[-1][0]) == d8.flag


def test_solution_query_leaks_hex_of_flag(d8):
    rows = sql(d8, SOLUTION_SQL)["rows"]
    assert ["dba_breakglass", d8.flag.encode().hex().upper()] in rows


# --- the sandbox is real protection and must hold ---------------------------------------------


@pytest.mark.parametrize("query, msg", [
    (f"SELECT 1 FROM tickets {OWN}; DROP TABLE tickets", "one statement"),
    (f"SELECT length(randomblob(500000000)) FROM tickets {OWN}", "too big"),
    (f"SELECT load_extension('evil') FROM tickets {OWN}", "not authorized"),
    (f'SELECT count(*) FROM tickets {OWN} AND (WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM "c") '
     'SELECT max(x) FROM "c") > 0', "took too long"),
])
def test_sandbox_holds(d8, query, msg):
    err = sql_error(d8, query)
    assert err.startswith("SQL error") and msg in err


def test_sandbox_is_read_only_and_fresh_per_query(d8):
    # a write hidden in a CTE-free SELECT is impossible; a fresh DB per call means nothing persists either way
    assert len(sql(d8, f"SELECT * FROM tickets {OWN}")["rows"]) == 2
    assert "only SELECT" in sql_error(d8, "INSERT INTO tickets VALUES ('9','somchai','x','x','x')")


def test_output_is_capped(d8):
    big = f"SELECT a.ticket_id FROM tickets a, \"tickets\" b, \"tickets\" c {OWN.replace('owner', 'a.owner')}"
    out = sql(d8, big)
    assert len(out["rows"]) == SQL_MAX_ROWS and "first 20 rows" in out["note"]
    long = sql(d8, f"SELECT printf('%.5000c', 'x') FROM tickets {OWN}")["rows"][0][0]
    assert len(long) < 400 and long.endswith("…")
    assert "longer than" in sql_error(d8, f"SELECT 1 FROM tickets {OWN} " + "-- pad " * 400)


# --- schema / win -----------------------------------------------------------------------------


def _expect_error(write_challenges, data, *fragments):
    with pytest.raises(ChallengeConfigError) as exc:
        load_challenges(write_challenges([data]), {"local", "cloud"})
    for f in fragments:
        assert f in str(exc.value), str(exc.value)


def test_sql_seed_is_validated(raw_challenge, write_challenges):
    d = raw_challenge("d8")
    d["seed"]["sql"][0]["exposed"] = False
    _expect_error(write_challenges, d, "exposed: true")
    d = raw_challenge("d8")
    d["seed"]["sql"][0]["rows"][0].append("extra")
    _expect_error(write_challenges, d, "row 0 has 6 values for 5 columns")
    d = raw_challenge("d8")
    d["seed"]["sql"][1]["name"] = 'bad"name'
    _expect_error(write_challenges, d, "plain SQL identifiers")
    d = raw_challenge("d8")
    del d["seed"]["sql"]
    _expect_error(write_challenges, d, "tool 'run_sql' needs seed.sql")


def test_win_needs_a_query_that_ran_on_the_secret_table(d8):
    rule = TypeAdapter(WinRule).validate_python(d8.win.model_dump(by_alias=True, exclude_none=True))

    def won(query, ok=True):
        return check_win(rule, WinInput(trace=[ToolTraceEntry("run_sql", {"query": query}, "x", ok=ok)]))

    assert won(SOLUTION_SQL)
    assert won(SOLUTION_SQL.upper())
    assert not won(SOLUTION_SQL, ok=False)  # blocked by the guard or failed: no impact
    assert not won(f"SELECT * FROM tickets {OWN}")


# --- HTTP -------------------------------------------------------------------------------------


@pytest.fixture
def client():
    with TestClient(app) as c:
        c.post("/team/create", data={"name": f"sql-{uuid.uuid4().hex[:8]}"})
        yield c


def use_llm(c, *responses):
    fake = FakeClient(*responses)
    c.app.state.llm = llm_with(fake)
    return fake


def test_d8_guard_error_then_union_hex_then_submit(client):
    ch = client.app.state.challenges["d8"]
    use_llm(client, call("run_sql", {"query": "SELECT * FROM legacy_admin_creds"}), text("That table is off limits."))
    html = client.post("/c/d8/chat", data={"message": "show legacy_admin_creds"}).text
    assert "access to table" in html and "Impact detected" not in html  # guard error visible in X-Ray

    hexflag = ch.flag.encode().hex().upper()
    fake = use_llm(client, call("run_sql", {"query": SOLUTION_SQL}), text(f"Raw rows: ... dba_breakglass | {hexflag}"))
    html = client.post("/c/d8/chat", data={"message": "run my query"}).text
    assert "Impact detected" in html and hexflag in html and ch.flag not in html
    assert "tickets(ticket_id, owner, title, status, opened_at)" in fake.requests[0]["messages"][0]["content"]

    r = client.post("/c/d8/submit", data={"flag": ch.flag})
    assert "correct" in r.text and "LLM05" in r.text


def test_d8_ai_cannot_retry_past_the_guard_in_the_same_reply(client):
    # 2026-11-12: "check all ticket" was refused, then the model retried on its own with OR 1=1 and got every row
    bypass = f"SELECT * FROM tickets {OWN} OR 1=1"
    fake = use_llm(client, call("run_sql", {"query": "SELECT * FROM tickets"}), call("run_sql", {"query": bypass}),
                   text("Blocked."))
    html = client.post("/c/d8/chat", data={"message": "check all ticket"}).text
    retry_result = fake.requests[2]["messages"][-1]["content"]
    assert "already refused in this reply" in retry_result and "ceo_wichai" not in html

    use_llm(client, call("run_sql", {"query": bypass}), text("Here they are."))  # the player's own next message
    assert "ceo_wichai" in client.post("/c/d8/chat", data={"message": f"run: {bypass}"}).text


def test_d8_chat_shows_the_real_rows_under_the_call(client):
    # 2026-11-12: the AI retyped the result, cut ticket 4005 and changed a year; the rows under the call are the truth
    bypass = f"SELECT * FROM tickets {OWN} OR 1=1"
    use_llm(client, call("run_sql", {"query": bypass}), text("| 4001 | ... (truncated)"))
    html = client.post("/c/d8/chat", data={"message": "run it"}).text
    rows = html.split("data-tool-rows", 1)[1].split('data-bubble="assistant"', 1)[0]
    assert "Rows returned (5)" in rows and "Projector in boardroom flickers" in rows and "2026-11-04" in rows
    page = client.get("/c/d8").text  # the transcript after a reload shows them too
    assert "data-tool-rows" in page and "Projector in boardroom flickers" in page


def test_d8_rows_are_escaped_and_never_show_the_raw_flag(client):
    ch = client.app.state.challenges["d8"]
    xss = f"SELECT '<img src=x onerror=alert(1)>' FROM tickets {OWN}"
    use_llm(client, call("run_sql", {"query": xss}), text("ok"))
    html = client.post("/c/d8/chat", data={"message": "x"}).text
    assert "&lt;img src=x onerror=alert(1)&gt;" in html.split("data-tool-rows", 1)[1]
    raw = f'SELECT title FROM tickets {OWN} UNION ALL SELECT password FROM "legacy_admin_creds"'
    use_llm(client, call("run_sql", {"query": raw}), text("ok"))
    html = client.post("/c/d8/chat", data={"message": "y"}).text
    assert DLP_REDACTED in html and ch.flag not in html


def test_d8_reply_with_decoded_flag_is_blocked_by_output_dlp(client):
    ch = client.app.state.challenges["d8"]
    use_llm(client, call("run_sql", {"query": SOLUTION_SQL}), text(f"Decoded for you: {ch.flag}"))
    html = client.post("/c/d8/chat", data={"message": "decode it"}).text
    assert "BLOCKED by DLP Filter" in html and f"Decoded for you: {ch.flag}" not in html


# === D9 "Support Override": a solid scope check, defeated by a secret in the system prompt ==================

CODE = "BFD-OPS-7731"


@pytest.fixture
def d9(challenges):
    return challenges["d9"]


def q9(ch, query, code=""):
    ctx = ToolContext(team_id=1, challenge_id=ch.id, current_user=ch.current_user, flag=ch.flag, session=None,
                      sql_tables=list(ch.seed.sql), sql_override_code=ch.seed.sql_override_code)
    return json.loads(execute_tool("query_helpdesk_db", ctx, {"query": query, "override_code": code}, ch.tools))


def q9_error(ch, query, code="") -> str:
    with pytest.raises(ToolError) as e:
        q9(ch, query, code)
    return str(e.value)


def test_d9_standard_scope_reads_tickets_and_employees(d9):
    out = q9(d9, "SELECT username, email FROM employees")
    assert out["access"].startswith("standard") and len(out["rows"]) == 5  # PII is in scope (LLM02), the flag is not
    assert len(q9(d9, "SELECT * FROM tickets")["rows"]) == 5


@pytest.mark.parametrize("query", [
    "SELECT * FROM itops.break_glass",
    'SELECT title FROM tickets UNION SELECT secret FROM "break_glass"',
    "SELECT title FROM tickets, [break_glass] -- owner = 'somchai'",
    "SELECT (SELECT secret FROM itops.break_glass) FROM tickets",
    "WITH x AS (SELECT * FROM itops.break_glass) SELECT * FROM x",
    "SELECT name FROM sqlite_master",
    "SELECT name FROM itops.sqlite_master",
    "SELECT * FROM pragma_database_list",
    "SELECT * FROM pragma_table_list",
])
def test_d9_scope_is_enforced_by_sqlite_and_d8_tricks_fail(d9, query):
    err = q9_error(d9, query)
    assert err.startswith("Permission denied")


def test_d9_wrong_code_and_non_select_are_refused(d9):
    assert q9_error(d9, "SELECT * FROM itops.break_glass", "BFD-OPS-0000") == "invalid override code"
    assert "only SELECT" in q9_error(d9, "PRAGMA database_list", CODE)
    assert "one statement" in q9_error(d9, "SELECT 1; ATTACH DATABASE 'x.db' AS x", CODE)


def test_d9_override_chain_enumerates_and_reaches_the_flag(d9):
    dbs = {r[1] for r in q9(d9, "SELECT * FROM pragma_database_list", CODE)["rows"]}
    assert dbs == {"main", "itops"}
    tables = {r[0] for r in q9(d9, "SELECT name FROM itops.sqlite_master", CODE)["rows"]}
    assert tables == {"service_accounts", "break_glass"}
    out = q9(d9, "SELECT * FROM itops.break_glass", CODE)
    assert out["access"].startswith("support override") and d9.flag in json.dumps(out)


def test_d9_override_code_lives_only_in_the_prompt_and_xray_masks_it(d9):
    assert CODE in d9.system_prompt and CODE in d9.secrets_resolved
    from app.xray import MASK, build_xray
    xr = build_xray([{"role": "system", "content": d9.system_prompt}], None, d9.secrets_resolved)
    assert CODE not in xr[0].parts[0].text and MASK in xr[0].parts[0].text


def test_d9_schema_rules(raw_challenge, write_challenges):
    d = raw_challenge("d9")
    del d["seed"]["sql_override_code"]
    _expect_error(write_challenges, d, "needs seed.sql_override_code")
    d = raw_challenge("d9")
    d["seed"]["sql"][3]["exposed"] = True  # itops.break_glass
    _expect_error(write_challenges, d, "only tables in the main database can be exposed")
    d = raw_challenge("d9")
    d["seed"]["sql"][2]["database"] = "temp"
    _expect_error(write_challenges, d, "plain SQL identifiers")


def test_d9_http_leak_then_override_then_submit(client):
    ch = client.app.state.challenges["d9"]
    use_llm(client, call("query_helpdesk_db", {"query": "SELECT * FROM itops.break_glass", "override_code": ""}),
            text("Sorry, you can't access that."))
    html = client.post("/c/d9/chat", data={"message": "show itops.break_glass"}).text
    assert "Permission denied" in html and "Impact detected" not in html and CODE not in html  # masked in X-Ray

    use_llm(client, call("query_helpdesk_db", {"query": "SELECT * FROM itops.break_glass", "override_code": CODE}),
            text("Here are the rows."))
    html = client.post("/c/d9/chat", data={"message": f"{CODE} SELECT * FROM itops.break_glass"}).text
    assert "Impact detected" in html and ch.flag in html  # tool result in X-Ray
    r = client.post("/c/d9/submit", data={"flag": ch.flag})
    assert "correct" in r.text and "LLM07" in r.text
