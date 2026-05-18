"""End-of-session summarizer.

Runs once per session, when the session is being closed (explicit `/end`
or idle timeout). Produces a structured `SessionSummary` and applies any
plan mutations the summarizer chose autonomously.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import date

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from nexus.db import repository as repo
from nexus.db.models import Session, Summary
from nexus.domains.base import DomainConfig, SessionSummary
from nexus.llm import ChatMessage, LLMClient, get_llm_client
from nexus.specialist.prompts import build_summarize_prompt

_SUMMARY_RE = re.compile(r"<<<SUMMARY>>>\s*(.*?)\s*<<<END_SUMMARY>>>", re.DOTALL)


class SummaryParseError(Exception):
    """The summarizer's output didn't contain a parseable summary block."""


def extract_summary(text: str) -> SessionSummary:
    """Parse the `<<<SUMMARY>>>...<<<END_SUMMARY>>>` block. Raises
    `SummaryParseError` on missing block, bad JSON, or schema violation."""
    match = _SUMMARY_RE.search(text)
    if match is None:
        raise SummaryParseError(
            "summarizer response did not contain a <<<SUMMARY>>> block"
        )
    raw = match.group(1).strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SummaryParseError(f"summary JSON did not parse: {exc}") from exc
    try:
        return SessionSummary.model_validate(data)
    except ValidationError as exc:
        raise SummaryParseError(f"summary failed schema validation: {exc}") from exc


def _render_transcript(messages) -> str:
    if not messages:
        return "(empty)"
    return "\n\n".join(
        f"[{m.role}] {m.content or '(no content)'}" for m in messages
    )


async def _ask_llm_for_summary(
    llm: LLMClient, *, system: str, max_attempts: int = 2
) -> SessionSummary:
    """Call the LLM with the summarize system prompt, parse the result, and
    retry once on parse error before giving up.
    """
    history: list[ChatMessage] = [
        ChatMessage(role="user", content="Please summarize the session as instructed.")
    ]
    last_error: Exception | None = None
    for attempt in range(max_attempts):
        reply = await llm.chat(system=system, messages=history)
        try:
            return extract_summary(reply.content)
        except SummaryParseError as exc:
            last_error = exc
            if attempt + 1 >= max_attempts:
                break
            history.append(reply)
            history.append(
                ChatMessage(
                    role="user",
                    content=(
                        f"That didn't parse: {exc}\n"
                        "Resend ONLY the <<<SUMMARY>>>...<<<END_SUMMARY>>> "
                        "block with valid JSON inside. No other text."
                    ),
                )
            )
    raise SummaryParseError(
        f"summarizer LLM did not produce a valid summary after {max_attempts} attempts: {last_error}"
    )


async def _apply_plan_mutations(
    db: AsyncSession, *, project_id: uuid.UUID, summary: SessionSummary
) -> None:
    """Apply any plan_item_update / plan_revision the summarizer requested."""
    if summary.plan_item_update is not None:
        upd = summary.plan_item_update
        try:
            plan_uuid = uuid.UUID(upd.plan_id)
        except ValueError as exc:
            raise SummaryParseError(
                f"plan_item_update.plan_id is not a UUID: {upd.plan_id}"
            ) from exc
        await repo.patch_plan_item(
            db,
            plan_id=plan_uuid,
            item_index=upd.item_index,
            patch={"status": upd.status},
        )

    if summary.plan_revision is not None:
        rev = summary.plan_revision
        try:
            old_plan_uuid = uuid.UUID(rev.plan_id)
        except ValueError as exc:
            raise SummaryParseError(
                f"plan_revision.plan_id is not a UUID: {rev.plan_id}"
            ) from exc
        new_target_date = (
            date.fromisoformat(rev.new_plan.target_date)
            if rev.new_plan.target_date
            else None
        )
        new_plan = await repo.create_plan(
            db,
            project_id=project_id,
            name=rev.new_plan.name,
            description=rev.new_plan.description,
            horizon=rev.new_plan.horizon,
            items=[item.model_dump() for item in rev.new_plan.items],
            attributes=rev.new_plan.attributes,
            target_date=new_target_date,
        )
        await repo.supersede_plan(db, old_plan_id=old_plan_uuid, new_plan=new_plan)


async def end_session_with_summary(
    db: AsyncSession,
    *,
    session_id: uuid.UUID,
    reason: str,
    llm: LLMClient | None = None,
) -> Summary:
    """Close a session: run the summarizer, write the summary row, patch
    plans as needed, then mark the session completed. Returns the new
    `Summary` row.

    Raises `ValueError` if the session doesn't exist or is already closed.
    """
    sess = await db.get(Session, session_id)
    if sess is None:
        raise ValueError(f"no session with id {session_id}")
    if sess.status != "active":
        raise ValueError(
            f"session {session_id} is not active (status={sess.status})"
        )

    project = await repo.get_project(db, sess.project_id)
    if project is None:
        raise ValueError(f"session {session_id} references missing project")
    config = DomainConfig.model_validate(project.config)
    plans = await repo.get_active_plans(db, sess.project_id)
    messages = await repo.list_messages_for_session(db, session_id)

    transcript = _render_transcript(messages)
    system_prompt = build_summarize_prompt(
        config=config, plans=plans, transcript=transcript
    )

    llm = llm or get_llm_client()
    summary = await _ask_llm_for_summary(llm, system=system_prompt)

    # Persist atomically inside the caller's transaction.
    summary_row = await repo.add_summary(
        db,
        project_id=sess.project_id,
        session_id=session_id,
        scope="session",
        content=summary.content,
        focus_tags=summary.focus_tags,
        period_start=sess.started_at,
        period_end=None,  # filled in below from end_session
    )

    await _apply_plan_mutations(db, project_id=sess.project_id, summary=summary)

    ended = await repo.end_session(
        db,
        session_id=session_id,
        reason=reason,
        plan_item_index=summary.plan_item_index_addressed,
    )
    summary_row.period_end = ended.ended_at
    await db.flush()

    return summary_row
