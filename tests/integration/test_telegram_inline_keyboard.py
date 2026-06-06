"""Inline-keyboard project selection.

These tests instantiate ``NexusBot`` against a fake token (no network call —
PTB's ``Application.builder().build()`` is offline) and drive ``cmd_projects``
and ``on_callback_query`` with hand-rolled mocks of the Update / CallbackQuery
surface.

Because the handlers open their own ``session_scope()`` transactions (real
``COMMIT``), these tests bypass the conftest's rollback-per-test fixture:
they set up data in their own ``session_scope`` and clean up in a finally
block. Mixing the fixture's open transaction with another connection from
the pool deadlocks asyncpg.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from nexus.clients.telegram import CALLBACK_USE_PROJECT_PREFIX, NexusBot
from nexus.db import repository as repo
from nexus.db.engine import session_scope


@pytest.fixture
def bot(monkeypatch: pytest.MonkeyPatch) -> NexusBot:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake:token-for-tests")
    return NexusBot()


@pytest_asyncio.fixture
async def telegram_user_factory() -> AsyncIterator:
    """Returns a callable that creates a user with the given telegram_id,
    returning ``(user_id, telegram_id)``. Cleans up every user it created
    at teardown — cascading deletes catch the projects too."""
    created_telegram_ids: list[int] = []

    async def _make(telegram_id: int, display_name: str = "T") -> tuple[uuid.UUID, int]:
        async with session_scope() as session:
            user = await repo.create_user(
                session, telegram_id=telegram_id, display_name=display_name
            )
            created_telegram_ids.append(telegram_id)
            return user.id, telegram_id

    yield _make

    async with session_scope() as session:
        for tg_id in created_telegram_ids:
            user = await repo.get_user_by_telegram_id(session, tg_id)
            if user is not None:
                await session.delete(user)


def _make_update_for_command(*, telegram_id: int) -> SimpleNamespace:
    """Update shape that ``cmd_projects`` reads from: effective_user,
    message.reply_text."""
    message = SimpleNamespace(reply_text=AsyncMock(return_value=None))
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=telegram_id),
        message=message,
    )


def _make_update_for_callback(
    *, telegram_id: int, chat_id: int, data: str
) -> SimpleNamespace:
    """Update shape that ``on_callback_query`` reads from."""
    message = SimpleNamespace(chat_id=chat_id)
    query = SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=telegram_id),
        message=message,
        answer=AsyncMock(return_value=None),
        edit_message_text=AsyncMock(return_value=None),
    )
    return SimpleNamespace(callback_query=query)


# ---------------------------------------------------------------------------
# cmd_projects renders InlineKeyboardMarkup
# ---------------------------------------------------------------------------


async def test_cmd_projects_renders_keyboard_per_project(
    bot: NexusBot, telegram_user_factory
) -> None:
    user_id, tg = await telegram_user_factory(42, "Keyboard")
    async with session_scope() as session:
        p1 = await repo.create_project(
            session, user_id=user_id, name="Spanish", domain="language_learning"
        )
        p2 = await repo.create_project(
            session, user_id=user_id, name="Strength", domain="fitness"
        )
        p1_id, p2_id = p1.id, p2.id

    update = _make_update_for_command(telegram_id=tg)
    await bot.cmd_projects(update, context=SimpleNamespace())

    update.message.reply_text.assert_awaited_once()
    args, kwargs = update.message.reply_text.call_args
    assert args[0] == "Tap a project to bind this chat:"
    markup = kwargs["reply_markup"]
    rows = markup.inline_keyboard
    assert len(rows) == 2  # one button per project, each on its own row
    button_texts = {row[0].text for row in rows}
    button_payloads = {row[0].callback_data for row in rows}
    # Button label is the project name ONLY (no domain suffix) — Telegram
    # truncates labels past ~20 chars and the architect prompt caps
    # project_name at 25, so any suffix would push the label out of view.
    assert button_texts == {"Spanish", "Strength"}
    assert all(p.startswith(CALLBACK_USE_PROJECT_PREFIX) for p in button_payloads)
    assert {p.removeprefix(CALLBACK_USE_PROJECT_PREFIX) for p in button_payloads} == {
        str(p1_id),
        str(p2_id),
    }


async def test_cmd_projects_no_projects(
    bot: NexusBot, telegram_user_factory
) -> None:
    _, tg = await telegram_user_factory(99, "Empty")
    update = _make_update_for_command(telegram_id=tg)
    await bot.cmd_projects(update, context=SimpleNamespace())

    update.message.reply_text.assert_awaited()
    _, kwargs = update.message.reply_text.call_args
    assert "reply_markup" not in kwargs


# ---------------------------------------------------------------------------
# on_callback_query binds and confirms
# ---------------------------------------------------------------------------


async def test_callback_binds_chat_to_project(
    bot: NexusBot, telegram_user_factory
) -> None:
    user_id, tg = await telegram_user_factory(100, "Tapper")
    async with session_scope() as session:
        project = await repo.create_project(
            session, user_id=user_id, name="P", domain="language_learning"
        )
        project_id = project.id

    update = _make_update_for_callback(
        telegram_id=tg,
        chat_id=555,
        data=f"{CALLBACK_USE_PROJECT_PREFIX}{project_id}",
    )
    await bot.on_callback_query(update, context=SimpleNamespace())

    update.callback_query.answer.assert_awaited_once()
    update.callback_query.edit_message_text.assert_awaited_once()
    confirmation = update.callback_query.edit_message_text.call_args.args[0]
    assert "Bound this chat to" in confirmation
    assert "P" in confirmation

    async with session_scope() as session:
        user = await repo.get_user_by_telegram_id(session, tg)
        assert await repo.get_active_project_for_chat(user, 555) == project_id


async def test_callback_rejects_other_users_project(
    bot: NexusBot, telegram_user_factory
) -> None:
    """Auth check — a user tapping a button for a project they don't own
    (e.g. a forwarded message) gets an error, not an unauthorized bind."""
    owner_id, _ = await telegram_user_factory(200, "Owner")
    _, attacker_tg = await telegram_user_factory(201, "Other")
    async with session_scope() as session:
        project = await repo.create_project(
            session, user_id=owner_id, name="Private", domain="language_learning"
        )
        project_id = project.id

    update = _make_update_for_callback(
        telegram_id=attacker_tg,
        chat_id=777,
        data=f"{CALLBACK_USE_PROJECT_PREFIX}{project_id}",
    )
    await bot.on_callback_query(update, context=SimpleNamespace())

    update.callback_query.answer.assert_awaited_once()
    update.callback_query.edit_message_text.assert_awaited_once()
    msg = update.callback_query.edit_message_text.call_args.args[0]
    assert "isn't available" in msg

    # Attacker's chat is NOT bound.
    async with session_scope() as session:
        attacker = await repo.get_user_by_telegram_id(session, attacker_tg)
        assert await repo.get_active_project_for_chat(attacker, 777) is None


async def test_callback_handles_garbage_uuid(bot: NexusBot) -> None:
    update = _make_update_for_callback(
        telegram_id=300, chat_id=1, data=f"{CALLBACK_USE_PROJECT_PREFIX}not-a-uuid"
    )
    await bot.on_callback_query(update, context=SimpleNamespace())
    update.callback_query.answer.assert_awaited_once()
    update.callback_query.edit_message_text.assert_awaited_once()
    assert "Invalid" in update.callback_query.edit_message_text.call_args.args[0]


async def test_callback_ignores_other_prefixes(bot: NexusBot) -> None:
    """A callback for a different action (future-proofing) gets answered
    and silently dropped — no edit, no exception."""
    update = _make_update_for_callback(
        telegram_id=300, chat_id=1, data="some_other_action:42"
    )
    await bot.on_callback_query(update, context=SimpleNamespace())
    update.callback_query.answer.assert_awaited_once()
    update.callback_query.edit_message_text.assert_not_awaited()
