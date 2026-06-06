"""Tests for the MarkdownV2 escape + plain-text fallback in
``nexus.clients.telegram``. Matches the pattern used in
messiah1349/telegram_agent_caller (escape_markdown_v2 + Can't-parse-entities
fallback).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from telegram.constants import ParseMode
from telegram.error import BadRequest

from nexus.clients.telegram import (
    _strip_markdown_formatting,
    escape_markdown_v2,
    reply_chunked,
)


# ---------------------------------------------------------------------------
# escape_markdown_v2
# ---------------------------------------------------------------------------


def test_escape_dot_and_parens() -> None:
    out = escape_markdown_v2("Today we learn 3.5 things (focus on cooking).")
    assert out == "Today we learn 3\\.5 things \\(focus on cooking\\)\\."


def test_escape_preserves_asterisk_for_bold() -> None:
    out = escape_markdown_v2("**hello** world")
    # ** collapses to single * (MarkdownV2 bold); * is NOT escaped.
    assert out == "*hello* world"


def test_escape_underscore() -> None:
    # _ is in the escape set in the reference impl.
    out = escape_markdown_v2("plan_item_index")
    assert out == "plan\\_item\\_index"


def test_escape_heading_becomes_bold_italic() -> None:
    out = escape_markdown_v2("# Today's plan\n")
    # `# Heading\n` → `*_Heading_*\n\n`, then `_` gets escaped.
    # After escape: `*\_Today's plan\_*\n\n`
    assert "*\\_Today's plan\\_*" in out
    # Two trailing newlines because the header replacement appends \n\n
    assert out.endswith("\n\n")


def test_escape_strips_leftover_hash() -> None:
    out = escape_markdown_v2("Not a header — just # in the middle.")
    assert "#" not in out


def test_escape_literal_backslash_n_becomes_real_newline() -> None:
    # LLMs sometimes emit literal `\n` (two chars) instead of an actual newline.
    out = escape_markdown_v2("line one\\nline two")
    assert "\n" in out
    assert "\\n" not in out  # neither the literal two-char form nor the escape


def test_escape_is_idempotent_for_plain_text() -> None:
    # No special chars → identical output.
    out = escape_markdown_v2("hello world")
    assert out == "hello world"


def test_strip_markdown_formatting_removes_markers() -> None:
    assert _strip_markdown_formatting("*bold* _italic_ `code`") == "bold italic code"


# ---------------------------------------------------------------------------
# reply_chunked end-to-end with a mocked message
# ---------------------------------------------------------------------------


async def test_reply_chunked_sends_with_markdown_v2() -> None:
    msg = AsyncMock()
    await reply_chunked(msg, "Hello! This is **bold**.")
    msg.reply_text.assert_awaited_once()
    args, kwargs = msg.reply_text.call_args
    # Asserts on the actual transmitted shape:
    sent_text = args[0]
    assert sent_text == "Hello\\! This is *bold*\\."
    assert kwargs["parse_mode"] == ParseMode.MARKDOWN_V2


async def test_reply_chunked_falls_back_on_parse_error() -> None:
    """When Telegram rejects MarkdownV2 with 'Can't parse entities', we
    retry the same chunk as plain text — formatting markers stripped."""
    msg = AsyncMock()
    parse_error = BadRequest("Can't parse entities: something unbalanced")
    msg.reply_text.side_effect = [parse_error, None]

    await reply_chunked(msg, "**hello** _world_")

    assert msg.reply_text.await_count == 2
    # First attempt: MarkdownV2.
    first_call = msg.reply_text.call_args_list[0]
    assert first_call.kwargs["parse_mode"] == ParseMode.MARKDOWN_V2
    # Second attempt: plain text, formatting markers (* _ `) stripped.
    # The MarkdownV2 escape backslashes around _ etc. remain — that matches
    # the reference impl (https://github.com/messiah1349/telegram_agent_caller
    # /blob/main/bot/client/client.py). Ugly but parses.
    second_call = msg.reply_text.call_args_list[1]
    assert "parse_mode" not in second_call.kwargs
    assert "hello" in second_call.args[0]
    assert "world" in second_call.args[0]
    assert "*" not in second_call.args[0]
    assert "_" not in second_call.args[0]


async def test_reply_chunked_does_not_swallow_other_bad_requests() -> None:
    """A BadRequest that isn't a parse error (e.g. our old 'Message is too
    long' bug) must still propagate — we don't want to silently drop it."""
    msg = AsyncMock()
    msg.reply_text.side_effect = BadRequest("Message is too long")
    with pytest.raises(BadRequest, match="too long"):
        await reply_chunked(msg, "anything")


async def test_reply_chunked_skips_empty() -> None:
    msg = AsyncMock()
    await reply_chunked(msg, "")
    msg.reply_text.assert_not_called()
