"""SQLModel tables (SPEC §7) + engine/session helpers.

Deviation from §7: TeamKB / TeamTicket / TeamEmail also carry `challenge_id`, because D5 and I2
both seed tickets and "Reset challenge" must only touch that challenge's data.
AppFlag is a tiny key/value table for global switches (e.g. indirect track unlock).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import UniqueConstraint, inspect, text
from sqlalchemy.engine import Engine
from sqlmodel import Field, Session, SQLModel, create_engine, select


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Team(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)
    join_code: str = Field(index=True, unique=True)
    created_at: datetime = Field(default_factory=utcnow)


class Solve(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("team_id", "challenge_id", name="uq_solve_team_challenge"),)
    id: int | None = Field(default=None, primary_key=True)
    team_id: int = Field(foreign_key="team.id", index=True)
    challenge_id: str = Field(index=True)
    solved_at: datetime = Field(default_factory=utcnow)
    points: int


class Submission(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    team_id: int = Field(foreign_key="team.id", index=True)
    challenge_id: str = Field(index=True)
    value: str
    correct: bool
    # correct flag submitted with no chat/victim activity by this team on this challenge (SPEC §11 M6:
    # flag-sharing warning). Stored at submit time because "Reset challenge" deletes that activity.
    no_activity: bool = False
    # AI-agent heuristic (correct submissions only): seconds from the team's first view of the challenge page
    # to this submission, and how many chat messages / victim runs it took.
    solve_seconds: int | None = None
    attempts: int = 0
    # anti-cheat (app/flags.py), wrong submissions only: "canary" (the hidden AI-agent flag), "base" (the
    # flag from WRITEUP/YAML, which no team's data contains when per-team flags are on), "team:<name>" (another
    # team's flag variant). Empty otherwise.
    signal: str = ""
    at: datetime = Field(default_factory=utcnow)


class ChallengeView(SQLModel, table=True):
    """First time a team opened a challenge page. Never cleared by "Reset challenge"."""

    __table_args__ = (UniqueConstraint("team_id", "challenge_id", name="uq_view_team_challenge"),)
    id: int | None = Field(default=None, primary_key=True)
    team_id: int = Field(foreign_key="team.id", index=True)
    challenge_id: str = Field(index=True)
    first_at: datetime = Field(default_factory=utcnow)


class ChatTurn(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    team_id: int = Field(foreign_key="team.id", index=True)
    challenge_id: str = Field(index=True)
    role: str  # user | assistant
    content: str
    # "New chat" hides old turns from the player and the LLM but keeps them, so the admin's activity checks
    # (flag-sharing ⚠, attempt counts) still see what the team did.
    archived: bool = False
    at: datetime = Field(default_factory=utcnow)


class HintView(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("team_id", "challenge_id", "hint_index", name="uq_hintview"),)
    id: int | None = Field(default=None, primary_key=True)
    team_id: int = Field(foreign_key="team.id", index=True)
    challenge_id: str = Field(index=True)
    hint_index: int
    at: datetime = Field(default_factory=utcnow)


class TeamKB(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    team_id: int = Field(foreign_key="team.id", index=True)
    challenge_id: str = Field(index=True)
    article_id: str
    title: str
    body: str


class TeamTicket(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    team_id: int = Field(foreign_key="team.id", index=True)
    challenge_id: str = Field(index=True)
    ticket_id: str
    owner: str
    title: str
    description: str
    extra_json: str = "{}"


class TeamEmail(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    team_id: int = Field(foreign_key="team.id", index=True)
    challenge_id: str = Field(index=True)
    email_id: str
    mailbox: str  # helpdesk | attacker
    sender: str
    subject: str
    body: str
    at: datetime = Field(default_factory=utcnow)


class TeamUpload(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    team_id: int = Field(foreign_key="team.id", index=True)
    challenge_id: str = Field(index=True)
    filename: str
    ai_text: str
    human_text: str
    caught_by_human: bool
    hidden_spans: int = 0
    at: datetime = Field(default_factory=utcnow)


class VictimRun(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    team_id: int = Field(foreign_key="team.id", index=True)
    challenge_id: str = Field(index=True)
    output: str
    trace_json: str = "[]"
    won: bool = False
    at: datetime = Field(default_factory=utcnow)


class AppFlag(SQLModel, table=True):
    key: str = Field(primary_key=True)
    value: str


INDIRECT_UNLOCKED = "indirect_unlocked"


# --- engine / session -------------------------------------------------------

_engine: Engine | None = None


def make_engine(database_url: str) -> Engine:
    if database_url.startswith("sqlite:///"):
        db_path = database_url.removeprefix("sqlite:///")
        if db_path and db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    return create_engine(database_url, connect_args={"check_same_thread": False})


def add_missing_columns(engine: Engine) -> list[str]:
    """create_all() never alters existing tables. Add columns that newer models have and an older DB file
    lacks (SQLite ADD COLUMN keeps the data). Only for columns with a scalar default or that are nullable."""
    added: list[str] = []
    insp = inspect(engine)
    with engine.begin() as conn:
        for table in SQLModel.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            existing = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in existing:
                    continue
                default = col.default.arg if col.default is not None and col.default.is_scalar else None
                if default is None and not col.nullable:
                    raise RuntimeError(f"cannot add {table.name}.{col.name}: NOT NULL without a default; recreate the DB")
                ddl = f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {col.type.compile(engine.dialect)}'
                if default is not None:
                    literal = int(default) if isinstance(default, bool) else default
                    ddl += f" NOT NULL DEFAULT {literal!r}" if not isinstance(literal, str) else f" NOT NULL DEFAULT '{literal}'"
                conn.execute(text(ddl))
                added.append(f"{table.name}.{col.name}")
    return added


def init_db(database_url: str) -> Engine:
    global _engine
    _engine = make_engine(database_url)
    SQLModel.metadata.create_all(_engine)
    add_missing_columns(_engine)
    return _engine


def get_engine() -> Engine:
    if _engine is None:
        raise RuntimeError("init_db() has not been called")
    return _engine


def get_session():
    """FastAPI dependency."""
    with Session(get_engine()) as session:
        yield session


def get_flag(session: Session, key: str, default: str = "") -> str:
    row = session.exec(select(AppFlag).where(AppFlag.key == key)).first()
    return row.value if row else default


def set_flag(session: Session, key: str, value: str) -> None:
    row = session.get(AppFlag, key)
    if row:
        row.value = value
    else:
        session.add(AppFlag(key=key, value=value))
    session.commit()


def reset_all(session: Session) -> None:
    """Admin "Reset all": delete every team and everything they did, and lock the Indirect track again."""
    for table in reversed(SQLModel.metadata.sorted_tables):  # children before team
        if table.name != AppFlag.__tablename__:
            session.exec(table.delete())
    session.commit()
    set_flag(session, INDIRECT_UNLOCKED, "0")
