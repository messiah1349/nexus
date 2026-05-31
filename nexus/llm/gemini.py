"""Gemini implementation of `LLMClient` (Google `google-genai` SDK).

Translation notes vs Anthropic:
- Gemini calls the assistant role "model" rather than "assistant". The
  translation happens at the boundary of `chat()`; callers continue to
  use "user" / "assistant" exclusively.
- system instruction is passed via `GenerateContentConfig.system_instruction`,
  not as a special message — same shape as Anthropic.
- Gemini supports a separate explicit prompt-cache API (create a cache
  resource, reference it from subsequent calls). We don't wire it up
  here; the v1 win-on-caching was Anthropic's transparent ephemeral
  cache. Add Gemini caching later if/when measured cost requires it.
"""

from __future__ import annotations

from google import genai
from google.genai import types as gtypes

from nexus.llm.base import ChatMessage, LLMClient
from nexus.settings import get_settings


def _to_gemini_role(role: str) -> str:
    # Provider-agnostic uses "assistant"; Gemini wants "model".
    return "model" if role == "assistant" else role


class GeminiClient(LLMClient):
    DEFAULT_MODEL = "gemini-2.5-flash"

    def __init__(self, *, api_key: str | None = None, model: str | None = None) -> None:
        settings = get_settings()
        resolved_key = api_key or settings.gemini_api_key
        if not resolved_key:
            raise ValueError(
                "GEMINI_API_KEY is not set — add it to .env or pass api_key="
            )
        self._client = genai.Client(api_key=resolved_key)
        self._default_model = model or settings.llm_model or self.DEFAULT_MODEL

    async def chat(
        self,
        *,
        messages: list[ChatMessage],
        system: str | None = None,
        max_tokens: int = 4096,
        model: str | None = None,
    ) -> ChatMessage:
        contents = [
            gtypes.Content(
                role=_to_gemini_role(m.role),
                parts=[gtypes.Part.from_text(text=m.content)],
            )
            for m in messages
        ]

        config = gtypes.GenerateContentConfig(
            system_instruction=system if system else None,
            max_output_tokens=max_tokens,
        )

        response = await self._client.aio.models.generate_content(
            model=model or self._default_model,
            contents=contents,
            config=config,
        )

        # `response.text` is the SDK's convenience accessor for the joined
        # text of the first candidate. Defensive fallback below if the SDK
        # ever returns a candidate without text (tool-only response, safety
        # block, etc.).
        text = getattr(response, "text", None)
        if not text:
            candidate = response.candidates[0] if response.candidates else None
            if candidate and candidate.content and candidate.content.parts:
                text = "".join(p.text or "" for p in candidate.content.parts)
            else:
                text = ""
        return ChatMessage(role="assistant", content=text)
