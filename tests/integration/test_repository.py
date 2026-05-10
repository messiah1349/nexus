from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from nexus.db import repository as repo


def _unique_telegram_id() -> int:
    # Stable within a single test run, unique across runs against the shared DB.
    return uuid.uuid4().int & 0x7FFFFFFFFFFFFFFF


async def test_create_user_minimal(session: AsyncSession) -> None:
    user = await repo.create_user(session, display_name="Alice")
    assert user.id is not None
    assert user.display_name == "Alice"
    assert user.settings == {}


async def test_create_user_with_telegram_unique(session: AsyncSession) -> None:
    tg = _unique_telegram_id()
    await repo.create_user(session, telegram_id=tg)
    with pytest.raises(IntegrityError):
        await repo.create_user(session, telegram_id=tg)
        await session.flush()


async def test_get_user_by_telegram_id(session: AsyncSession) -> None:
    tg = _unique_telegram_id()
    created = await repo.create_user(session, telegram_id=tg, display_name="Bob")
    fetched = await repo.get_user_by_telegram_id(session, tg)
    assert fetched is not None
    assert fetched.id == created.id


async def test_create_project_for_user(session: AsyncSession) -> None:
    user = await repo.create_user(session, display_name="Carol")
    project = await repo.create_project(
        session, user_id=user.id, name="Spanish B2", domain="language_learning"
    )
    assert project.user_id == user.id
    assert project.config == {}
    assert project.archived_at is None


async def test_list_projects_excludes_archived(session: AsyncSession) -> None:
    user = await repo.create_user(session, display_name="Dan")
    active = await repo.create_project(
        session, user_id=user.id, name="Active", domain="language_learning"
    )
    archived = await repo.create_project(
        session, user_id=user.id, name="Archived", domain="fitness"
    )
    archived.archived_at = datetime.now(timezone.utc)
    await session.flush()

    projects = await repo.list_projects(session, user.id)
    ids = {p.id for p in projects}
    assert active.id in ids
    assert archived.id not in ids


async def test_add_and_recent_messages(session: AsyncSession) -> None:
    user = await repo.create_user(session, display_name="Erin")
    project = await repo.create_project(
        session, user_id=user.id, name="P", domain="language_learning"
    )

    base = datetime.now(timezone.utc) - timedelta(minutes=10)
    for i in range(3):
        msg = await repo.add_message(
            session,
            project_id=project.id,
            role="user" if i % 2 == 0 else "assistant",
            content=f"msg {i}",
        )
        # Override the server default — server now() resolves to txn-start, so
        # without explicit times all three messages would tie and the recent
        # ordering would be non-deterministic.
        msg.occurred_at = base + timedelta(seconds=i)
    await session.flush()

    messages = await repo.recent_messages(session, project.id, limit=10)
    assert len(messages) == 3
    # Newest first.
    assert messages[0].content == "msg 2"
    assert messages[-1].content == "msg 0"


async def test_add_event_and_filter_by_since(session: AsyncSession) -> None:
    user = await repo.create_user(session, display_name="Fay")
    project = await repo.create_project(
        session, user_id=user.id, name="P", domain="fitness"
    )

    old = await repo.add_event(
        session,
        project_id=project.id,
        type="workout_set",
        payload={"reps": 5, "weight_kg": 80},
        occurred_at=datetime.now(timezone.utc) - timedelta(days=10),
    )
    new = await repo.add_event(
        session,
        project_id=project.id,
        type="workout_set",
        payload={"reps": 5, "weight_kg": 82.5},
    )

    cutoff = datetime.now(timezone.utc) - timedelta(days=1)
    recent = await repo.recent_events(session, project.id, since=cutoff)
    ids = {e.id for e in recent}
    assert new.id in ids
    assert old.id not in ids


async def test_add_event_with_entity_links(session: AsyncSession) -> None:
    user = await repo.create_user(session, display_name="Gus")
    project = await repo.create_project(
        session, user_id=user.id, name="P", domain="fitness"
    )
    bench = await repo.upsert_entity(
        session,
        project_id=project.id,
        type="exercise",
        name="Bench Press",
    )
    event = await repo.add_event(
        session,
        project_id=project.id,
        type="workout_set",
        payload={"reps": 5, "weight_kg": 80},
        entity_ids=[bench.id],
    )
    assert event.id is not None
    # Spot-check the join row exists.
    from nexus.db.models import EventEntity

    row = await session.get(EventEntity, {"event_id": event.id, "entity_id": bench.id})
    assert row is not None
    assert row.role == "subject"


async def test_upsert_entity_creates_then_merges(session: AsyncSession) -> None:
    user = await repo.create_user(session, display_name="Hal")
    project = await repo.create_project(
        session, user_id=user.id, name="P", domain="language_learning"
    )

    first = await repo.upsert_entity(
        session,
        project_id=project.id,
        type="vocab_word",
        name="aprender",
        attributes={"translation": "to learn"},
        state={"mastery_level": 0},
    )
    assert first.attributes == {"translation": "to learn"}
    assert first.state == {"mastery_level": 0}

    second = await repo.upsert_entity(
        session,
        project_id=project.id,
        type="vocab_word",
        name="aprender",
        attributes={"part_of_speech": "verb"},
        state={"mastery_level": 2, "last_reviewed_at": "2026-05-10T00:00:00Z"},
    )
    assert second.id == first.id
    assert second.attributes == {"translation": "to learn", "part_of_speech": "verb"}
    assert second.state == {
        "mastery_level": 2,
        "last_reviewed_at": "2026-05-10T00:00:00Z",
    }


async def test_get_entity_by_name_returns_none_for_unknown(session: AsyncSession) -> None:
    user = await repo.create_user(session, display_name="Iris")
    project = await repo.create_project(
        session, user_id=user.id, name="P", domain="language_learning"
    )
    missing = await repo.get_entity_by_name(
        session, project.id, type="vocab_word", name="nonexistent"
    )
    assert missing is None
