"""Phase 5 — fitness as the second domain.

These tests don't exercise fitness-specific features in code (there are
none); they verify that a fitness-configured project routes through the
same specialist + summarizer machinery and lands the summary via the
``summarize_fitness`` prompt rather than ``summarize_language_learning``.

If the architectural promise of "one Python codebase, per-domain YAML +
prompt files" holds, both tests pass with zero non-config changes.
"""

from __future__ import annotations

import json

from sqlalchemy.ext.asyncio import AsyncSession

from nexus.config import list_available_domains, render_prompt
from nexus.db import repository as repo
from nexus.domains.base import DomainConfig
from nexus.domains.registry import load_domain_default
from nexus.specialist.agent import SpecialistAgent
from nexus.specialist.prompts import build_summarize_prompt
from nexus.specialist.summarizer import end_session_with_summary
from tests.integration._scripted_llm import ScriptedLLM


def test_fitness_domain_is_discoverable() -> None:
    assert "fitness" in list_available_domains()


def test_fitness_default_config_validates() -> None:
    cfg = load_domain_default("fitness")
    assert cfg.domain == "fitness"
    assert cfg.summary.prompt_style == "fitness"
    assert "yearly" in cfg.plan_horizons
    assert "weekly" in cfg.plan_horizons
    # Idle timeout is intentionally longer than language learning so a
    # user mid-set doesn't get prematurely summarized.
    assert cfg.sessions.idle_timeout_minutes >= 30


def test_summarize_fitness_prompt_renders() -> None:
    rendered = render_prompt(
        "summarize_fitness",
        active_plans_with_ids="Plan abc — horizon=weekly, name='Strength block'\n  [0] [pending] Squat 5x5",
        transcript="[user] did squats 5x5 @ 100kg, felt strong\n[assistant] nice — that's a PR rep",
    )
    # The prompt must address fitness-specific guidance, not language stuff.
    assert "fitness session" in rendered
    assert "preserve reported numbers verbatim" in rendered
    assert "vocabulary" not in rendered  # not the language_learning prompt
    # And must contain the structural markers the parser expects.
    assert "<<<SUMMARY>>>" in rendered
    assert "<<<END_SUMMARY>>>" in rendered


def _make_fitness_project_dict() -> dict:
    """A fitness DomainConfig that the architect would have produced."""
    return DomainConfig.model_validate(
        {
            "domain": "fitness",
            "profile": {
                "units": "metric",
                "experience_level": "intermediate",
                "training_focus": "strength",
            },
            "summary": {"prompt_style": "fitness"},
            "plan_horizons": ["yearly", "weekly"],
        }
    ).model_dump()


async def test_fitness_session_end_to_end(session: AsyncSession) -> None:
    """Drive a fitness project through chat + end_session and verify it
    routes through the fitness summarize prompt (which mentions
    'fitness session'), not the language-learning one (which mentions
    'language-learning session')."""

    user = await repo.create_user(session, display_name="Strong")
    project = await repo.create_project(
        session,
        user_id=user.id,
        name="Strength training",
        domain="fitness",
        config=_make_fitness_project_dict(),
    )
    plan = await repo.create_plan(
        session,
        project_id=project.id,
        name="Week 1 strength block",
        horizon="weekly",
        items=[
            {"sequence": 1, "title": "Squat 5x5 @ 100kg", "status": "pending"},
            {"sequence": 2, "title": "Bench 3x8 @ 70kg", "status": "pending"},
        ],
    )

    # Inspect that the per-domain summarize prompt is actually selected.
    cfg = DomainConfig.model_validate(project.config)
    summarize_prompt = build_summarize_prompt(
        config=cfg, plans=[plan], transcript="[user] hi"
    )
    assert "fitness session" in summarize_prompt
    assert "language-learning" not in summarize_prompt

    # Two LLMs: one for the chat turn, one for the end-of-session summary.
    chat_llm = ScriptedLLM(replies=["Let's hit squats today — ready?"])
    agent = SpecialistAgent(project_id=project.id, llm=chat_llm)
    _, sess = await agent.handle_message(
        session, "Going to squat 5x5 today, feeling strong"
    )

    summary_payload = {
        "content": "Squatted 5x5 @ 100kg, felt strong. PR for the rep range.",
        "focus_tags": ["legs", "PR"],
        "plan_item_index_addressed": 0,
        "plan_item_update": {
            "plan_id": str(plan.id),
            "item_index": 0,
            "status": "completed",
        },
        "plan_revision": None,
    }
    summary_llm = ScriptedLLM(
        replies=[f"<<<SUMMARY>>>\n{json.dumps(summary_payload)}\n<<<END_SUMMARY>>>"]
    )

    saved_summary = await end_session_with_summary(
        session, session_id=sess.id, reason="explicit", llm=summary_llm
    )

    # The summarizer LLM saw the fitness-flavoured prompt.
    seen_system = summary_llm.calls[0][0]
    assert "fitness session" in seen_system

    # And the plan-item patch landed.
    refreshed_plan = await repo.get_plan(session, plan.id)
    assert refreshed_plan.items[0]["status"] == "completed"
    assert refreshed_plan.items[1]["status"] == "pending"
    assert "PR" in saved_summary.focus_tags
    assert "100kg" in saved_summary.content


async def test_two_domains_coexist_under_same_user(session: AsyncSession) -> None:
    """A user with both a language_learning and a fitness project should
    be able to operate them independently. This is the core promise of
    'fixed schema, config-driven domains'."""

    from nexus.domains.base import Profile, SummaryConfig

    user = await repo.create_user(session, display_name="Polymath")
    lang_cfg = DomainConfig(
        domain="language_learning",
        profile=Profile.model_validate({"language": "spanish"}),
        summary=SummaryConfig(prompt_style="language_learning"),
    ).model_dump()
    lang_project = await repo.create_project(
        session,
        user_id=user.id,
        name="Spanish",
        domain="language_learning",
        config=lang_cfg,
    )
    fit_project = await repo.create_project(
        session,
        user_id=user.id,
        name="Strength",
        domain="fitness",
        config=_make_fitness_project_dict(),
    )

    projects = await repo.list_projects(session, user.id)
    domains = {p.domain for p in projects}
    assert domains == {"language_learning", "fitness"}
    assert {p.id for p in projects} == {lang_project.id, fit_project.id}
