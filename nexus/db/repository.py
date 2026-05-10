"""Phase 1 repository helpers — only the minimum set needed to bring up users,
projects, messages, events, and entities. Anything that wants to talk to
postgres should go through these functions; raw SQL stays in this module.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from nexus.db.models import Entity, Event, EventEntity, Message, Project, User


async def create_user(
    session: AsyncSession,
    *,
    display_name: str | None = None,
    telegram_id: int | None = None,
    email: str | None = None,
) -> User:
    user = User(display_name=display_name, telegram_id=telegram_id, email=email)
    session.add(user)
    await session.flush()
    return user


async def get_user_by_telegram_id(
    session: AsyncSession, telegram_id: int
) -> User | None:
    result = await session.execute(select(User).where(User.telegram_id == telegram_id))
    return result.scalar_one_or_none()


async def get_user(session: AsyncSession, user_id: uuid.UUID) -> User | None:
    return await session.get(User, user_id)


async def create_project(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    name: str,
    domain: str,
    config: dict | None = None,
) -> Project:
    project = Project(user_id=user_id, name=name, domain=domain, config=config or {})
    session.add(project)
    await session.flush()
    return project


async def get_project(session: AsyncSession, project_id: uuid.UUID) -> Project | None:
    return await session.get(Project, project_id)


async def list_projects(session: AsyncSession, user_id: uuid.UUID) -> list[Project]:
    result = await session.execute(
        select(Project)
        .where(Project.user_id == user_id, Project.archived_at.is_(None))
        .order_by(Project.created_at.desc())
    )
    return list(result.scalars().all())


async def add_message(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    role: str,
    content: str | None = None,
    meta: dict | None = None,
) -> Message:
    msg = Message(project_id=project_id, role=role, content=content, meta=meta or {})
    session.add(msg)
    await session.flush()
    return msg


async def recent_messages(
    session: AsyncSession, project_id: uuid.UUID, limit: int = 20
) -> list[Message]:
    result = await session.execute(
        select(Message)
        .where(Message.project_id == project_id)
        .order_by(desc(Message.occurred_at))
        .limit(limit)
    )
    return list(result.scalars().all())


async def add_event(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    type: str,
    payload: dict | None = None,
    occurred_at: datetime | None = None,
    source: str = "agent",
    entity_ids: list[uuid.UUID] | None = None,
) -> Event:
    event = Event(
        project_id=project_id,
        type=type,
        payload=payload or {},
        source=source,
    )
    if occurred_at is not None:
        event.occurred_at = occurred_at
    session.add(event)
    await session.flush()

    if entity_ids:
        for eid in entity_ids:
            session.add(EventEntity(event_id=event.id, entity_id=eid))
        await session.flush()

    return event


async def recent_events(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    since: datetime | None = None,
    limit: int = 100,
) -> list[Event]:
    query = select(Event).where(Event.project_id == project_id)
    if since is not None:
        query = query.where(Event.occurred_at >= since)
    query = query.order_by(desc(Event.occurred_at)).limit(limit)
    result = await session.execute(query)
    return list(result.scalars().all())


async def upsert_entity(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    type: str,
    name: str,
    attributes: dict | None = None,
    state: dict | None = None,
) -> Entity:
    """Get-or-create by ``(project_id, type, name)`` and shallow-merge any
    provided ``attributes`` / ``state`` into the existing row.

    Concurrent callers can race here; the unique constraint on
    ``(project_id, type, name)`` will reject duplicate inserts. A future
    revision can switch to ``INSERT ... ON CONFLICT`` once that becomes a
    real concern.
    """
    existing = await get_entity_by_name(session, project_id, type, name)
    if existing is None:
        entity = Entity(
            project_id=project_id,
            type=type,
            name=name,
            attributes=attributes or {},
            state=state or {},
        )
        session.add(entity)
        await session.flush()
        return entity

    if attributes:
        existing.attributes = {**existing.attributes, **attributes}
    if state:
        existing.state = {**existing.state, **state}
    await session.flush()
    return existing


async def get_entity_by_name(
    session: AsyncSession,
    project_id: uuid.UUID,
    type: str,
    name: str,
) -> Entity | None:
    result = await session.execute(
        select(Entity).where(
            Entity.project_id == project_id,
            Entity.type == type,
            Entity.name == name,
        )
    )
    return result.scalar_one_or_none()
