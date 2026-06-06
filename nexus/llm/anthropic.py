"""Anthropic implementation of `LLMClient`.

Enables prompt caching on the system prompt — every turn of an architect
interview or specialist session shares the same system prompt, so caching
cuts cost and latency materially after the first turn.
"""

from __future__ import annotations

import logging
import time

from anthropic import AsyncAnthropic

from nexus.settings import get_settings
from nexus.llm.base import ChatMessage, LLMClient

logger = logging.getLogger(__name__)


class AnthropicClient(LLMClient):
    DEFAULT_MODEL = "claude-sonnet-4-6"

    def __init__(self, *, api_key: str | None = None, model: str | None = None) -> None:
        settings = get_settings()
        resolved_key = api_key or settings.anthropic_api_key
        if not resolved_key:
            raise ValueError(
                "ANTHROPIC_API_KEY is not set — add it to .env or pass api_key="
            )
        self._client = AsyncAnthropic(api_key=resolved_key)
        self._default_model = model or settings.llm_model or self.DEFAULT_MODEL

    async def chat(
        self,
        *,
        messages: list[ChatMessage],
        system: str | None = None,
        max_tokens: int = 4096,
        model: str | None = None,
    ) -> ChatMessage:
        anthropic_messages = [{"role": m.role, "content": m.content} for m in messages]

        # Cache the system prompt so subsequent turns of the same session
        # don't re-bill the static prefix.
        system_param: list[dict] | None = None
        if system:
            system_param = [
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            ]

        m = model or self._default_model
        logger.info(
            "anthropic.chat start: model=%s n_messages=%d system_len=%d",
            m,
            len(anthropic_messages),
            len(system) if system else 0,
        )
        t0 = time.monotonic()
        try:
            response = await self._client.messages.create(
                model=m,
                max_tokens=max_tokens,
                system=system_param if system_param is not None else [],
                messages=anthropic_messages,
            )
        except Exception:
            logger.exception(
                "anthropic.chat failed: model=%s elapsed=%.1fs",
                m,
                time.monotonic() - t0,
            )
            raise
        logger.info(
            "anthropic.chat done: model=%s elapsed=%.1fs", m, time.monotonic() - t0
        )

        # Plain-text response — content is a list of blocks; in our usage there
        # is exactly one TextBlock per response. Concatenate any extras
        # defensively in case the model returns multiple chunks.
        text_parts = [
            block.text for block in response.content if getattr(block, "type", None) == "text"
        ]
        return ChatMessage(role="assistant", content="".join(text_parts))
