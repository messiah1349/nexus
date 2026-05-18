from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from nexus.db import repository as repo


async def _setup_project(session: AsyncSession) -> tuple:
    user = await repo.create_user(session, display_name="Tester")
    project = await repo.create_project(
        session, user_id=user.id, name="P", domain="language_learning"
    )
    return user, project


async def test_create_plan_with_items(session: AsyncSession) -> None:
    _, project = await _setup_project(session)
    plan = await repo.create_plan(
        session,
        project_id=project.id,
        name="Week of 2026-05-18",
        horizon="weekly",
        items=[
            {"sequence": 1, "title": "Cooking verbs", "status": "pending"},
            {"sequence": 2, "title": "Conditional intro", "status": "pending"},
        ],
        attributes={"focus": "vocab"},
        target_date=date.today() + timedelta(days=7),
    )
    assert plan.id is not None
    assert plan.status == "active"
    assert len(plan.items) == 2
    assert plan.items[0]["title"] == "Cooking verbs"
    assert plan.attributes == {"focus": "vocab"}


async def test_get_active_plans_filters_status_and_horizon(
    session: AsyncSession,
) -> None:
    _, project = await _setup_project(session)
    yearly = await repo.create_plan(
        session, project_id=project.id, name="Year", horizon="yearly"
    )
    weekly = await repo.create_plan(
        session, project_id=project.id, name="Week", horizon="weekly"
    )
    superseded = await repo.create_plan(
        session, project_id=project.id, name="Old", horizon="weekly", status="superseded"
    )

    active_all = await repo.get_active_plans(session, project.id)
    ids = {p.id for p in active_all}
    assert yearly.id in ids
    assert weekly.id in ids
    assert superseded.id not in ids

    active_weekly = await repo.get_active_plans(session, project.id, horizon="weekly")
    assert {p.id for p in active_weekly} == {weekly.id}


async def test_supersede_plan_sets_status_and_pointer(session: AsyncSession) -> None:
    _, project = await _setup_project(session)
    old = await repo.create_plan(
        session, project_id=project.id, name="Old", horizon="weekly"
    )
    new = await repo.create_plan(
        session, project_id=project.id, name="New", horizon="weekly"
    )
    updated_old = await repo.supersede_plan(
        session, old_plan_id=old.id, new_plan=new
    )
    assert updated_old.status == "superseded"
    assert updated_old.superseded_by == new.id


async def test_patch_plan_item_merges_keys(session: AsyncSession) -> None:
    _, project = await _setup_project(session)
    plan = await repo.create_plan(
        session,
        project_id=project.id,
        name="W",
        horizon="weekly",
        items=[
            {"sequence": 1, "title": "Cooking verbs", "status": "pending"},
            {"sequence": 2, "title": "Conditional", "status": "pending"},
        ],
    )
    patched = await repo.patch_plan_item(
        session,
        plan_id=plan.id,
        item_index=0,
        patch={"status": "completed", "completed_at": "2026-05-18"},
    )
    assert patched.items[0]["status"] == "completed"
    assert patched.items[0]["completed_at"] == "2026-05-18"
    # title untouched
    assert patched.items[0]["title"] == "Cooking verbs"
    # other items untouched
    assert patched.items[1]["status"] == "pending"


async def test_patch_plan_item_out_of_range(session: AsyncSession) -> None:
    _, project = await _setup_project(session)
    plan = await repo.create_plan(
        session, project_id=project.id, name="W", horizon="weekly", items=[]
    )
    with pytest.raises(IndexError):
        await repo.patch_plan_item(
            session, plan_id=plan.id, item_index=0, patch={"status": "completed"}
        )


async def test_session_create_and_end(session: AsyncSession) -> None:
    _, project = await _setup_project(session)
    plan = await repo.create_plan(
        session, project_id=project.id, name="W", horizon="weekly"
    )
    sess = await repo.create_session(
        session, project_id=project.id, plan_id=plan.id
    )
    assert sess.status == "active"
    assert sess.plan_item_index is None  # null at creation per design

    active = await repo.get_active_session(session, project.id)
    assert active is not None
    assert active.id == sess.id

    ended = await repo.end_session(
        session, session_id=sess.id, reason="explicit", plan_item_index=0
    )
    assert ended.status == "completed"
    assert ended.end_reason == "explicit"
    assert ended.plan_item_index == 0
    assert ended.ended_at is not None

    # no longer in active query
    still_active = await repo.get_active_session(session, project.id)
    assert still_active is None


async def test_messages_with_session_id(session: AsyncSession) -> None:
    _, project = await _setup_project(session)
    sess = await repo.create_session(session, project_id=project.id)
    await repo.add_message(
        session, project_id=project.id, session_id=sess.id, role="user", content="hi"
    )
    await repo.add_message(
        session,
        project_id=project.id,
        session_id=sess.id,
        role="assistant",
        content="hello",
    )
    msgs = await repo.list_messages_for_session(session, sess.id)
    assert [m.content for m in msgs] == ["hi", "hello"]


async def test_summary_links_to_session(session: AsyncSession) -> None:
    _, project = await _setup_project(session)
    sess = await repo.create_session(session, project_id=project.id)
    summary = await repo.add_summary(
        session,
        project_id=project.id,
        session_id=sess.id,
        scope="session",
        content="Maria covered cooking verbs.",
        focus_tags=["vocab"],
    )
    assert summary.session_id == sess.id

    recents = await repo.recent_summaries(session, project.id)
    assert len(recents) == 1
    assert recents[0].id == summary.id
