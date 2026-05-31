from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from nexus.db import repository as repo
from nexus.domains.base import DomainConfig, Profile, SummaryConfig
from nexus.workers.timeout import sweep_stale_sessions
from tests.integration._scripted_llm import ScriptedLLM


def _domain_config_dict(idle_timeout_minutes: int = 30) -> dict:
    cfg = DomainConfig(
        domain="language_learning",
        profile=Profile.model_validate({"language": "spanish"}),
        summary=SummaryConfig(prompt_style="language_learning"),
    ).model_dump()
    cfg["sessions"]["idle_timeout_minutes"] = idle_timeout_minutes
    return cfg


def _summary_block() -> str:
    payload = {
        "content": "Stale session swept.",
        "focus_tags": ["timeout"],
        "plan_item_index_addressed": None,
        "plan_item_update": None,
        "plan_revision": None,
    }
    return f"<<<SUMMARY>>>\n{json.dumps(payload)}\n<<<END_SUMMARY>>>"


async def _make_project(session: AsyncSession, *, idle: int = 30):
    user = await repo.create_user(session, display_name="Sweeper")
    project = await repo.create_project(
        session,
        user_id=user.id,
        name="P",
        domain="language_learning",
        config=_domain_config_dict(idle_timeout_minutes=idle),
    )
    return user, project


async def test_sweeper_ends_stale_leaves_fresh(session: AsyncSession) -> None:
    _, project = await _make_project(session, idle=30)

    fresh = await repo.create_session(session, project_id=project.id)
    await repo.add_message(
        session,
        project_id=project.id,
        session_id=fresh.id,
        role="user",
        content="still here",
    )

    stale = await repo.create_session(session, project_id=project.id)
    msg = await repo.add_message(
        session,
        project_id=project.id,
        session_id=stale.id,
        role="user",
        content="said this hours ago",
    )
    msg.occurred_at = datetime.now(tz=timezone.utc) - timedelta(hours=2)
    await session.flush()

    llm = ScriptedLLM(replies=[_summary_block()])

    ended = await sweep_stale_sessions(session, llm=llm)
    assert ended == [stale.id]

    refreshed_stale = await session.get(type(stale), stale.id)
    refreshed_fresh = await session.get(type(fresh), fresh.id)
    assert refreshed_stale.status == "completed"
    assert refreshed_stale.end_reason == "timeout"
    assert refreshed_fresh.status == "active"

    summaries = await repo.recent_summaries(session, project.id)
    assert len(summaries) == 1
    assert summaries[0].session_id == stale.id


async def test_sweeper_handles_no_active_sessions(session: AsyncSession) -> None:
    ended = await sweep_stale_sessions(session)
    assert ended == []


async def test_sweeper_uses_per_project_idle_timeout(session: AsyncSession) -> None:
    """A session that's stale under one project's config might be fresh
    under another's."""
    _, short_project = await _make_project(session, idle=5)
    _, long_project = await _make_project(session, idle=120)

    # Both projects have a session whose last message is 30 minutes ago.
    for project_id in (short_project.id, long_project.id):
        sess = await repo.create_session(session, project_id=project_id)
        msg = await repo.add_message(
            session,
            project_id=project_id,
            session_id=sess.id,
            role="user",
            content="x",
        )
        msg.occurred_at = datetime.now(tz=timezone.utc) - timedelta(minutes=30)
    await session.flush()

    llm = ScriptedLLM(replies=[_summary_block()])
    ended = await sweep_stale_sessions(session, llm=llm)
    assert len(ended) == 1

    # Long-timeout project's session should still be active.
    long_active = await repo.get_active_session(session, long_project.id)
    short_active = await repo.get_active_session(session, short_project.id)
    assert long_active is not None
    assert short_active is None
