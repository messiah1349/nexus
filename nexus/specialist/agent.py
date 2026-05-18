"""SpecialistAgent — the chat loop a client (CLI / Telegram / future web)
drives one turn at a time. No mid-turn tool calls in v1; the agent just
loads context, persists the user message, calls the LLM, and persists the
assistant message.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from nexus.db import repository as repo
from nexus.db.models import Session
from nexus.llm import ChatMessage, LLMClient, get_llm_client
from nexus.specialist.context import build_session_context
from nexus.specialist.prompts import build_specialist_system_prompt
from nexus.specialist.session import open_or_resume_session


class SpecialistAgent:
    def __init__(self, *, project_id: uuid.UUID, llm: LLMClient | None = None) -> None:
        self.project_id = project_id
        self.llm = llm or get_llm_client()

    async def handle_message(
        self, db: AsyncSession, user_input: str
    ) -> tuple[str, Session]:
        """Process one user turn. Returns ``(assistant_text, session)``."""
        sess = await open_or_resume_session(db, project_id=self.project_id, llm=self.llm)

        await repo.add_message(
            db,
            project_id=self.project_id,
            session_id=sess.id,
            role="user",
            content=user_input,
        )

        ctx = await build_session_context(db, sess=sess)
        system = build_specialist_system_prompt(
            config=ctx.config, plans=ctx.plans, summaries=ctx.summaries
        )

        history = [
            ChatMessage(role=m.role, content=m.content or "")
            for m in ctx.messages
            if m.role in ("user", "assistant") and m.content
        ]

        reply = await self.llm.chat(system=system, messages=history)

        await repo.add_message(
            db,
            project_id=self.project_id,
            session_id=sess.id,
            role="assistant",
            content=reply.content,
        )

        return reply.content, sess
