import os
import tempfile
from pathlib import Path

# Point the app at a throwaway DB before anything imports app.settings.
_TMP = Path(tempfile.mkdtemp(prefix="bigfishdesk-test-"))
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP / 'test.db').as_posix()}"
# Existing tests assert the base flag from the YAML; tests/test_flags.py switches per-team flags on.
os.environ["PER_TEAM_FLAGS"] = "false"

import pytest  # noqa: E402
import yaml  # noqa: E402

from app.settings import ROOT_DIR  # noqa: E402

CHALLENGES_DIR = ROOT_DIR / "challenges"
# Challenges cut from the workshop (2026-11-05). Still loaded in tests: they exercise engine features
# (input/output filters, email tools, on_impact chat wins) that the active set does not use.
ARCHIVE_DIR = CHALLENGES_DIR / "_archive"
ACTIVE_IDS = ["d5", "d6", "d7", "d8", "d9", "i1", "i2", "i3", "i4"]


@pytest.fixture
def raw_challenge():
    """Factory: load a real challenge YAML as a dict, so tests can mutate one field at a time."""

    def _load(cid: str) -> dict:
        path = CHALLENGES_DIR / f"{cid}.yaml"
        if not path.exists():
            path = ARCHIVE_DIR / f"{cid}.yaml"
        return yaml.safe_load(path.read_text(encoding="utf-8"))

    return _load


@pytest.fixture(scope="session")
def challenges():
    from app.challenges import load_challenges
    from app.settings import get_settings, load_providers

    s = get_settings()
    providers = set(load_providers(s.providers_file, s.default_provider))
    return {**load_challenges(CHALLENGES_DIR, providers), **load_challenges(ARCHIVE_DIR, providers)}


@pytest.fixture
def session(tmp_path):
    from sqlmodel import Session, SQLModel

    from app.db import make_engine

    engine = make_engine(f"sqlite:///{(tmp_path / 'state.db').as_posix()}")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s
    engine.dispose()


@pytest.fixture
def make_team(session, challenges):
    """Create a team with every challenge seeded; returns team id."""
    from app.db import Team
    from app.state import seed_team

    def _make(name: str = "Red") -> int:
        t = Team(name=name, join_code=f"CODE-{name}")
        session.add(t)
        session.commit()
        seed_team(session, t.id, challenges.values())
        return t.id

    return _make


@pytest.fixture
def write_challenges(tmp_path):
    """Write a list of challenge dicts into a temp dir (file name = id) and return the dir."""

    def _write(items: list[dict]) -> Path:
        for i, data in enumerate(items):
            name = data.get("id") or f"bad{i}"
            (tmp_path / f"{name}.yaml").write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
        return tmp_path

    return _write
