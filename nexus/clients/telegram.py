"""Telegram bot — primary user-facing client in v1.

Commands:
  /start                  — register / greet
  /projects               — list this user's projects
  /use <name>             — bind this chat to a project (by name, case-insensitive) /architect <domain>     — start an architect interview in this chat
  /end                    — end the current session and summarize
  default text            — forwarded to SpecialistAgent.handle_message

Architect interview state is held in memory per chat. A bot restart loses
in-flight interviews; the user just re-runs /architect. Acceptable for MVP.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import desc as sa_desc
from sqlalchemy import select
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from nexus.architect import ArchitectInterview, persist_architect_output
from nexus.config import list_available_domains
from nexus.db import repository as repo
from nexus.db.engine import session_scope
from nexus.db.models import Project
from nexus.settings import get_settings
from nexus.specialist import SpecialistAgent, end_session_with_summary
from nexus.workers.timeout import sweep_once

logger = logging.getLogger(__name__)

SWEEP_INTERVAL_SECONDS = 60

# InlineKeyboardButton callback_data prefix for "bind this chat to a project".
# Format: ``use_project:<uuid>`` — 48 bytes, well under Telegram's 64-byte cap.
CALLBACK_USE_PROJECT_PREFIX = "use_project:"

# Telegram's hard cap on a single text message. Anything longer must be
# split into multiple `send_message` calls.
MAX_TELEGRAM_MESSAGE_LEN = 4096


def chunk_for_telegram(
    text: str, *, max_len: int = MAX_TELEGRAM_MESSAGE_LEN
) -> list[str]:
    """Split `text` into chunks that fit Telegram's per-message limit.

    Prefers, in order: paragraph break (``\\n\\n``) → line break (``\\n``)
    → sentence boundary (``. ``) → word boundary (``" "``) → hard cut.
    Empty input returns an empty list. Each emitted chunk has its leading
    and trailing whitespace trimmed.
    """
    if not text:
        return []
    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= max_len:
            chunks.append(remaining.strip())
            break
        window = remaining[:max_len]
        split_at: int | None = None
        for sep in ("\n\n", "\n", ". ", " "):
            idx = window.rfind(sep)
            if idx > 0:
                split_at = idx + len(sep)
                break
        if split_at is None:
            split_at = max_len
        chunks.append(remaining[:split_at].strip())
        remaining = remaining[split_at:]
    return [c for c in chunks if c]


async def reply_chunked(message, text: str) -> None:
    """Send ``text`` as one or more Telegram messages, splitting if needed.

    ``message`` is a ``telegram.Message`` (typed loosely so unit tests can
    pass a stub).
    """
    for chunk in chunk_for_telegram(text):
        await message.reply_text(chunk)


@dataclass
class _ChatState:
    """Per-chat in-memory state. Reset on bot restart."""

    architect: ArchitectInterview | None = None
    architect_domain: str | None = None


class NexusBot:
    def __init__(self, token: str | None = None) -> None:
        settings = get_settings()
        resolved_token = token or settings.telegram_bot_token
        if not resolved_token:
            raise ValueError(
                "TELEGRAM_BOT_TOKEN is not set — add it to .env or pass token="
            )
        self.app: Application = Application.builder().token(resolved_token).build()
        self._chat_state: dict[int, _ChatState] = {}
        self._register_handlers()

    # ------------------------------------------------------------------
    # Handler registration

    def _register_handlers(self) -> None:
        self.app.add_handler(CommandHandler("start", self.cmd_start))
        self.app.add_handler(CommandHandler("projects", self.cmd_projects))
        self.app.add_handler(CommandHandler("use", self.cmd_use))
        self.app.add_handler(CommandHandler("architect", self.cmd_architect))
        self.app.add_handler(CommandHandler("end", self.cmd_end))
        self.app.add_handler(
            CallbackQueryHandler(
                self.on_callback_query, pattern=f"^{CALLBACK_USE_PROJECT_PREFIX}"
            )
        )
        self.app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_text)
        )

        # Background sweeper for idle-timeout sessions. PTB's JobQueue runs
        # this on the same event loop as the bot.
        if self.app.job_queue is not None:
            self.app.job_queue.run_repeating(
                self._sweep_job,
                interval=SWEEP_INTERVAL_SECONDS,
                first=SWEEP_INTERVAL_SECONDS,
                name="idle_timeout_sweeper",
            )

    # ------------------------------------------------------------------
    # Helpers

    def _state(self, chat_id: int) -> _ChatState:
        return self._chat_state.setdefault(chat_id, _ChatState())

    @staticmethod
    async def _typing(update: Update) -> None:
        if update.effective_chat is None:
            return
        await update.effective_chat.send_action(ChatAction.TYPING)

    # ------------------------------------------------------------------
    # Commands

    async def cmd_start(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if update.effective_user is None or update.message is None:
            return
        tg = update.effective_user
        async with session_scope() as session:
            user = await repo.get_or_create_user_by_telegram_id(
                session,
                telegram_id=tg.id,
                display_name=(tg.full_name or tg.username or None),
            )
        await reply_chunked(update.message, 
            "Welcome to Nexus.\n\n"
            "Next steps:\n"
            "  /architect <domain> — set up your first project\n"
            "  /projects — list your projects\n"
            "  /use <name> — bind this chat to a project\n"
            "\n"
            f"Your user id: {user.id}\n"
            f"Available domains: {', '.join(list_available_domains())}"
        )

    async def cmd_projects(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if update.effective_user is None or update.message is None:
            return
        async with session_scope() as session:
            user = await repo.get_or_create_user_by_telegram_id(
                session, telegram_id=update.effective_user.id
            )
            projects = await repo.list_projects(session, user.id)
        if not projects:
            await reply_chunked(update.message, 
                "No projects yet. Run /architect <domain> to create one."
            )
            return
        # Button text is the project name only — Telegram truncates labels
        # past ~20 characters depending on font/locale, so any additional
        # suffix (e.g. domain in brackets) gets cut off mid-name. The
        # architect prompt caps project_name length at 25 chars to fit.
        keyboard = [
            [
                InlineKeyboardButton(
                    p.name,
                    callback_data=f"{CALLBACK_USE_PROJECT_PREFIX}{p.id}",
                )
            ]
            for p in projects
        ]
        # Keyboard message stays on the direct reply_text path — short text,
        # and reply_chunked doesn't pass keyword args through.
        await update.message.reply_text(
            "Tap a project to bind this chat:",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    async def cmd_use(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if (
            update.effective_user is None
            or update.message is None
            or update.effective_chat is None
        ):
            return
        if not context.args:
            await reply_chunked(update.message, "Usage: /use <project-name>")
            return
        target_name = " ".join(context.args).strip().lower()
        async with session_scope() as session:
            user = await repo.get_or_create_user_by_telegram_id(
                session, telegram_id=update.effective_user.id
            )
            projects = await repo.list_projects(session, user.id)
            matches = [p for p in projects if p.name.lower() == target_name]
            if not matches:
                # Fall back to prefix match for convenience.
                matches = [
                    p for p in projects if p.name.lower().startswith(target_name)
                ]
            if not matches:
                await reply_chunked(update.message, 
                    f"No project matched '{' '.join(context.args)}'. /projects to list."
                )
                return
            if len(matches) > 1:
                await reply_chunked(update.message, 
                    "Ambiguous — multiple projects match. Use the full name."
                )
                return
            project = matches[0]
            await repo.set_active_project_for_chat(
                session,
                user=user,
                chat_id=update.effective_chat.id,
                project_id=project.id,
            )
        await reply_chunked(update.message, f"This chat is now bound to '{project.name}'.")

    async def cmd_architect(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if (
            update.effective_user is None
            or update.message is None
            or update.effective_chat is None
        ):
            return
        if not context.args:
            await reply_chunked(update.message, 
                "Usage: /architect <domain>. Available: "
                f"{', '.join(list_available_domains())}"
            )
            return
        domain = context.args[0]
        if domain not in list_available_domains():
            await reply_chunked(update.message, 
                f"Unknown domain '{domain}'. Available: "
                f"{', '.join(list_available_domains())}"
            )
            return

        # Spin up the interview in memory; it lives until the proposal is
        # persisted or the bot restarts.
        state = self._state(update.effective_chat.id)
        state.architect = ArchitectInterview(domain=domain)
        state.architect_domain = domain
        await self._typing(update)
        opener = await state.architect.kick_off()
        await reply_chunked(update.message, opener)

    async def cmd_end(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if (
            update.effective_user is None
            or update.message is None
            or update.effective_chat is None
        ):
            return
        async with session_scope() as session:
            user = await repo.get_or_create_user_by_telegram_id(
                session, telegram_id=update.effective_user.id
            )
            project_id = await repo.get_active_project_for_chat(
                user, update.effective_chat.id
            )
            if project_id is None:
                await reply_chunked(update.message, 
                    "No active project for this chat. Use /use <name> first."
                )
                return
            active = await repo.get_active_session(session, project_id)
            if active is None:
                await reply_chunked(update.message, "No active session — nothing to end.")
                return
            await self._typing(update)
            summary = await end_session_with_summary(
                session, session_id=active.id, reason="explicit"
            )
        await reply_chunked(update.message, 
            f"Session closed.\n\nSummary:\n{summary.content}"
        )

    # ------------------------------------------------------------------
    # Inline-keyboard callbacks

    async def on_callback_query(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        query = update.callback_query
        if query is None or query.data is None or query.from_user is None:
            return

        # Always answer first so Telegram dismisses the user's loading spinner,
        # even if we then bail on a validation error.
        await query.answer()

        if not query.data.startswith(CALLBACK_USE_PROJECT_PREFIX):
            return
        raw_id = query.data[len(CALLBACK_USE_PROJECT_PREFIX):]
        try:
            project_uuid = uuid.UUID(raw_id)
        except ValueError:
            if query.message is not None:
                await query.edit_message_text("Invalid project id in callback.")
            return

        chat_id = query.message.chat_id if query.message is not None else None
        async with session_scope() as session:
            user = await repo.get_or_create_user_by_telegram_id(
                session, telegram_id=query.from_user.id
            )
            project = await repo.get_project(session, project_uuid)
            # Auth: the tapper must own the project. Prevents a stale or
            # forwarded button from binding someone else's project.
            if project is None or project.user_id != user.id:
                if query.message is not None:
                    await query.edit_message_text(
                        "That project isn't available — it may have been removed."
                    )
                return
            if chat_id is None:
                return
            await repo.set_active_project_for_chat(
                session,
                user=user,
                chat_id=chat_id,
                project_id=project.id,
            )

        if query.message is not None:
            await query.edit_message_text(
                f"Bound this chat to '{project.name}'. Say hi to start."
            )

    # ------------------------------------------------------------------
    # Default text — dispatch to architect or specialist

    async def on_text(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if (
            update.effective_user is None
            or update.message is None
            or update.effective_chat is None
            or update.message.text is None
        ):
            return
        chat_id = update.effective_chat.id
        state = self._state(chat_id)
        text = update.message.text

        # 1) Architect mode (if a /architect interview is in flight).
        if state.architect is not None:
            await self._typing(update)
            reply, done = await state.architect.turn(text)
            await reply_chunked(update.message, reply)
            if done and state.architect.proposal is not None:
                proposal = state.architect.proposal
                domain = state.architect_domain or proposal.config.domain
                async with session_scope() as session:
                    user = await repo.get_or_create_user_by_telegram_id(
                        session, telegram_id=update.effective_user.id
                    )
                    project, plans = await persist_architect_output(
                        session,
                        user_id=user.id,
                        domain=domain,
                        proposal=proposal,
                    )
                    await repo.set_active_project_for_chat(
                        session,
                        user=user,
                        chat_id=chat_id,
                        project_id=project.id,
                    )
                state.architect = None
                state.architect_domain = None
                plan_lines = [
                    f"  • [{p.horizon}] {p.name} ({len(p.items)} items)"
                    for p in plans
                ]
                await reply_chunked(update.message, 
                    "Saved.\n"
                    f"Project: {project.name}\n"
                    f"Plans:\n" + "\n".join(plan_lines) + "\n\n"
                    "This chat is now bound to your new project. "
                    "Say hi to start your first lesson."
                )
            return

        # 2) Specialist chat.
        async with session_scope() as session:
            user = await repo.get_or_create_user_by_telegram_id(
                session, telegram_id=update.effective_user.id
            )
            project_id = await repo.get_active_project_for_chat(user, chat_id)
            if project_id is None:
                await reply_chunked(update.message, 
                    "This chat isn't bound to a project yet. "
                    "Use /projects then /use <name>, or /architect <domain>."
                )
                return
            await self._typing(update)
            agent = SpecialistAgent(project_id=project_id)
            reply, _ = await agent.handle_message(session, text)
        await reply_chunked(update.message, reply)

    # ------------------------------------------------------------------
    # Background sweeper

    async def _sweep_job(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            ended = await sweep_once()
            if ended:
                logger.info("idle-timeout sweep ended %d session(s)", len(ended))
        except Exception:
            logger.exception("idle-timeout sweep failed")

    # ------------------------------------------------------------------
    # Entry point

    def run(self) -> None:
        """Synchronous entry point — PTB's run_polling handles the event
        loop and graceful shutdown."""
        logger.info("starting Nexus bot")
        self.app.run_polling(stop_signals=None)
