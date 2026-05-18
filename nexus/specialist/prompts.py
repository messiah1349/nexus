"""Thin wrappers around `nexus.config.render_prompt` for specialist prompts.

The prose itself lives in `nexus/config/prompts/`; this module supplies the
substitution data from runtime objects.
"""

from __future__ import annotations

from nexus.config import render_prompt
from nexus.db.models import Plan, Summary
from nexus.domains.base import DomainConfig


def _render_plans(plans: list[Plan]) -> str:
    if not plans:
        return "(no active plans)"
    blocks = []
    for plan in plans:
        item_lines = []
        for i, item in enumerate(plan.items):
            status = item.get("status", "pending")
            title = item.get("title", "(untitled)")
            item_lines.append(f"  [{i}] [{status}] {title}")
        items_block = "\n".join(item_lines) if item_lines else "  (no items)"
        target = (
            f", target_date={plan.target_date.isoformat()}" if plan.target_date else ""
        )
        blocks.append(
            f"Plan {plan.id} — horizon={plan.horizon}, name={plan.name!r}{target}\n"
            f"{items_block}"
        )
    return "\n\n".join(blocks)


def _render_summaries(summaries: list[Summary]) -> str:
    if not summaries:
        return "(no prior sessions)"
    blocks = []
    for s in summaries:
        period = (
            s.period_end.isoformat() if s.period_end else s.created_at.isoformat()
        )
        tags = ", ".join(s.focus_tags) if s.focus_tags else "—"
        blocks.append(f"[{period}] tags={tags}\n{s.content}")
    return "\n\n".join(blocks)


def build_specialist_system_prompt(
    *,
    config: DomainConfig,
    plans: list[Plan],
    summaries: list[Summary],
) -> str:
    return render_prompt(
        "specialist_system",
        domain=config.domain,
        prompt_style=config.summary.prompt_style,
        active_plans=_render_plans(plans),
        recent_summaries=_render_summaries(summaries),
    )


def build_summarize_prompt(
    *,
    config: DomainConfig,
    plans: list[Plan],
    transcript: str,
) -> str:
    name = f"summarize_{config.summary.prompt_style}"
    return render_prompt(
        name,
        active_plans_with_ids=_render_plans(plans),
        transcript=transcript,
    )
