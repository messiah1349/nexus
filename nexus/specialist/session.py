"""Session lifecycle for the specialist.

`open_or_resume_session` is the single entry point clients call before
each user turn. It:

1. Looks up the project's active session, if any.
2. If that session is stale (no message activity within
   ``config.sessions.idle_timeout_minutes``), ends it via the summarizer
   and creates a new one.
3. Otherwise resumes it.
4. If no active session existed, creates a fresh one.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from nexus.db import repository as repo
from nexus.db.models import Session
from nexus.domains.base import DomainConfig
from nexus.llm import LLMClient

logger = logging.getLogger(__name__)


class SessionLifecycleError(Exception):
    """Lifecycle precondition violated (missing project, missing config)."""


async def is_session_stale(
    db: AsyncSession, *, sess: Session, idle_timeout_minutes: int
) -> bool:
    """True if the session has gone idle beyond its timeout."""
    last = await repo.last_message_for_session(db, sess.id)
    reference = last.occurred_at if last is not None else sess.started_at
    cutoff = datetime.now(tz=timezone.utc) - timedelta(minutes=idle_timeout_minutes)
    return reference < cutoff


async def open_or_resume_session(
    db: AsyncSession,
    *,
    project_id,
    llm: LLMClient | None = None,
) -> Session:
    """Resolve the active session for a project, ending+summarizing the
    previous one if it timed out.

    The summarizer is imported lazily to avoid a circular import
    (summarizer references session lifecycle for end_reason semantics).
    """
    project = await repo.get_project(db, project_id)
    if project is None:
        raise SessionLifecycleError(f"no project with id {project_id}")
    config = DomainConfig.model_validate(project.config)

    active = await repo.get_active_session(db, project_id)
    if active is not None:
        stale = await is_session_stale(
            db, sess=active, idle_timeout_minutes=config.sessions.idle_timeout_minutes
        )
        if not stale:
            logger.info(
                "open_or_resume_session: resuming fresh session=%s project=%s",
                active.id,
                project_id,
            )
            return active

        # Stale — end + summarize the previous session before opening a new one.
        from nexus.specialist.summarizer import end_session_with_summary

        logger.info(
            "open_or_resume_session: previous session=%s is stale, "
            "running end_session_with_summary (this will call the LLM)",
            active.id,
        )
        await end_session_with_summary(db, session_id=active.id, reason="timeout", llm=llm)
        logger.info(
            "open_or_resume_session: summarizer done for session=%s", active.id
        )

    # Pick a plan to attach the new session to. Prefer the most recent
    # weekly; fall back to whichever active plan exists; fall back to None.
    plans = await repo.get_active_plans(db, project_id)
    plan_id = None
    if plans:
        weekly = [p for p in plans if p.horizon == "weekly"]
        chosen = weekly[-1] if weekly else plans[-1]
        plan_id = chosen.id

    new_sess = await repo.create_session(db, project_id=project_id, plan_id=plan_id)
    logger.info(
        "open_or_resume_session: created session=%s project=%s plan_id=%s",
        new_sess.id,
        project_id,
        plan_id,
    )
    return new_sess
