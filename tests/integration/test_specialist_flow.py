from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from nexus.db import repository as repo
from nexus.domains.base import DomainConfig, Profile, SummaryConfig
from nexus.specialist.agent import SpecialistAgent
from nexus.specialist.session import is_session_stale, open_or_resume_session
from nexus.specialist.summarizer import (
    SummaryParseError,
    end_session_with_summary,
    extract_summary,
)
from tests.integration._scripted_llm import ScriptedLLM


def _domain_config_dict() -> dict:
    return DomainConfig(
        domain="language_learning",
        profile=Profile.model_validate(
            {"language": "spanish", "proficiency_target": "B2"}
        ),
        summary=SummaryConfig(prompt_style="language_learning"),
    ).model_dump()


async def _make_project_with_plan(session: AsyncSession):
    user = await repo.create_user(session, display_name="Tester")
    project = await repo.create_project(
        session,
        user_id=user.id,
        name="Spanish",
        domain="language_learning",
        config=_domain_config_dict(),
    )
    plan = await repo.create_plan(
        session,
        project_id=project.id,
        name="Week of test",
        horizon="weekly",
        items=[
            {"sequence": 1, "title": "Cooking verbs", "status": "pending"},
            {"sequence": 2, "title": "Conditional intro", "status": "pending"},
        ],
    )
    return user, project, plan


def _summary_block(payload: dict) -> str:
    return f"<<<SUMMARY>>>\n{json.dumps(payload)}\n<<<END_SUMMARY>>>"


# ---------------------------------------------------------------------------
# extract_summary
# ---------------------------------------------------------------------------


def test_extract_summary_happy_path() -> None:
    payload = {
        "content": "Maria covered cooking verbs.",
        "focus_tags": ["vocabulary"],
        "plan_item_index_addressed": 0,
        "plan_item_update": None,
        "plan_revision": None,
    }
    summary = extract_summary(_summary_block(payload))
    assert summary.content == "Maria covered cooking verbs."
    assert summary.focus_tags == ["vocabulary"]
    assert summary.plan_item_index_addressed == 0


def test_extract_summary_missing_block() -> None:
    with pytest.raises(SummaryParseError, match="did not contain"):
        extract_summary("just some prose, no marker")


def test_extract_summary_bad_json() -> None:
    with pytest.raises(SummaryParseError, match="did not parse"):
        extract_summary("<<<SUMMARY>>>not json<<<END_SUMMARY>>>")


def test_extract_summary_schema_violation() -> None:
    with pytest.raises(SummaryParseError, match="schema validation"):
        extract_summary(_summary_block({"focus_tags": []}))  # missing 'content'


# ---------------------------------------------------------------------------
# Session lifecycle
# ---------------------------------------------------------------------------


async def test_is_session_stale_with_old_message(session: AsyncSession) -> None:
    _, project, _ = await _make_project_with_plan(session)
    sess = await repo.create_session(session, project_id=project.id)
    msg = await repo.add_message(
        session, project_id=project.id, session_id=sess.id, role="user", content="hi"
    )
    msg.occurred_at = datetime.now(tz=timezone.utc) - timedelta(hours=2)
    await session.flush()

    assert await is_session_stale(session, sess=sess, idle_timeout_minutes=30) is True


async def test_is_session_stale_with_recent_message(session: AsyncSession) -> None:
    _, project, _ = await _make_project_with_plan(session)
    sess = await repo.create_session(session, project_id=project.id)
    await repo.add_message(
        session, project_id=project.id, session_id=sess.id, role="user", content="hi"
    )
    assert await is_session_stale(session, sess=sess, idle_timeout_minutes=30) is False


async def test_open_or_resume_creates_when_none(session: AsyncSession) -> None:
    _, project, plan = await _make_project_with_plan(session)
    llm = ScriptedLLM(replies=[])  # not called
    sess = await open_or_resume_session(session, project_id=project.id, llm=llm)
    assert sess.status == "active"
    assert sess.plan_id == plan.id
    assert sess.plan_item_index is None  # null at creation per design


async def test_open_or_resume_resumes_fresh(session: AsyncSession) -> None:
    _, project, _ = await _make_project_with_plan(session)
    first = await open_or_resume_session(session, project_id=project.id)
    second = await open_or_resume_session(session, project_id=project.id)
    assert first.id == second.id


async def test_open_or_resume_summarizes_stale_then_creates_new(
    session: AsyncSession,
) -> None:
    _, project, plan = await _make_project_with_plan(session)
    stale_sess = await open_or_resume_session(session, project_id=project.id)
    msg = await repo.add_message(
        session,
        project_id=project.id,
        session_id=stale_sess.id,
        role="user",
        content="something",
    )
    msg.occurred_at = datetime.now(tz=timezone.utc) - timedelta(hours=2)
    await session.flush()

    summary_payload = {
        "content": "Brief stale-session summary",
        "focus_tags": ["test"],
        "plan_item_index_addressed": None,
        "plan_item_update": None,
        "plan_revision": None,
    }
    llm = ScriptedLLM(replies=[_summary_block(summary_payload)])

    new_sess = await open_or_resume_session(
        session, project_id=project.id, llm=llm
    )
    assert new_sess.id != stale_sess.id

    # Old session is closed with a summary attached.
    refreshed_old = await session.get(type(stale_sess), stale_sess.id)
    assert refreshed_old.status == "completed"
    assert refreshed_old.end_reason == "timeout"

    summaries = await repo.recent_summaries(session, project.id)
    assert len(summaries) == 1
    assert summaries[0].session_id == stale_sess.id


# ---------------------------------------------------------------------------
# SpecialistAgent.handle_message
# ---------------------------------------------------------------------------


async def test_agent_handle_message_persists_user_and_assistant(
    session: AsyncSession,
) -> None:
    _, project, _ = await _make_project_with_plan(session)
    llm = ScriptedLLM(replies=["Hi, let's start with cooking verbs."])
    agent = SpecialistAgent(project_id=project.id, llm=llm)

    reply, sess = await agent.handle_message(session, "Hola!")
    assert reply == "Hi, let's start with cooking verbs."

    messages = await repo.list_messages_for_session(session, sess.id)
    assert [m.role for m in messages] == ["user", "assistant"]
    assert messages[0].content == "Hola!"
    assert messages[1].content == "Hi, let's start with cooking verbs."


async def test_agent_handle_message_resumes_across_turns(
    session: AsyncSession,
) -> None:
    _, project, _ = await _make_project_with_plan(session)
    llm = ScriptedLLM(replies=["reply 1", "reply 2"])
    agent = SpecialistAgent(project_id=project.id, llm=llm)

    _, sess1 = await agent.handle_message(session, "turn 1")
    _, sess2 = await agent.handle_message(session, "turn 2")

    assert sess1.id == sess2.id

    messages = await repo.list_messages_for_session(session, sess1.id)
    assert [m.role for m in messages] == ["user", "assistant", "user", "assistant"]
    # System prompt is included on each LLM call.
    assert len(llm.calls) == 2
    assert llm.calls[0][0]  # non-empty system
    assert llm.calls[1][0]


# ---------------------------------------------------------------------------
# end_session_with_summary
# ---------------------------------------------------------------------------


async def test_end_session_writes_summary_and_completes(
    session: AsyncSession,
) -> None:
    _, project, plan = await _make_project_with_plan(session)
    agent_llm = ScriptedLLM(replies=["chat reply"])
    agent = SpecialistAgent(project_id=project.id, llm=agent_llm)
    _, sess = await agent.handle_message(session, "hi")

    summary_llm = ScriptedLLM(
        replies=[
            _summary_block(
                {
                    "content": "Maria worked on cooking verbs.",
                    "focus_tags": ["vocabulary"],
                    "plan_item_index_addressed": 0,
                    "plan_item_update": {
                        "plan_id": str(plan.id),
                        "item_index": 0,
                        "status": "completed",
                    },
                    "plan_revision": None,
                }
            )
        ]
    )
    summary_row = await end_session_with_summary(
        session, session_id=sess.id, reason="explicit", llm=summary_llm
    )

    assert summary_row.content.startswith("Maria")
    assert summary_row.session_id == sess.id

    refreshed = await session.get(type(sess), sess.id)
    assert refreshed.status == "completed"
    assert refreshed.end_reason == "explicit"
    assert refreshed.plan_item_index == 0
    assert refreshed.ended_at is not None

    refreshed_plan = await repo.get_plan(session, plan.id)
    assert refreshed_plan.items[0]["status"] == "completed"
    assert refreshed_plan.items[1]["status"] == "pending"


async def test_end_session_applies_plan_revision(session: AsyncSession) -> None:
    _, project, plan = await _make_project_with_plan(session)
    agent_llm = ScriptedLLM(replies=["chat reply"])
    agent = SpecialistAgent(project_id=project.id, llm=agent_llm)
    _, sess = await agent.handle_message(session, "actually let's revise the week")

    summary_llm = ScriptedLLM(
        replies=[
            _summary_block(
                {
                    "content": "User asked to revisit yesterday's verbs.",
                    "focus_tags": ["review"],
                    "plan_item_index_addressed": 0,
                    "plan_item_update": None,
                    "plan_revision": {
                        "plan_id": str(plan.id),
                        "reason": "user-driven schedule shift",
                        "new_plan": {
                            "name": "Week of test (rev 2)",
                            "horizon": "weekly",
                            "items": [
                                {"sequence": 1, "title": "Cooking verbs", "status": "completed"},
                                {"sequence": 2, "title": "Cooking-verb review", "status": "completed"},
                                {"sequence": 3, "title": "Conditional intro", "status": "pending"},
                            ],
                        },
                    },
                }
            )
        ]
    )
    await end_session_with_summary(
        session, session_id=sess.id, reason="explicit", llm=summary_llm
    )

    old_plan = await repo.get_plan(session, plan.id)
    assert old_plan.status == "superseded"
    assert old_plan.superseded_by is not None

    active_plans = await repo.get_active_plans(session, project.id)
    assert len(active_plans) == 1
    assert active_plans[0].id == old_plan.superseded_by
    assert active_plans[0].name == "Week of test (rev 2)"
    assert len(active_plans[0].items) == 3


async def test_end_session_retries_on_malformed_summary(
    session: AsyncSession,
) -> None:
    _, project, _ = await _make_project_with_plan(session)
    agent_llm = ScriptedLLM(replies=["chat reply"])
    agent = SpecialistAgent(project_id=project.id, llm=agent_llm)
    _, sess = await agent.handle_message(session, "hi")

    good_block = _summary_block(
        {
            "content": "ok",
            "focus_tags": [],
            "plan_item_index_addressed": None,
            "plan_item_update": None,
            "plan_revision": None,
        }
    )
    summary_llm = ScriptedLLM(
        replies=[
            "<<<SUMMARY>>>not-json<<<END_SUMMARY>>>",  # first attempt — bad
            good_block,                                  # retry — good
        ]
    )
    summary_row = await end_session_with_summary(
        session, session_id=sess.id, reason="explicit", llm=summary_llm
    )
    assert summary_row.content == "ok"
    assert len(summary_llm.calls) == 2


async def test_end_session_rejects_already_closed(session: AsyncSession) -> None:
    _, project, _ = await _make_project_with_plan(session)
    sess = await repo.create_session(session, project_id=project.id)
    await repo.end_session(session, session_id=sess.id, reason="explicit")

    with pytest.raises(ValueError, match="not active"):
        await end_session_with_summary(
            session, session_id=sess.id, reason="explicit", llm=ScriptedLLM(replies=[])
        )


async def test_end_session_unknown_id(session: AsyncSession) -> None:
    with pytest.raises(ValueError, match="no session"):
        await end_session_with_summary(
            session, session_id=uuid.uuid4(), reason="explicit", llm=ScriptedLLM(replies=[])
        )
