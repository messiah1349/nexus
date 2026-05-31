"""Unit tests for the Gemini provider — no network calls.

Covers the translation layer (role mapping, default model fallback,
get_llm_client dispatch). The real `generate_content` call is mocked.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from nexus.llm import ChatMessage
from nexus.llm.gemini import GeminiClient, _to_gemini_role


def test_role_mapping() -> None:
    assert _to_gemini_role("assistant") == "model"
    assert _to_gemini_role("user") == "user"


def test_default_model_falls_back_to_class_constant() -> None:
    with patch.dict("os.environ", {"GEMINI_API_KEY": "x", "LLM_MODEL": ""}, clear=False):
        # LLM_MODEL="" is treated as None by pydantic; we want the class default.
        client = GeminiClient(api_key="x")
        assert client._default_model == GeminiClient.DEFAULT_MODEL


def test_explicit_model_overrides_default() -> None:
    client = GeminiClient(api_key="x", model="gemini-2.5-pro")
    assert client._default_model == "gemini-2.5-pro"


def test_missing_api_key_raises() -> None:
    import os

    # Ensure no GEMINI_API_KEY in env nor settings cache
    with patch.dict(os.environ, {}, clear=True):
        with pytest.raises(ValueError, match="GEMINI_API_KEY"):
            GeminiClient()


async def test_chat_translates_roles_and_returns_assistant_message() -> None:
    """The translation layer: 'assistant' role goes out as 'model'; the
    response's text comes back wrapped in role='assistant'.
    """
    client = GeminiClient(api_key="x")

    captured: dict = {}

    class _FakeResponse:
        text = "hello back"
        candidates = []

    async def fake_generate_content(*, model, contents, config):
        captured["model"] = model
        captured["contents"] = contents
        captured["config"] = config
        return _FakeResponse()

    with patch.object(
        client._client.aio.models,
        "generate_content",
        new=AsyncMock(side_effect=fake_generate_content),
    ):
        out = await client.chat(
            messages=[
                ChatMessage(role="user", content="hi"),
                ChatMessage(role="assistant", content="hello"),
                ChatMessage(role="user", content="how are you"),
            ],
            system="be friendly",
        )

    assert out.role == "assistant"
    assert out.content == "hello back"

    # Roles translated: assistant → model
    roles = [c.role for c in captured["contents"]]
    assert roles == ["user", "model", "user"]
    # System instruction routed through config, not as a message
    assert captured["config"].system_instruction == "be friendly"
    assert captured["model"] == client.DEFAULT_MODEL


def test_get_llm_client_dispatch() -> None:
    import os

    from nexus.llm import get_llm_client

    with patch.dict(
        os.environ,
        {"LLM_PROVIDER": "gemini", "GEMINI_API_KEY": "x", "LLM_MODEL": ""},
        clear=False,
    ):
        # Settings is constructed each call (no module-level cache); safe to read here.
        client = get_llm_client()
        assert type(client).__name__ == "GeminiClient"
