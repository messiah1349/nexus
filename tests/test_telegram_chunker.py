"""Unit tests for the Telegram message chunker.

Telegram's per-message hard cap is 4096 characters; the bot was crashing
with `BadRequest: Message is too long` when the specialist's reply
exceeded that (see errors/err1.log). `chunk_for_telegram` splits at the
most natural boundary that fits within the window: paragraph → line →
sentence → word → hard cut.
"""

from __future__ import annotations

from nexus.clients.telegram import MAX_TELEGRAM_MESSAGE_LEN, chunk_for_telegram


def test_empty_returns_empty() -> None:
    assert chunk_for_telegram("") == []


def test_short_returns_single_chunk() -> None:
    assert chunk_for_telegram("hello world") == ["hello world"]


def test_no_split_when_at_limit() -> None:
    text = "a" * MAX_TELEGRAM_MESSAGE_LEN
    chunks = chunk_for_telegram(text)
    assert len(chunks) == 1
    assert len(chunks[0]) == MAX_TELEGRAM_MESSAGE_LEN


def test_every_chunk_is_within_limit() -> None:
    text = "lorem ipsum " * 1000  # ~12000 chars
    chunks = chunk_for_telegram(text)
    assert len(chunks) >= 3
    assert all(len(c) <= MAX_TELEGRAM_MESSAGE_LEN for c in chunks)


def test_round_trips_content() -> None:
    """Concatenating chunks (with a separator) should restore the original
    content modulo whitespace at boundaries."""
    text = (
        "Paragraph one. " * 200
        + "\n\nParagraph two. " * 200
        + "\n\nParagraph three. " * 200
    )
    chunks = chunk_for_telegram(text)
    rejoined = " ".join(chunks)
    # Strip all whitespace and compare — chunker is allowed to trim at edges.
    assert "".join(text.split()) == "".join(rejoined.split())


def test_prefers_paragraph_break() -> None:
    """Given a paragraph break within the window, split there rather than
    mid-sentence."""
    body = "Some sentence here. " * 100  # 2000 chars
    tail = "x" * 3000  # 3000 chars
    text = body + "\n\n" + tail
    # Total ~5002 chars; with max_len=4000 the paragraph break (index ~2000)
    # falls inside the window and should win over sentence/word boundaries.
    chunks = chunk_for_telegram(text, max_len=4000)
    assert len(chunks) == 2
    assert chunks[0].endswith(".")
    assert chunks[1].startswith("x")


def test_falls_back_to_word_boundary() -> None:
    """No paragraph or line breaks → split on a space."""
    text = ("word " * 1500).strip()  # ~7500 chars, only spaces between words
    chunks = chunk_for_telegram(text)
    assert len(chunks) >= 2
    # No chunk should end mid-word; "word" is the whole vocabulary here so
    # every chunk should end in "word".
    for c in chunks:
        assert c.endswith("word")


def test_hard_cut_when_no_boundary() -> None:
    """Pathological input with no whitespace at all — chunker falls back
    to a hard cut at max_len so we still respect Telegram's limit."""
    text = "x" * (MAX_TELEGRAM_MESSAGE_LEN * 2 + 5)
    chunks = chunk_for_telegram(text)
    assert len(chunks) == 3
    assert len(chunks[0]) == MAX_TELEGRAM_MESSAGE_LEN
    assert len(chunks[1]) == MAX_TELEGRAM_MESSAGE_LEN
    assert len(chunks[2]) == 5
