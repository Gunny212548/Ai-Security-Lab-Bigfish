"""Read models for /scoreboard and /admin (SPEC §8, §11 M6). Pure queries, no HTTP."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, func, select

from app.db import ChallengeView, ChatTurn, HintView, Solve, Submission, Team, VictimRun, utcnow


def utc_naive(dt: datetime) -> datetime:
    """SQLite hands back naive UTC; fresh in-memory rows are aware. Compare them on one footing."""
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt


@dataclass
class Standing:
    rank: int
    team: Team
    score: int = 0
    last_solve: datetime | None = None
    solves: dict[str, Solve] = field(default_factory=dict)  # challenge id -> Solve


def first_bloods(session: Session) -> dict[str, Solve]:
    """Earliest solve per challenge (ties broken by insert order)."""
    out: dict[str, Solve] = {}
    for s in session.exec(select(Solve)):
        best = out.get(s.challenge_id)
        if best is None or (utc_naive(s.solved_at), s.id) < (utc_naive(best.solved_at), best.id):
            out[s.challenge_id] = s
    return out


def standings(session: Session) -> list[Standing]:
    """Ranking: score desc, then whoever reached it first (last solve earlier), then team creation."""
    teams = {t.id: Standing(rank=0, team=t) for t in session.exec(select(Team))}
    for s in session.exec(select(Solve)):
        row = teams.get(s.team_id)
        if row is None:
            continue
        row.solves[s.challenge_id] = s
        row.score += s.points
        at = utc_naive(s.solved_at)
        row.last_solve = max(row.last_solve or at, at)
    ordered = sorted(teams.values(), key=lambda r: (-r.score, r.last_solve or datetime.max,
                                                    utc_naive(r.team.created_at), r.team.id))
    for i, row in enumerate(ordered, 1):
        row.rank = i
    return ordered


def has_activity(session: Session, team_id: int, challenge_id: str) -> bool:
    """Did this team chat with the bot or run the victim on this challenge? (flag-sharing heuristic)"""
    for model in (ChatTurn, VictimRun):
        if session.exec(select(model.id).where(model.team_id == team_id, model.challenge_id == challenge_id)).first():
            return True
    return False


def record_view(session: Session, team_id: int, challenge_id: str) -> None:
    """Remember the first time this team opened the challenge (start of the solve-time clock)."""
    q = select(ChallengeView.id).where(ChallengeView.team_id == team_id, ChallengeView.challenge_id == challenge_id)
    if session.exec(q).first():
        return
    session.add(ChallengeView(team_id=team_id, challenge_id=challenge_id))
    try:
        session.commit()
    except IntegrityError:  # two teammates opened it at the same moment
        session.rollback()


def solve_stats(session: Session, team_id: int, challenge_id: str) -> tuple[int | None, int]:
    """(seconds since the team first opened the challenge, chat messages + victim runs so far)."""
    view = session.exec(select(ChallengeView).where(ChallengeView.team_id == team_id,
                                                    ChallengeView.challenge_id == challenge_id)).first()
    seconds = None
    if view is not None:
        seconds = max(0, int((utc_naive(utcnow()) - utc_naive(view.first_at)).total_seconds()))
    chats = session.exec(select(func.count()).select_from(ChatTurn).where(
        ChatTurn.team_id == team_id, ChatTurn.challenge_id == challenge_id, ChatTurn.role == "user")).one()
    runs = session.exec(select(func.count()).select_from(VictimRun).where(
        VictimRun.team_id == team_id, VictimRun.challenge_id == challenge_id)).one()
    return seconds, int(chats) + int(runs)


@dataclass
class ChallengeStats:
    solves: int = 0
    wrong: int = 0
    hints: int = 0


def challenge_stats(session: Session) -> dict[str, ChallengeStats]:
    stats: dict[str, ChallengeStats] = {}

    def get(cid: str) -> ChallengeStats:
        return stats.setdefault(cid, ChallengeStats())

    for cid, n in session.exec(select(Solve.challenge_id, func.count()).group_by(Solve.challenge_id)):
        get(cid).solves = n
    wrong_q = select(Submission.challenge_id, func.count()).where(Submission.correct == False)  # noqa: E712
    for cid, n in session.exec(wrong_q.group_by(Submission.challenge_id)):
        get(cid).wrong = n
    for cid, n in session.exec(select(HintView.challenge_id, func.count()).group_by(HintView.challenge_id)):
        get(cid).hints = n
    return stats


def recent_submissions(session: Session, limit: int = 200) -> list[tuple[Submission, str]]:
    """Newest first, with the team name."""
    q = (select(Submission, Team.name).join(Team, Team.id == Submission.team_id)
         .order_by(Submission.at.desc(), Submission.id.desc()).limit(limit))
    return list(session.exec(q))
