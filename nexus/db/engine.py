from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from itertools import count

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from nexus.settings import get_settings

logger = logging.getLogger(__name__)
_scope_counter = count()

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def make_engine(url: str | None = None, *, echo: bool = False) -> AsyncEngine:
    settings = get_settings()
    return create_async_engine(url or settings.postgres_url, echo=echo, future=True)


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = make_engine()
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _sessionmaker


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    sm = get_sessionmaker()
    scope_id = next(_scope_counter)
    logger.debug("session_scope[%d] opening", scope_id)
    t0 = time.monotonic()
    async with sm() as session:
        logger.debug(
            "session_scope[%d] connection acquired in %.3fs",
            scope_id,
            time.monotonic() - t0,
        )
        try:
            yield session
            t_commit = time.monotonic()
            await session.commit()
            logger.debug(
                "session_scope[%d] committed in %.3fs",
                scope_id,
                time.monotonic() - t_commit,
            )
        except Exception:
            await session.rollback()
            logger.debug("session_scope[%d] rolled back", scope_id)
            raise
        finally:
            logger.debug(
                "session_scope[%d] closing (total %.3fs)",
                scope_id,
                time.monotonic() - t0,
            )


async def dispose_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _sessionmaker = None
