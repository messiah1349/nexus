"""Repository helpers — anything that wants to talk to postgres goes through
these functions; raw SQL stays in this module.

Phase 1 added: users, projects, messages, events, entities.
Phase 2 added: plans, sessions, plus session-aware message/summary helpers.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from nexus.db.models import (
    Entity,
    Event,
    EventEntity,
    Message,
    Plan,
    Project,
    Session,
    Summary,
    User,
)


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
    session_id: uuid.UUID | None = None,
) -> Message:
    msg = Message(
        project_id=project_id,
        session_id=session_id,
        role=role,
        content=content,
        meta=meta or {},
    )
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


async def list_messages_for_session(
    session: AsyncSession, session_id: uuid.UUID
) -> list[Message]:
    """Messages in chronological order — used by context builder and summarizer."""
    result = await session.execute(
        select(Message)
        .where(Message.session_id == session_id)
        .order_by(Message.occurred_at)
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


# ---------------------------------------------------------------------------
# Plans (Phase 2)
# ---------------------------------------------------------------------------


async def create_plan(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    name: str,
    horizon: str,
    items: list[dict] | None = None,
    description: str | None = None,
    attributes: dict | None = None,
    target_date: date | None = None,
    status: str = "active",
) -> Plan:
    plan = Plan(
        project_id=project_id,
        name=name,
        horizon=horizon,
        items=items or [],
        description=description,
        attributes=attributes or {},
        target_date=target_date,
        status=status,
    )
    session.add(plan)
    await session.flush()
    return plan


async def get_plan(session: AsyncSession, plan_id: uuid.UUID) -> Plan | None:
    return await session.get(Plan, plan_id)


async def get_active_plans(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    horizon: str | None = None,
) -> list[Plan]:
    query = select(Plan).where(
        Plan.project_id == project_id, Plan.status == "active"
    )
    if horizon is not None:
        query = query.where(Plan.horizon == horizon)
    query = query.order_by(Plan.created_at)
    result = await session.execute(query)
    return list(result.scalars().all())


async def supersede_plan(
    session: AsyncSession,
    *,
    old_plan_id: uuid.UUID,
    new_plan: Plan,
) -> Plan:
    """Mark `old_plan_id` superseded by `new_plan` (which must already be persisted
    and active). Returns the updated old plan.
    """
    old = await session.get(Plan, old_plan_id)
    if old is None:
        raise ValueError(f"no plan with id {old_plan_id}")
    old.status = "superseded"
    old.superseded_by = new_plan.id
    await session.flush()
    return old


async def patch_plan_item(
    session: AsyncSession,
    *,
    plan_id: uuid.UUID,
    item_index: int,
    patch: dict,
) -> Plan:
    """Shallow-merge `patch` into ``plan.items[item_index]`` and persist.

    JSONB columns in SQLAlchemy don't track in-place mutation; the whole list
    is reassigned so the dirty bit fires.
    """
    plan = await session.get(Plan, plan_id)
    if plan is None:
        raise ValueError(f"no plan with id {plan_id}")
    if not 0 <= item_index < len(plan.items):
        raise IndexError(f"item_index {item_index} out of range for plan {plan_id}")
    new_items = list(plan.items)
    new_items[item_index] = {**new_items[item_index], **patch}
    plan.items = new_items
    await session.flush()
    return plan


# ---------------------------------------------------------------------------
# Sessions (Phase 2)
# ---------------------------------------------------------------------------


async def create_session(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    plan_id: uuid.UUID | None = None,
    kind: str = "lesson",
    attributes: dict | None = None,
) -> Session:
    sess = Session(
        project_id=project_id,
        plan_id=plan_id,
        kind=kind,
        attributes=attributes or {},
    )
    session.add(sess)
    await session.flush()
    return sess


async def get_active_session(
    session: AsyncSession, project_id: uuid.UUID
) -> Session | None:
    result = await session.execute(
        select(Session)
        .where(Session.project_id == project_id, Session.status == "active")
        .order_by(desc(Session.started_at))
        .limit(1)
    )
    return result.scalar_one_or_none()


async def end_session(
    session: AsyncSession,
    *,
    session_id: uuid.UUID,
    reason: str,
    plan_item_index: int | None = None,
) -> Session:
    sess = await session.get(Session, session_id)
    if sess is None:
        raise ValueError(f"no session with id {session_id}")
    sess.status = "completed"
    sess.ended_at = datetime.now(timezone.utc)
    sess.end_reason = reason
    if plan_item_index is not None:
        sess.plan_item_index = plan_item_index
    await session.flush()
    return sess


# ---------------------------------------------------------------------------
# Summaries (Phase 2)
# ---------------------------------------------------------------------------


async def add_summary(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    scope: str,
    content: str,
    session_id: uuid.UUID | None = None,
    focus_tags: list[str] | None = None,
    period_start: datetime | None = None,
    period_end: datetime | None = None,
) -> Summary:
    summary = Summary(
        project_id=project_id,
        session_id=session_id,
        scope=scope,
        content=content,
        focus_tags=focus_tags or [],
        period_start=period_start,
        period_end=period_end,
    )
    session.add(summary)
    await session.flush()
    return summary


async def recent_summaries(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    scope: str = "session",
    limit: int = 5,
) -> list[Summary]:
    result = await session.execute(
        select(Summary)
        .where(Summary.project_id == project_id, Summary.scope == scope)
        .order_by(desc(Summary.created_at))
        .limit(limit)
    )
    return list(result.scalars().all())
