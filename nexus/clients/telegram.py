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

from nexus.architect import (
    ArchitectInterview,
    ExistingProjectStub,
    persist_architect_output,
)
from nexus.config import list_available_domains
from nexus.db import repository as repo
from nexus.db.engine import session_scope
from nexus.db.models import Project
from nexus.settings import get_settings
from nexus.specialist import SpecialistAgent, end_session_with_summary
from nexus.workers.timeout import sweep_once

logger = logging.getLogger(__name__)

SWEEP_INTERVAL_SECONDS = 60

# InlineKeyboardButton callback_data prefixes. Telegram caps callback_data at
# 64 bytes; we stay comfortably under by using short prefixes plus a UUID or
# short domain name.
CALLBACK_USE_PROJECT_PREFIX = "use_project:"   # bind chat to <project uuid>
CALLBACK_ARCHITECT_NEW_PREFIX = "architect_new:"  # start interview for <domain>

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
        # One CallbackQueryHandler routes both prefixes; we dispatch by
        # prefix inside on_callback_query.
        callback_pattern = (
            f"^({CALLBACK_USE_PROJECT_PREFIX}|{CALLBACK_ARCHITECT_NEW_PREFIX})"
        )
        self.app.add_handler(
            CallbackQueryHandler(self.on_callback_query, pattern=callback_pattern)
        )
        self.app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_text)
        )

        # Top-level error handler — without this, exceptions raised inside a
        # handler get logged by PTB's internal logger at WARNING, which is
        # easy to miss if root logging is unconfigured. Surface every
        # exception explicitly so silent hangs are diagnosable.
        self.app.add_error_handler(self._on_error)

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
            await reply_chunked(
                update.message,
                "Usage: /architect <domain>. Available: "
                f"{', '.join(list_available_domains())}",
            )
            return
        domain = context.args[0]
        if domain not in list_available_domains():
            await reply_chunked(
                update.message,
                f"Unknown domain '{domain}'. Available: "
                f"{', '.join(list_available_domains())}",
            )
            return

        # If the user already has active projects in this domain, surface
        # them before silently starting a new interview — most "create"
        # intents are actually "resume". Tap an existing project to bind
        # this chat to it, or tap "Create new project" to proceed with
        # the architect (the LLM will pick a name that differs from the
        # existing ones).
        async with session_scope() as session:
            user = await repo.get_or_create_user_by_telegram_id(
                session, telegram_id=update.effective_user.id
            )
            same_domain = [
                p
                for p in await repo.list_projects(session, user.id)
                if p.domain == domain
            ]

        if same_domain:
            keyboard = [
                [
                    InlineKeyboardButton(
                        p.name,
                        callback_data=f"{CALLBACK_USE_PROJECT_PREFIX}{p.id}",
                    )
                ]
                for p in same_domain
            ]
            keyboard.append(
                [
                    InlineKeyboardButton(
                        "➕ Create new project",
                        callback_data=f"{CALLBACK_ARCHITECT_NEW_PREFIX}{domain}",
                    )
                ]
            )
            await update.message.reply_text(
                f"You already have {len(same_domain)} {domain} "
                "project(s). Use one, or start fresh:",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
            return

        # No collision — start the interview directly.
        await self._typing(update)
        opener = await self._start_architect_interview(
            update.effective_chat.id, domain=domain, existing_projects=[]
        )
        await reply_chunked(update.message, opener)

    async def _start_architect_interview(
        self,
        chat_id: int,
        *,
        domain: str,
        existing_projects: list[ExistingProjectStub],
    ) -> str:
        """Spin up an in-memory architect interview for the chat and return
        the opener (so the caller can send it via whichever message surface
        it has — a command's update.message or a callback's edit/send)."""
        state = self._state(chat_id)
        state.architect = ArchitectInterview(
            domain=domain, existing_projects=existing_projects or None
        )
        state.architect_domain = domain
        return await state.architect.kick_off()

    async def _collect_existing_project_stubs(
        self, user_id, domain: str
    ) -> list[ExistingProjectStub]:
        """Returns ExistingProjectStub for every active same-domain project of
        the user — used both for the keyboard list and the architect prompt."""
        async with session_scope() as session:
            return [
                ExistingProjectStub(
                    id=str(p.id),
                    name=p.name,
                    profile=(p.config or {}).get("profile", {}),
                )
                for p in await repo.list_projects(session, user_id)
                if p.domain == domain
            ]

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

        if query.data.startswith(CALLBACK_USE_PROJECT_PREFIX):
            await self._handle_use_project_callback(query)
            return
        if query.data.startswith(CALLBACK_ARCHITECT_NEW_PREFIX):
            await self._handle_architect_new_callback(query)
            return
        # Any other prefix: already answered, nothing to do.

    async def _handle_use_project_callback(self, query) -> None:
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

    async def _handle_architect_new_callback(self, query) -> None:
        domain = query.data[len(CALLBACK_ARCHITECT_NEW_PREFIX):]
        if domain not in list_available_domains():
            if query.message is not None:
                await query.edit_message_text(f"Unknown domain '{domain}'.")
            return
        chat_id = query.message.chat_id if query.message is not None else None
        if chat_id is None:
            return

        async with session_scope() as session:
            user = await repo.get_or_create_user_by_telegram_id(
                session, telegram_id=query.from_user.id
            )
            user_id = user.id
        existing_projects = await self._collect_existing_project_stubs(
            user_id, domain
        )

        opener = await self._start_architect_interview(
            chat_id, domain=domain, existing_projects=existing_projects
        )
        # Replace the keyboard message with the architect's opener so the
        # buttons disappear and the user just sees the next question.
        if query.message is not None:
            await query.edit_message_text(opener)

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

        logger.info(
            "on_text: chat=%s tg_user=%s text=%r architect_in_flight=%s",
            chat_id,
            update.effective_user.id,
            (text[:80] + "…") if len(text) > 80 else text,
            state.architect is not None,
        )

        # 1) Architect mode (if a /architect interview is in flight).
        if state.architect is not None:
            await self._typing(update)
            reply, done = await state.architect.turn(text)
            await reply_chunked(update.message, reply)
            if done:
                # Architect produced one of two outcomes: bind to an existing
                # project, or create a new one. The two cases are mutually
                # exclusive (the prompt forbids emitting both markers).
                if state.architect.use_existing_project_id is not None:
                    await self._finalize_use_existing(
                        update, chat_id, state.architect.use_existing_project_id
                    )
                elif state.architect.proposal is not None:
                    await self._finalize_new_project(
                        update, chat_id, state.architect, state.architect_domain
                    )
                state.architect = None
                state.architect_domain = None
            return

    async def _finalize_use_existing(
        self, update: Update, chat_id: int, raw_project_id: str
    ) -> None:
        """Bind the chat to the existing project the architect identified."""
        try:
            project_uuid = uuid.UUID(raw_project_id)
        except ValueError:
            await reply_chunked(
                update.message,
                "The architect picked an invalid project id — please /architect again.",
            )
            return
        async with session_scope() as session:
            user = await repo.get_or_create_user_by_telegram_id(
                session, telegram_id=update.effective_user.id
            )
            project = await repo.get_project(session, project_uuid)
            # Auth: must belong to this user (defensive; architect was given
            # only this user's projects).
            if project is None or project.user_id != user.id:
                await reply_chunked(
                    update.message,
                    "That project isn't available anymore — try /architect again.",
                )
                return
            await repo.set_active_project_for_chat(
                session, user=user, chat_id=chat_id, project_id=project.id
            )
        await reply_chunked(
            update.message,
            f"Continuing with '{project.name}'. This chat is now bound to it.",
        )

    async def _finalize_new_project(
        self,
        update: Update,
        chat_id: int,
        interview: ArchitectInterview,
        domain: str | None,
    ) -> None:
        """Persist the architect proposal as a brand-new project."""
        proposal = interview.proposal
        assert proposal is not None  # caller guards
        domain = domain or proposal.config.domain
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
        plan_lines = [
            f"  • [{p.horizon}] {p.name} ({len(p.items)} items)" for p in plans
        ]
        await reply_chunked(
            update.message,
            "Saved.\n"
            f"Project: {project.name}\n"
            f"Plans:\n" + "\n".join(plan_lines) + "\n\n"
            "This chat is now bound to your new project. "
            "Say hi to start your first lesson.",
        )

        # 2) Specialist chat.
        logger.info("on_text: entering specialist branch, opening session_scope")
        async with session_scope() as session:
            logger.info("on_text: session_scope open, loading user")
            user = await repo.get_or_create_user_by_telegram_id(
                session, telegram_id=update.effective_user.id
            )
            logger.info(
                "on_text: user loaded id=%s settings_keys=%s",
                user.id,
                sorted((user.settings or {}).keys()),
            )
            project_id = await repo.get_active_project_for_chat(user, chat_id)
            logger.info(
                "on_text: get_active_project_for_chat(%s) -> %s",
                chat_id,
                project_id,
            )
            if project_id is None:
                logger.info("on_text: not bound — sending nudge")
                await reply_chunked(
                    update.message,
                    "This chat isn't bound to a project yet. "
                    "Use /projects then /use <name>, or /architect <domain>.",
                )
                logger.info("on_text: nudge sent, returning")
                return
            logger.info(
                "specialist.handle_message start: chat=%s project=%s",
                chat_id,
                project_id,
            )
            await self._typing(update)
            agent = SpecialistAgent(project_id=project_id)
            reply, _ = await agent.handle_message(session, text)
        logger.info(
            "specialist.handle_message done: chat=%s reply_len=%d",
            chat_id,
            len(reply or ""),
        )
        await reply_chunked(update.message, reply)

    # ------------------------------------------------------------------
    # Error visibility — without this, unhandled handler exceptions log at
    # WARNING through PTB's own logger and are easy to miss when root logging
    # isn't configured.

    async def _on_error(
        self, update: object, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        chat_hint = ""
        if isinstance(update, Update) and update.effective_chat is not None:
            chat_hint = f" chat={update.effective_chat.id}"
        logger.exception(
            "unhandled exception in handler%s: %s", chat_hint, context.error
        )

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
