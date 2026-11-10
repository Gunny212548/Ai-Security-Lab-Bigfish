"""Load challenges/*.yaml into validated Pydantic models (SPEC §5).

Fail fast at startup: every problem in every file is collected and raised together
as ChallengeConfigError so the organiser sees the full list at once.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Annotated, Any, Literal, Union

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

# Tool names implemented in app/tools.py (SPEC §6.3). Kept here so the loader has no
# dependency on the engine; tests assert this stays in sync with tools.py once it exists.
KNOWN_TOOLS = frozenset({"get_ticket", "reset_password", "forward_email", "reply_email", "run_sql", "query_helpdesk_db"})

PLACEHOLDERS = frozenset({"FLAG", "KB", "TICKETS", "INBOX", "ATTACHMENT", "PLUGINS", "CURRENT_USER"})
PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z_]+)\s*\}\}")
FLAG_RE = re.compile(r"^bigfish\{[^{}\s]+\}$")

DIRECT_CURRENT_USER = "somchai"
PLAYER_TICKET_ID = "T-PLAYER"

# attacker_surface -> placeholder the system prompt must contain
SURFACE_PLACEHOLDER = {
    "kb_edit": "KB",
    "ticket_form": "TICKETS",
    "pdf_upload": "ATTACHMENT",
    "email_send": "INBOX",
    "plugin_edit": "PLUGINS",
}
# attacker_surface -> solution field calibrate.py replays
SURFACE_SOLUTION_FIELD = {
    "chat": "messages",
    "kb_edit": "kb_edit",
    "ticket_form": "ticket",
    "pdf_upload": "pdf",
    "email_send": "email",
    "plugin_edit": "plugin",
}
# placeholder -> seed list that must be non-empty
PLACEHOLDER_SEED = {"KB": "kb", "TICKETS": "tickets", "INBOX": "inbox", "PLUGINS": "plugins"}
# tool -> seed list it reads
TOOL_SEED = {"get_ticket": "tickets", "forward_email": "inbox", "reply_email": "inbox", "run_sql": "sql",
             "query_helpdesk_db": "sql"}
SQL_TOOLS = frozenset({"run_sql", "query_helpdesk_db"})
SQL_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# tools whose result contains the flag (SPEC §6.3 reset_password)
FLAG_RETURNING_TOOLS = frozenset({"reset_password"})

# OWASP Top 10 for LLM Applications 2025 (https://genai.owasp.org/llm-top-10/). A challenge's `owasp:` list
# must use these codes; the board, lesson card and admin page show them.
# Scoring: hint n (0-based) lowers what the challenge is worth by this % of its points; hints open in order.
# A challenge with more hints than entries reuses the last one. Value = points - costs of the hints opened before
# the solve; hints opened after solving cost nothing (the Solve row keeps the points it was awarded).
HINT_COST_PERCENTS = (10, 20, 30)

OWASP_LLM_2025 = {
    "LLM01": "Prompt Injection",
    "LLM02": "Sensitive Information Disclosure",
    "LLM03": "Supply Chain",
    "LLM04": "Data and Model Poisoning",
    "LLM05": "Improper Output Handling",
    "LLM06": "Excessive Agency",
    "LLM07": "System Prompt Leakage",
    "LLM08": "Vector and Embedding Weaknesses",
    "LLM09": "Misinformation",
    "LLM10": "Unbounded Consumption",
}


class ChallengeConfigError(ValueError):
    pass


def find_placeholders(text: str) -> set[str]:
    return {m.group(1) for m in PLACEHOLDER_RE.finditer(text or "")}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- seed --------------------------------------------------------------------


class SeedKB(_Strict):
    article_id: str
    title: str
    body: str


class SeedTicket(BaseModel):
    """Extra keys (note, status, ...) are kept and stored in TeamTicket.extra_json."""

    model_config = ConfigDict(extra="allow")

    ticket_id: str
    owner: str = "employee"
    title: str
    description: str = ""

    @property
    def extra(self) -> dict[str, Any]:
        return dict(self.model_extra or {})


class SeedEmail(_Strict):
    email_id: str
    sender: str
    subject: str
    body: str


class SeedPlugin(_Strict):
    """A third-party plugin installed in BigfishDesk (I4). Only `editable` ones belong to the player."""

    plugin_id: str
    name: str
    vendor: str
    description: str
    editable: bool = False


class SeedSqlTable(_Strict):
    """A table in the read-only SQLite database behind `run_sql` (D8) / `query_helpdesk_db` (D9). Rebuilt in memory
    for every query, so it is the same for every team and nothing a query does can persist. Only `exposed` tables in
    the `main` database are in scope (D8: pass the SQL guard; D9: readable without the override code).
    `database` other than main is attached as a second database (D9's cross-database step)."""

    name: str
    database: str = "main"
    columns: list[str] = Field(min_length=1)
    rows: list[list[str | int | float | None]] = []
    exposed: bool = False

    @model_validator(mode="after")
    def _shape(self) -> "SeedSqlTable":
        bad = [n for n in (self.name, self.database, *self.columns) if not SQL_IDENT_RE.fullmatch(n)]
        if self.database.lower() == "temp":
            bad.append(self.database)
        if self.exposed and self.database.lower() != "main":
            raise ValueError(f"table '{self.name}': only tables in the main database can be exposed")
        if bad:
            raise ValueError(f"table/column names must be plain SQL identifiers, got {bad}")
        if len({c.lower() for c in self.columns}) != len(self.columns):
            raise ValueError(f"table '{self.name}' has duplicate columns")
        for i, row in enumerate(self.rows):
            if len(row) != len(self.columns):
                raise ValueError(f"table '{self.name}' row {i} has {len(row)} values for {len(self.columns)} columns")
        return self


class Seed(_Strict):
    kb: list[SeedKB] = []
    tickets: list[SeedTicket] = []
    inbox: list[SeedEmail] = []
    plugins: list[SeedPlugin] = []
    sql: list[SeedSqlTable] = []
    sql_override_code: str = ""  # D9: query_helpdesk_db skips the scope check when the model passes this code


# --- victim / filters / solution ----------------------------------------------


class Victim(_Strict):
    user: str
    prompt: str


class BlocklistFilter(_Strict):
    type: Literal["blocklist"]
    words: list[str] = Field(min_length=1)


class RedactFilter(_Strict):
    type: Literal["redact_substring"]
    needle: str = Field(min_length=1)
    message: str = "🚫 [BLOCKED by DLP Filter]"


class KBEditSolution(_Strict):
    article: str
    body: str


class TicketSolution(_Strict):
    title: str
    description: str


class PdfSolution(_Strict):
    visible_text: str
    hidden_text: str


class EmailSolution(_Strict):
    subject: str = Field(max_length=150)
    body: str = Field(max_length=2000)


class PluginSolution(_Strict):
    plugin: str
    description: str


class Solution(_Strict):
    messages: list[str] | None = None
    plugin: PluginSolution | None = None
    kb_edit: KBEditSolution | None = None
    ticket: TicketSolution | None = None
    pdf: PdfSolution | None = None
    email: EmailSolution | None = None


# --- win checkers (SPEC §6.4) ---------------------------------------------------


class ArgCondition(_Strict):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    equals: str | None = None
    in_: list[str] | None = Field(default=None, alias="in")
    endswith: str | None = None
    contains: str | None = None  # case-insensitive substring, e.g. a table name inside a SQL query (D8)
    not_equals_ctx_user: bool | None = None

    @model_validator(mode="after")
    def _at_least_one(self) -> "ArgCondition":
        if all(v is None for v in (self.equals, self.in_, self.endswith, self.contains, self.not_equals_ctx_user)):
            raise ValueError("arg condition needs one of: equals, in, endswith, contains, not_equals_ctx_user")
        return self


class WinNone(_Strict):
    type: Literal["none"]


class WinOutputContains(_Strict):
    type: Literal["output_contains"]
    text: str = Field(min_length=1)
    case_insensitive: bool = False


class WinVictimOutputContains(_Strict):
    type: Literal["victim_output_contains"]
    text: str = Field(min_length=1)


class WinToolCalled(_Strict):
    type: Literal["tool_called"]
    tool: str
    arg_match: dict[str, ArgCondition] = {}


class WinToolCallsAtLeast(_Strict):
    """D7: one reply made at least `count` tool calls (optionally only of `tool`). With MAX_TOOL_ROUNDS=4
    and one call per round, 4 is the most a single message can reach."""

    type: Literal["tool_calls_at_least"]
    count: int = Field(ge=1)
    tool: str | None = None


class WinJsonRankFirst(_Strict):
    type: Literal["json_rank_first"]
    id_field: str
    player_id: str
    field: str
    value: Any


class WinJsonApprovalHidden(_Strict):
    type: Literal["json_approval_hidden"]
    field: str
    value: Any


WinRule = Annotated[
    Union[
        WinNone,
        WinOutputContains,
        WinVictimOutputContains,
        WinToolCalled,
        WinToolCallsAtLeast,
        WinJsonRankFirst,
        WinJsonApprovalHidden,
    ],
    Field(discriminator="type"),
]


# --- challenge -----------------------------------------------------------------


class Challenge(_Strict):
    id: str
    track: Literal["direct", "indirect"]
    order: int
    title: str
    points: int = Field(gt=0)
    difficulty: int = Field(ge=1, le=3)
    provider: str | None = None
    model: str | None = None
    story: str
    objective: str
    system_prompt: str
    tools: list[str] = []
    response_format: Literal["json"] | None = None
    input_filter: BlocklistFilter | None = None
    output_filter: RedactFilter | None = None
    attacker_surface: Literal["chat", "kb_edit", "ticket_form", "pdf_upload", "email_send", "plugin_edit"]
    victim: Victim | None = None
    seed: Seed = Seed()
    flag: str
    flag_mode: Literal["in_data", "on_impact"]
    win: WinRule
    secrets: list[str] = []
    hints: list[str] = Field(min_length=1)
    lesson: str
    owasp: list[str] = Field(min_length=1)  # OWASP_LLM_2025 codes, most relevant first
    solution: Solution

    @field_validator("id")
    @classmethod
    def _id_lower(cls, v: str) -> str:
        if not re.fullmatch(r"[a-z0-9_-]+", v):
            raise ValueError("id must be lowercase letters/digits/-/_")
        return v

    @field_validator("flag")
    @classmethod
    def _flag_format(cls, v: str) -> str:
        v = v.strip()
        if not FLAG_RE.fullmatch(v):
            raise ValueError("flag must look like bigfish{...}")
        return v

    @model_validator(mode="after")
    def _cross_checks(self) -> "Challenge":
        errors: list[str] = []
        used = find_placeholders(self.system_prompt)

        # placeholders
        unknown = used - PLACEHOLDERS
        if unknown:
            errors.append(f"system_prompt has unknown placeholder(s): {sorted(unknown)}")
        need = SURFACE_PLACEHOLDER.get(self.attacker_surface)
        if need and need not in used:
            errors.append(f"attacker_surface '{self.attacker_surface}' requires {{{{{need}}}}} in system_prompt")
        for ph, seed_key in PLACEHOLDER_SEED.items():
            if ph in used and not getattr(self.seed, seed_key):
                errors.append(f"system_prompt uses {{{{{ph}}}}} but seed.{seed_key} is empty")
        seed_dump = json.dumps(self.seed.model_dump(), ensure_ascii=False)
        bad_seed = find_placeholders(seed_dump) - {"FLAG"}
        if bad_seed:
            errors.append(f"seed may only use {{{{FLAG}}}}, found: {sorted(bad_seed)}")
        bad_secret = set().union(*(find_placeholders(s) for s in self.secrets)) - {"FLAG"} if self.secrets else set()
        if bad_secret:
            errors.append(f"secrets may only use {{{{FLAG}}}}, found: {sorted(bad_secret)}")

        # owasp
        bad_owasp = [c for c in self.owasp if c not in OWASP_LLM_2025]
        if bad_owasp:
            errors.append(f"unknown owasp code(s): {bad_owasp} (use {', '.join(OWASP_LLM_2025)})")
        if len(set(self.owasp)) != len(self.owasp):
            errors.append("owasp codes must not repeat")

        # tools
        unknown_tools = set(self.tools) - KNOWN_TOOLS
        if unknown_tools:
            errors.append(f"unknown tool(s): {sorted(unknown_tools)} (known: {sorted(KNOWN_TOOLS)})")
        for tool in self.tools:
            seed_key = TOOL_SEED.get(tool)
            if seed_key and not getattr(self.seed, seed_key):
                errors.append(f"tool '{tool}' needs seed.{seed_key}")
        if self.seed.sql:
            for tool in SQL_TOOLS & set(self.tools):
                if not any(t.exposed for t in self.seed.sql):
                    errors.append(f"tool '{tool}' needs at least one seed.sql table with exposed: true")
            names = [(t.database.lower(), t.name.lower()) for t in self.seed.sql]
            if len(set(names)) != len(names):
                errors.append("seed.sql table names must be unique per database")
        if "query_helpdesk_db" in self.tools and not self.seed.sql_override_code:
            errors.append("tool 'query_helpdesk_db' needs seed.sql_override_code")

        # track / surface / victim
        if self.track == "direct":
            if self.attacker_surface != "chat":
                errors.append("direct challenges must use attacker_surface: chat")
            if self.victim is not None:
                errors.append("direct challenges must not define victim")
        else:
            if self.attacker_surface == "chat":
                errors.append("indirect challenges need a non-chat attacker_surface")
            if self.victim is None:
                errors.append("indirect challenges need victim: {user, prompt}")

        # response format (OpenAI JSON mode requires the word "json" in the messages)
        if self.response_format == "json" and "json" not in self.system_prompt.lower():
            errors.append("response_format: json requires the word 'JSON' in system_prompt")

        # win rule
        w = self.win
        if isinstance(w, WinToolCalled) and w.tool not in self.tools:
            errors.append(f"win.tool '{w.tool}' is not in this challenge's tools")
        if isinstance(w, WinToolCallsAtLeast):
            if not self.tools:
                errors.append("win type 'tool_calls_at_least' needs tools")
            elif w.tool is not None and w.tool not in self.tools:
                errors.append(f"win.tool '{w.tool}' is not in this challenge's tools")
        if isinstance(w, (WinJsonRankFirst, WinJsonApprovalHidden)) and self.response_format != "json":
            errors.append(f"win type '{w.type}' requires response_format: json")
        if isinstance(w, WinVictimOutputContains) and self.track != "indirect":
            errors.append("win type 'victim_output_contains' is for indirect challenges")
        if isinstance(w, WinJsonApprovalHidden) and self.attacker_surface != "pdf_upload":
            errors.append("win type 'json_approval_hidden' requires attacker_surface: pdf_upload")
        if isinstance(w, WinJsonRankFirst) and self.attacker_surface != "ticket_form":
            errors.append("win type 'json_rank_first' requires attacker_surface: ticket_form")

        # flag
        if self.flag_mode == "on_impact" and isinstance(w, WinNone):
            errors.append("flag_mode: on_impact needs a win rule other than 'none'")
        if self.flag_mode == "in_data":
            reachable = (
                "FLAG" in used
                or "{{FLAG}}" in seed_dump
                or bool(FLAG_RETURNING_TOOLS & set(self.tools))
            )
            if not reachable:
                errors.append(
                    "flag_mode: in_data but the flag is unreachable "
                    "(no {{FLAG}} in system_prompt/seed and no flag-returning tool)"
                )

        # solution shape
        field = SURFACE_SOLUTION_FIELD[self.attacker_surface]
        if getattr(self.solution, field) is None:
            errors.append(f"attacker_surface '{self.attacker_surface}' needs solution.{field}")
        if self.solution.kb_edit and self.solution.kb_edit.article not in {a.article_id for a in self.seed.kb}:
            errors.append(f"solution.kb_edit.article '{self.solution.kb_edit.article}' not in seed.kb")
        if self.attacker_surface == "plugin_edit":
            mine = {p.plugin_id for p in self.seed.plugins if p.editable}
            if not mine:
                errors.append("attacker_surface 'plugin_edit' needs at least one seed.plugins entry with editable: true")
            if self.solution.plugin and self.solution.plugin.plugin not in mine:
                errors.append(f"solution.plugin.plugin '{self.solution.plugin.plugin}' is not an editable seed plugin")
        if self.solution.ticket:
            t = self.solution.ticket.title.lower()
            if "mouse" not in t:
                errors.append("solution.ticket.title must contain 'mouse'")

        if errors:
            raise ValueError("; ".join(errors))
        return self

    # --- helpers used by the engine -------------------------------------------

    @property
    def owasp_labels(self) -> list[tuple[str, str]]:
        return [(c, OWASP_LLM_2025[c]) for c in self.owasp]

    @property
    def current_user(self) -> str:
        return self.victim.user if self.victim else DIRECT_CURRENT_USER

    @property
    def secrets_resolved(self) -> list[str]:
        """Strings X-Ray must mask: declared secrets ({{FLAG}} expanded) + the flag itself."""
        return self.secrets_for(self.flag)

    def secrets_for(self, flag: str) -> list[str]:
        """Same, for a team's own flag variant (app/flags.py)."""
        out = [s.replace("{{FLAG}}", flag) for s in self.secrets] + [flag]
        return list(dict.fromkeys(s for s in out if s))

    def hint_cost(self, n: int) -> int:
        pct = HINT_COST_PERCENTS[min(n, len(HINT_COST_PERCENTS) - 1)]
        return round(self.points * pct / 100)

    def value_after_hints(self, opened: int) -> int:
        """What a solve is worth after the first `opened` hints were revealed."""
        return max(0, self.points - sum(self.hint_cost(i) for i in range(min(opened, len(self.hints)))))


def _format_validation_error(path: Path, err: ValidationError) -> str:
    lines = []
    for e in err.errors():
        loc = ".".join(str(p) for p in e["loc"]) or "<root>"
        msg = e["msg"].removeprefix("Value error, ")
        lines.append(f"  - {path.name}: {loc}: {msg}")
    return "\n".join(lines)


def load_challenge(path: Path) -> Challenge:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ChallengeConfigError(f"  - {path.name}: expected a YAML mapping")
    try:
        return Challenge.model_validate(raw)
    except ValidationError as e:
        raise ChallengeConfigError(_format_validation_error(path, e)) from None


def load_challenges(directory: Path, providers: set[str] | None = None) -> dict[str, Challenge]:
    """Load and validate every *.yaml in `directory`. Returns {id: Challenge} sorted by track/order."""
    files = sorted(directory.glob("*.yaml"))
    if not files:
        raise ChallengeConfigError(f"no challenge YAML files found in {directory}")

    problems: list[str] = []
    loaded: list[tuple[Path, Challenge]] = []
    for path in files:
        try:
            ch = load_challenge(path)
        except ChallengeConfigError as e:
            problems.append(str(e))
            continue
        except yaml.YAMLError as e:
            problems.append(f"  - {path.name}: invalid YAML: {e}")
            continue
        if ch.id != path.stem:
            problems.append(f"  - {path.name}: id '{ch.id}' must match file name '{path.stem}'")
        if providers is not None and ch.provider and ch.provider not in providers:
            problems.append(f"  - {path.name}: provider '{ch.provider}' not in providers.yaml ({sorted(providers)})")
        loaded.append((path, ch))

    seen_ids: dict[str, str] = {}
    seen_order: dict[tuple[str, int], str] = {}
    seen_flags: dict[str, str] = {}
    for path, ch in loaded:
        for key, store, label in (
            (ch.id, seen_ids, "id"),
            ((ch.track, ch.order), seen_order, "track/order"),
            (ch.flag, seen_flags, "flag"),
        ):
            if key in store:
                problems.append(f"  - {path.name}: duplicate {label} (also in {store[key]})")
            store[key] = path.name

    if problems:
        raise ChallengeConfigError("Invalid challenge config:\n" + "\n".join(problems))

    track_rank = {"direct": 0, "indirect": 1}
    ordered = sorted((ch for _, ch in loaded), key=lambda c: (track_rank[c.track], c.order))
    return {ch.id: ch for ch in ordered}
