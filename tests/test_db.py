import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, select

from app.db import (
    INDIRECT_UNLOCKED,
    ChatTurn,
    Solve,
    Team,
    TeamEmail,
    TeamKB,
    TeamTicket,
    TeamUpload,
    VictimRun,
    get_flag,
    make_engine,
    set_flag,
)


@pytest.fixture
def session(tmp_path):
    engine = make_engine(f"sqlite:///{(tmp_path / 'db.sqlite').as_posix()}")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def test_all_spec_tables_exist():
    expected = {
        "team", "solve", "submission", "chatturn", "hintview", "teamkb",
        "teamticket", "teamemail", "teamupload", "victimrun", "appflag",
    }
    assert expected <= set(SQLModel.metadata.tables)


def test_solve_unique_per_team_challenge(session):
    t = Team(name="Red", join_code="ABC123")
    session.add(t)
    session.commit()
    session.add(Solve(team_id=t.id, challenge_id="d1", points=50))
    session.commit()
    session.add(Solve(team_id=t.id, challenge_id="d1", points=50))
    with pytest.raises(IntegrityError):
        session.commit()


def test_team_name_and_code_unique(session):
    session.add(Team(name="Red", join_code="AAA111"))
    session.commit()
    session.add(Team(name="Red", join_code="BBB222"))
    with pytest.raises(IntegrityError):
        session.commit()


def test_per_team_state_rows_roundtrip(session):
    t = Team(name="Blue", join_code="XYZ789")
    session.add(t)
    session.commit()
    session.add_all([
        TeamKB(team_id=t.id, challenge_id="i1", article_id="kb-pw", title="t", body="b"),
        TeamTicket(team_id=t.id, challenge_id="d5", ticket_id="2001", owner="ceo", title="t", description="d"),
        TeamEmail(team_id=t.id, challenge_id="i4mail", email_id="E1", mailbox="helpdesk", sender="s", subject="x", body="y"),
        TeamUpload(team_id=t.id, challenge_id="i3", filename="a.pdf", ai_text="a", human_text="h", caught_by_human=False),
        VictimRun(team_id=t.id, challenge_id="i1", output="o"),
        ChatTurn(team_id=t.id, challenge_id="d1", role="user", content="hi"),
    ])
    session.commit()
    assert session.exec(select(TeamTicket).where(TeamTicket.team_id == t.id)).one().extra_json == "{}"
    assert session.exec(select(VictimRun)).one().won is False


def test_app_flag_get_set(session):
    assert get_flag(session, INDIRECT_UNLOCKED) == ""
    set_flag(session, INDIRECT_UNLOCKED, "1")
    assert get_flag(session, INDIRECT_UNLOCKED) == "1"
    set_flag(session, INDIRECT_UNLOCKED, "0")
    assert get_flag(session, INDIRECT_UNLOCKED) == "0"


def test_init_db_adds_columns_missing_from_an_old_db_file(tmp_path):
    import sqlite3

    from sqlmodel import Session

    from app.db import TeamUpload, init_db

    path = tmp_path / "old.db"
    con = sqlite3.connect(path)  # teamupload as created before `hidden_spans` existed
    con.execute("CREATE TABLE teamupload (id INTEGER PRIMARY KEY, team_id INTEGER NOT NULL, challenge_id VARCHAR NOT NULL,"
                " filename VARCHAR NOT NULL, ai_text VARCHAR NOT NULL, human_text VARCHAR NOT NULL,"
                " caught_by_human BOOLEAN NOT NULL, at DATETIME NOT NULL)")
    con.execute("INSERT INTO teamupload VALUES (1, 1, 'i3', 'a.pdf', 'ai', 'human', 0, '2026-11-05 00:00:00')")
    con.commit()
    con.close()

    engine = init_db(f"sqlite:///{path.as_posix()}")
    with Session(engine) as s:
        row = s.get(TeamUpload, 1)
    assert row.filename == "a.pdf" and row.hidden_spans == 0  # old row kept, new column defaulted
    engine.dispose()
