"""Repo helpers added for Phase 4 — Telegram bot's user resolution +
chat-to-project routing."""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from nexus.db import repository as repo


async def test_get_or_create_user_by_telegram_id_creates_when_missing(
    session: AsyncSession,
) -> None:
    tg_id = uuid.uuid4().int & 0x7FFFFFFFFFFFFFFF
    user = await repo.get_or_create_user_by_telegram_id(
        session, telegram_id=tg_id, display_name="Telly"
    )
    assert user.telegram_id == tg_id
    assert user.display_name == "Telly"


async def test_get_or_create_user_by_telegram_id_returns_existing(
    session: AsyncSession,
) -> None:
    tg_id = uuid.uuid4().int & 0x7FFFFFFFFFFFFFFF
    first = await repo.get_or_create_user_by_telegram_id(
        session, telegram_id=tg_id, display_name="A"
    )
    # display_name on subsequent calls is intentionally ignored — first one wins.
    second = await repo.get_or_create_user_by_telegram_id(
        session, telegram_id=tg_id, display_name="B"
    )
    assert second.id == first.id
    assert second.display_name == "A"


async def test_set_and_get_active_project_for_chat(session: AsyncSession) -> None:
    user = await repo.create_user(session, display_name="Maria")
    project = await repo.create_project(
        session, user_id=user.id, name="P", domain="language_learning"
    )
    chat_id = 998877

    # Initially unset
    assert await repo.get_active_project_for_chat(user, chat_id) is None

    await repo.set_active_project_for_chat(
        session, user=user, chat_id=chat_id, project_id=project.id
    )
    assert await repo.get_active_project_for_chat(user, chat_id) == project.id


async def test_active_project_independent_per_chat(session: AsyncSession) -> None:
    user = await repo.create_user(session, display_name="Multi")
    p1 = await repo.create_project(
        session, user_id=user.id, name="P1", domain="language_learning"
    )
    p2 = await repo.create_project(
        session, user_id=user.id, name="P2", domain="language_learning"
    )
    await repo.set_active_project_for_chat(
        session, user=user, chat_id=111, project_id=p1.id
    )
    await repo.set_active_project_for_chat(
        session, user=user, chat_id=222, project_id=p2.id
    )
    assert await repo.get_active_project_for_chat(user, 111) == p1.id
    assert await repo.get_active_project_for_chat(user, 222) == p2.id
    # rebinding chat 111 to p2 should overwrite, not append
    await repo.set_active_project_for_chat(
        session, user=user, chat_id=111, project_id=p2.id
    )
    assert await repo.get_active_project_for_chat(user, 111) == p2.id


async def test_list_active_sessions_returns_only_active(
    session: AsyncSession,
) -> None:
    user = await repo.create_user(session, display_name="Tester")
    project = await repo.create_project(
        session, user_id=user.id, name="P", domain="language_learning"
    )
    active1 = await repo.create_session(session, project_id=project.id)
    active2 = await repo.create_session(session, project_id=project.id)
    closed = await repo.create_session(session, project_id=project.id)
    await repo.end_session(session, session_id=closed.id, reason="explicit")

    listed = await repo.list_active_sessions(session)
    ids = {s.id for s in listed}
    assert active1.id in ids
    assert active2.id in ids
    assert closed.id not in ids
