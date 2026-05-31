"""Provider-agnostic LLM client interface.

The rest of the codebase imports `ChatMessage`, `LLMClient`, and `get_llm_client`
from this module — never the underlying provider SDKs. Adding a new provider
means writing a new module under `nexus.llm` and wiring it into the factory.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Literal

from pydantic import BaseModel

from nexus.settings import get_settings


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class LLMClient(ABC):
    @abstractmethod
    async def chat(
        self,
        *,
        messages: list[ChatMessage],
        system: str | None = None,
        max_tokens: int = 4096,
        model: str | None = None,
    ) -> ChatMessage:
        """Send a chat request. Returns the assistant message.

        ``system`` is passed separately rather than as a message — providers
        differ on how they want system instructions, and the abstraction
        keeps that translation at the boundary.

        ``model`` overrides the per-client default for one call (useful when
        the same client serves architect + specialist with different sizes).
        """


def get_llm_client() -> LLMClient:
    """Resolve the configured provider into a concrete client."""
    settings = get_settings()
    provider = settings.llm_provider.lower()
    if provider == "anthropic":
        from nexus.llm.anthropic import AnthropicClient

        return AnthropicClient()
    if provider == "gemini":
        from nexus.llm.gemini import GeminiClient

        return GeminiClient()
    raise ValueError(f"unknown LLM provider: {settings.llm_provider!r}")
