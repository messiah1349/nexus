"""Idle-timeout sweeper.

Periodically ends + summarizes sessions whose last message age has
exceeded the project's ``sessions.idle_timeout_minutes``. Designed to
run in-process alongside the Telegram bot via PTB's JobQueue, but the
core ``sweep_stale_sessions`` function is just an async coroutine and
can be driven from anywhere.
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from nexus.db import repository as repo
from nexus.db.engine import session_scope
from nexus.domains.base import DomainConfig
from nexus.llm import LLMClient
from nexus.specialist.session import is_session_stale
from nexus.specialist.summarizer import end_session_with_summary

logger = logging.getLogger(__name__)


async def sweep_stale_sessions(
    session: AsyncSession, *, llm: LLMClient | None = None
) -> list:
    """Single sweep over all active sessions. Returns the list of session ids
    that were ended."""
    active = await repo.list_active_sessions(session)
    ended: list = []
    for sess in active:
        project = await repo.get_project(session, sess.project_id)
        if project is None:
            continue
        config = DomainConfig.model_validate(project.config)
        if not await is_session_stale(
            session,
            sess=sess,
            idle_timeout_minutes=config.sessions.idle_timeout_minutes,
        ):
            continue
        try:
            await end_session_with_summary(
                session, session_id=sess.id, reason="timeout", llm=llm
            )
            ended.append(sess.id)
        except Exception:
            # Don't let one bad session break the whole sweep.
            logger.exception("failed to end+summarize session %s", sess.id)
    return ended


async def sweep_once() -> list:
    """Convenience wrapper that opens its own session_scope. Suitable for
    PTB JobQueue's ``run_repeating(callback)`` style."""
    async with session_scope() as session:
        return await sweep_stale_sessions(session)
