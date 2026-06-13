"""Tests for ``markdown_to_telegram_html`` + ``reply_chunked`` behavior.

Targets the subset of HTML Telegram accepts as of Bot API 10.x:
bold/italic/underline/strike, inline & fenced code (with language),
links, blockquote, expandable_blockquote, custom_emoji. Headings have
no Telegram equivalent so they degrade to bold.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from telegram.constants import ParseMode
from telegram.error import BadRequest

from nexus.clients.telegram import markdown_to_telegram_html, reply_chunked


# ---------------------------------------------------------------------------
# markdown_to_telegram_html
# ---------------------------------------------------------------------------


class TestMarkdownToHtml:
    def test_plain_text_passthrough(self) -> None:
        assert markdown_to_telegram_html("just words") == "just words"

    def test_html_specials_are_escaped(self) -> None:
        assert (
            markdown_to_telegram_html("a & b <not a tag>")
            == "a &amp; b &lt;not a tag&gt;"
        )

    def test_bold(self) -> None:
        assert markdown_to_telegram_html("**hi**") == "<b>hi</b>"

    def test_italic_asterisk(self) -> None:
        assert markdown_to_telegram_html("a *word* b") == "a <i>word</i> b"

    def test_italic_underscore(self) -> None:
        assert markdown_to_telegram_html("a _word_ b") == "a <i>word</i> b"

    def test_strike(self) -> None:
        assert markdown_to_telegram_html("~~gone~~") == "<s>gone</s>"

    def test_inline_code(self) -> None:
        assert markdown_to_telegram_html("use `x.y`") == "use <code>x.y</code>"

    def test_inline_code_escapes_html(self) -> None:
        # The content inside code is HTML-escaped so a literal `<` in user
        # code doesn't break parsing.
        assert (
            markdown_to_telegram_html("`<script>`")
            == "<code>&lt;script&gt;</code>"
        )

    def test_fenced_code_with_language(self) -> None:
        out = markdown_to_telegram_html("```python\nprint('hi')\n```")
        assert out == (
            '<pre><code class="language-python">'
            "print(&#x27;hi&#x27;)"
            "</code></pre>"
        )

    def test_fenced_code_without_language(self) -> None:
        out = markdown_to_telegram_html("```\nx\n```")
        assert out == "<pre>x</pre>"

    def test_link(self) -> None:
        out = markdown_to_telegram_html("[Tg](https://t.me/x)")
        assert out == '<a href="https://t.me/x">Tg</a>'

    def test_link_text_is_escaped(self) -> None:
        out = markdown_to_telegram_html("[a < b](https://e.com)")
        assert out == '<a href="https://e.com">a &lt; b</a>'

    def test_heading_becomes_bold(self) -> None:
        # No <h1> in Telegram HTML; the LLM uses headings, degrade to bold.
        assert markdown_to_telegram_html("# Title\nbody") == "<b>Title</b>\nbody"

    def test_multiline_blockquote_collapses(self) -> None:
        out = markdown_to_telegram_html(
            "intro\n> first\n> second\nback to normal"
        )
        assert out == (
            "intro\n<blockquote>\nfirst\nsecond\n</blockquote>\nback to normal"
        )

    def test_lone_blockquote_at_eof(self) -> None:
        # Blockquote ending at end of input still gets closed.
        out = markdown_to_telegram_html("> just one")
        assert out == "<blockquote>\njust one\n</blockquote>"

    def test_code_block_protects_special_chars_inside(self) -> None:
        # `*` and `_` inside a code block should NOT become italic markers.
        out = markdown_to_telegram_html("```\n*not italic*\n```")
        assert "<i>" not in out
        assert "*not italic*" in out  # the asterisks stay literal

    def test_mixed_message(self) -> None:
        src = (
            "# Today\n"
            "Did **squats** and a `bench press`.\n\n"
            "> Tomorrow: heavier.\n\n"
            "[plan](https://example.com)"
        )
        out = markdown_to_telegram_html(src)
        assert "<b>Today</b>" in out
        assert "<b>squats</b>" in out
        assert "<code>bench press</code>" in out
        assert "<blockquote>" in out
        assert "Tomorrow: heavier." in out
        assert '<a href="https://example.com">plan</a>' in out


# ---------------------------------------------------------------------------
# reply_chunked behavior
# ---------------------------------------------------------------------------


class TestReplyChunked:
    async def test_sends_with_html_parse_mode(self) -> None:
        msg = AsyncMock()
        await reply_chunked(msg, "**bold**")
        msg.reply_text.assert_awaited_once()
        args, kwargs = msg.reply_text.call_args
        assert args[0] == "<b>bold</b>"
        assert kwargs["parse_mode"] == ParseMode.HTML

    async def test_falls_back_to_plain_on_parse_error(self) -> None:
        msg = AsyncMock()
        # First attempt raises a parse error; second (plain text) succeeds.
        msg.reply_text.side_effect = [
            BadRequest("can't parse entities: unbalanced markup"),
            None,
        ]
        await reply_chunked(msg, "**half-broken")

        assert msg.reply_text.await_count == 2
        # First call: HTML attempt.
        first = msg.reply_text.call_args_list[0]
        assert first.kwargs["parse_mode"] == ParseMode.HTML
        # Second call: ORIGINAL chunk as plain text (not the half-converted HTML).
        second = msg.reply_text.call_args_list[1]
        assert "parse_mode" not in second.kwargs
        assert second.args[0] == "**half-broken"

    async def test_does_not_swallow_other_bad_requests(self) -> None:
        msg = AsyncMock()
        msg.reply_text.side_effect = BadRequest("Message is too long")
        with pytest.raises(BadRequest, match="too long"):
            await reply_chunked(msg, "anything")

    async def test_skips_empty(self) -> None:
        msg = AsyncMock()
        await reply_chunked(msg, "")
        msg.reply_text.assert_not_called()
