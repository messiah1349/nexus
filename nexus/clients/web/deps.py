"""FastAPI dependencies: DB session + current-user guard."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from nexus.db import repository as repo
from nexus.db.engine import session_scope
from nexus.db.models import User


async def get_db() -> AsyncIterator[AsyncSession]:
    """One transactional session per request — same shape as the bot's
    handlers, just dependency-injected so the route signatures stay flat."""
    async with session_scope() as session:
        yield session


async def get_current_user(
    request: Request, db: AsyncSession = Depends(get_db)
) -> User:
    """Loads the User identified by ``request.session['user_id']`` (signed
    cookie session). Raises 401 if no session or the user is gone."""
    raw_id = request.session.get("user_id")
    if not raw_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="not authenticated"
        )
    try:
        user_id = uuid.UUID(raw_id)
    except ValueError as exc:
        request.session.clear()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid session"
        ) from exc

    user = await repo.get_user(db, user_id)
    if user is None:
        request.session.clear()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="user not found"
        )
    return user
