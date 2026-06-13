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
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from telegram.constants import ParseMode

from nexus.clients.telegram import (
    CALLBACK_ARCHITECT_NEW_PREFIX,
    CALLBACK_USE_PROJECT_PREFIX,
    NexusBot,
)
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


def _make_update_for_architect_command(
    *, telegram_id: int, chat_id: int
) -> SimpleNamespace:
    """Update shape that ``cmd_architect`` reads — needs effective_chat too,
    plus message.reply_text returning an awaitable."""
    message = SimpleNamespace(reply_text=AsyncMock(return_value=None))
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=telegram_id),
        effective_chat=SimpleNamespace(
            id=chat_id, send_action=AsyncMock(return_value=None)
        ),
        message=message,
    )


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


# ---------------------------------------------------------------------------
# /architect collision guard — same-domain duplicate check
# ---------------------------------------------------------------------------


def _patched_interview(opener: str = "What's your goal?"):
    """Patch ArchitectInterview so kick_off doesn't need a real LLM."""
    p = patch("nexus.clients.telegram.ArchitectInterview")
    return p, opener


async def test_architect_no_existing_starts_interview_directly(
    bot: NexusBot, telegram_user_factory
) -> None:
    """With no projects in this domain, /architect kicks straight off."""
    _, tg = await telegram_user_factory(401, "Fresh")
    update = _make_update_for_architect_command(telegram_id=tg, chat_id=11)
    update.message.reply_text.reset_mock()

    patcher, opener = _patched_interview("First question?")
    with patcher as MockAI:
        instance = MockAI.return_value
        instance.kick_off = AsyncMock(return_value=opener)
        instance.proposal = None

        ctx = SimpleNamespace(args=["language_learning"])
        await bot.cmd_architect(update, ctx)

    MockAI.assert_called_once_with(
        domain="language_learning", existing_projects=None
    )
    # reply_chunked now sends with parse_mode=HTML (markdown rendering).
    update.message.reply_text.assert_awaited_with(opener, parse_mode=ParseMode.HTML)


async def test_architect_with_existing_same_domain_shows_keyboard(
    bot: NexusBot, telegram_user_factory
) -> None:
    """With existing same-domain projects, /architect shows the keyboard
    (existing projects + 'Create new') and does NOT start the interview."""
    user_id, tg = await telegram_user_factory(402, "Dup")
    async with session_scope() as session:
        await repo.create_project(
            session, user_id=user_id, name="Spanish", domain="language_learning"
        )
        await repo.create_project(
            session, user_id=user_id, name="French", domain="language_learning"
        )

    update = _make_update_for_architect_command(telegram_id=tg, chat_id=22)
    patcher, _ = _patched_interview()
    with patcher as MockAI:
        ctx = SimpleNamespace(args=["language_learning"])
        await bot.cmd_architect(update, ctx)
        MockAI.assert_not_called()  # interview should NOT start

    update.message.reply_text.assert_awaited_once()
    args, kwargs = update.message.reply_text.call_args
    assert "already have 2" in args[0]
    markup = kwargs["reply_markup"]
    rows = markup.inline_keyboard
    # 2 existing buttons + 1 "Create new" button
    assert len(rows) == 3
    existing_texts = {rows[0][0].text, rows[1][0].text}
    assert existing_texts == {"Spanish", "French"}
    for i in range(2):
        assert rows[i][0].callback_data.startswith(CALLBACK_USE_PROJECT_PREFIX)
    create_btn = rows[2][0]
    assert "Create new" in create_btn.text
    assert create_btn.callback_data == f"{CALLBACK_ARCHITECT_NEW_PREFIX}language_learning"


async def test_architect_ignores_other_domain_projects(
    bot: NexusBot, telegram_user_factory
) -> None:
    """A fitness project doesn't trigger the keyboard for /architect language_learning."""
    user_id, tg = await telegram_user_factory(403, "MixedDomains")
    async with session_scope() as session:
        await repo.create_project(
            session, user_id=user_id, name="Strength", domain="fitness"
        )

    update = _make_update_for_architect_command(telegram_id=tg, chat_id=33)
    patcher, opener = _patched_interview("Welcome.")
    with patcher as MockAI:
        instance = MockAI.return_value
        instance.kick_off = AsyncMock(return_value=opener)
        ctx = SimpleNamespace(args=["language_learning"])
        await bot.cmd_architect(update, ctx)
        MockAI.assert_called_once_with(
            domain="language_learning", existing_projects=None
        )


async def test_architect_new_callback_passes_existing_names(
    bot: NexusBot, telegram_user_factory
) -> None:
    """Tapping 'Create new project' passes the existing names into
    ArchitectInterview so the prompt requires a distinct new name."""
    user_id, tg = await telegram_user_factory(404, "WantsNew")
    async with session_scope() as session:
        await repo.create_project(
            session, user_id=user_id, name="Spanish", domain="language_learning"
        )

    patcher, opener = _patched_interview("Tell me about your goal.")
    with patcher as MockAI:
        instance = MockAI.return_value
        instance.kick_off = AsyncMock(return_value=opener)

        update = _make_update_for_callback(
            telegram_id=tg,
            chat_id=44,
            data=f"{CALLBACK_ARCHITECT_NEW_PREFIX}language_learning",
        )
        await bot.on_callback_query(update, context=SimpleNamespace())

    # The architect was given the existing project as a rich stub
    # (name + profile + id) so it can do semantic similarity in-interview.
    MockAI.assert_called_once()
    kwargs = MockAI.call_args.kwargs
    assert kwargs["domain"] == "language_learning"
    stubs = kwargs["existing_projects"]
    assert len(stubs) == 1
    assert stubs[0].name == "Spanish"
    update.callback_query.answer.assert_awaited_once()
    update.callback_query.edit_message_text.assert_awaited_with(opener)


async def test_architect_new_callback_unknown_domain(bot: NexusBot) -> None:
    update = _make_update_for_callback(
        telegram_id=405,
        chat_id=55,
        data=f"{CALLBACK_ARCHITECT_NEW_PREFIX}bogus_domain",
    )
    await bot.on_callback_query(update, context=SimpleNamespace())
    update.callback_query.answer.assert_awaited_once()
    msg = update.callback_query.edit_message_text.call_args.args[0]
    assert "Unknown domain" in msg


# ---------------------------------------------------------------------------
# Mid-interview semantic match → use-existing dispatch
# ---------------------------------------------------------------------------


def _make_update_for_on_text(
    *, telegram_id: int, chat_id: int, text: str
) -> SimpleNamespace:
    """Update shape that ``on_text`` reads — needs effective_user,
    effective_chat, and message.text plus reply_text."""
    message = SimpleNamespace(
        text=text, reply_text=AsyncMock(return_value=None)
    )
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=telegram_id),
        effective_chat=SimpleNamespace(
            id=chat_id, send_action=AsyncMock(return_value=None)
        ),
        message=message,
    )


async def test_use_existing_dispatch_binds_chat(
    bot: NexusBot, telegram_user_factory
) -> None:
    """When the in-flight architect's `use_existing_project_id` is set
    after a turn, the bot binds the chat to that project instead of
    persisting a new one."""
    user_id, tg = await telegram_user_factory(501, "MatchUser")
    async with session_scope() as session:
        existing = await repo.create_project(
            session, user_id=user_id, name="Spanish", domain="language_learning"
        )
        existing_id = existing.id

    # Pre-stage a fake architect interview that finishes via the
    # use-existing path on the next turn.
    state = bot._state(chat_id=99)
    fake_interview = SimpleNamespace(
        turn=AsyncMock(
            return_value=(
                "Sounds like your Spanish project. Binding now.",
                True,
            )
        ),
        proposal=None,
        use_existing_project_id=str(existing_id),
    )
    state.architect = fake_interview
    state.architect_domain = "language_learning"

    update = _make_update_for_on_text(
        telegram_id=tg, chat_id=99, text="yes use the existing one"
    )
    await bot.on_text(update, context=SimpleNamespace())

    # Two reply_text calls: the architect's chat reply, then the binding confirm.
    assert update.message.reply_text.await_count >= 2
    bind_confirm = update.message.reply_text.call_args_list[-1].args[0]
    assert "Spanish" in bind_confirm
    assert "bound" in bind_confirm.lower()

    # Architect state cleared, chat bound in DB.
    assert bot._state(99).architect is None
    async with session_scope() as session:
        u = await repo.get_user_by_telegram_id(session, tg)
        assert await repo.get_active_project_for_chat(u, 99) == existing_id


async def test_use_existing_with_bogus_uuid_recovers(
    bot: NexusBot, telegram_user_factory
) -> None:
    """If the LLM hands us a use_existing_project_id that isn't a UUID,
    we tell the user to retry and clear the interview state — no crash."""
    _, tg = await telegram_user_factory(502, "BadUuid")
    state = bot._state(chat_id=100)
    fake_interview = SimpleNamespace(
        turn=AsyncMock(return_value=("Locking in.", True)),
        proposal=None,
        use_existing_project_id="not-a-uuid",
    )
    state.architect = fake_interview
    state.architect_domain = "language_learning"

    update = _make_update_for_on_text(
        telegram_id=tg, chat_id=100, text="yes do it"
    )
    await bot.on_text(update, context=SimpleNamespace())

    last_reply = update.message.reply_text.call_args_list[-1].args[0]
    assert "invalid project id" in last_reply.lower()
    assert bot._state(100).architect is None
