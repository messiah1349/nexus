"""Load everything the specialist needs to answer one turn.

Called once per session-open (cheap to call per turn — plans + summaries are
small reads and won't change mid-session in v1, so the result is effectively
stable across a session).
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from nexus.db import repository as repo
from nexus.db.models import Message, Plan, Project, Session, Summary
from nexus.domains.base import DomainConfig


@dataclass
class SessionContext:
    project: Project
    config: DomainConfig
    plans: list[Plan]
    summaries: list[Summary]
    session: Session
    messages: list[Message]


async def build_session_context(
    db: AsyncSession, *, sess: Session, summary_limit: int = 5
) -> SessionContext:
    project = await repo.get_project(db, sess.project_id)
    if project is None:
        raise ValueError(f"no project with id {sess.project_id}")
    config = DomainConfig.model_validate(project.config)
    plans = await repo.get_active_plans(db, sess.project_id)
    summaries = await repo.recent_summaries(
        db, sess.project_id, scope="session", limit=summary_limit
    )
    messages = await repo.list_messages_for_session(db, sess.id)
    return SessionContext(
        project=project,
        config=config,
        plans=plans,
        summaries=summaries,
        session=sess,
        messages=messages,
    )
