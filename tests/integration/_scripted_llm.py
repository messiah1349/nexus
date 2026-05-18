"""Test helper — `LLMClient` stub returning canned replies in order.

Used by both the architect and specialist integration tests so neither hits
the real Anthropic API.
"""

from __future__ import annotations

from nexus.llm import ChatMessage, LLMClient


class ScriptedLLM(LLMClient):
    def __init__(self, replies: list[str]) -> None:
        self._replies = list(replies)
        self.calls: list[tuple[str, list[ChatMessage]]] = []

    async def chat(
        self, *, messages, system=None, max_tokens=4096, model=None
    ) -> ChatMessage:
        self.calls.append((system or "", list(messages)))
        if not self._replies:
            raise AssertionError("scripted LLM ran out of replies")
        return ChatMessage(role="assistant", content=self._replies.pop(0))
