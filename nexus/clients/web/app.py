"""FastAPI app for the Nexus web client.

Two HTML pages (``/login`` and ``/app``) backed by JSON endpoints under
``/api``. Sessions live in a signed cookie via Starlette's
``SessionMiddleware``. The specialist agent itself is unchanged — the
web is a third surface around the same primitives the CLI and Telegram
bot use.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import FileResponse, RedirectResponse, Response  # noqa: F401
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.middleware.sessions import SessionMiddleware

from nexus.clients.web.auth import TelegramAuthError, verify_telegram_widget
from nexus.clients.web.deps import get_current_user, get_db
from nexus.db import repository as repo
from nexus.db.models import User
from nexus.settings import Settings, get_settings
from nexus.specialist import SpecialistAgent, end_session_with_summary

logger = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).parent / "static"


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the FastAPI app. Factory so tests can pass a custom Settings."""
    settings = settings or get_settings()
    app = FastAPI(title="Nexus", docs_url="/api/docs", redoc_url=None)
    app.state.settings = settings
    app.add_middleware(SessionMiddleware, secret_key=settings.web_session_secret)

    # -- HTML pages -----------------------------------------------------

    @app.get("/")
    async def index(request: Request) -> RedirectResponse:
        target = "/app" if request.session.get("user_id") else "/login"
        return RedirectResponse(target)

    @app.get("/login", response_model=None)
    async def login_page(request: Request):
        if request.session.get("user_id"):
            return RedirectResponse("/app")
        return FileResponse(_STATIC_DIR / "auth.html")

    @app.get("/app", response_model=None)
    async def app_page(request: Request):
        if not request.session.get("user_id"):
            return RedirectResponse("/login")
        return FileResponse(_STATIC_DIR / "chat.html")

    # Static assets.
    app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")

    # -- Auth config (read by the frontend at load) ---------------------

    @app.get("/api/auth/config")
    async def auth_config() -> dict:
        return {
            "telegram_bot_username": settings.telegram_bot_username,
            "dev_auth_enabled": settings.web_dev_auth,
        }

    # -- Auth endpoints -------------------------------------------------

    @app.get("/auth/telegram/callback")
    async def telegram_callback(
        request: Request, db: AsyncSession = Depends(get_db)
    ) -> RedirectResponse:
        """Telegram Login Widget redirects here with the signed user payload
        as query parameters. We verify it, get-or-create the user, and
        establish a session."""
        if not settings.telegram_bot_token:
            raise HTTPException(503, "TELEGRAM_BOT_TOKEN not configured")
        params = dict(request.query_params)
        try:
            verified = verify_telegram_widget(settings.telegram_bot_token, params)
        except TelegramAuthError as exc:
            logger.warning("telegram auth failed: %s", exc)
            raise HTTPException(401, f"telegram auth failed: {exc}") from exc
        tg_id = int(verified["id"])
        display_name = (
            f"{verified.get('first_name', '')} {verified.get('last_name', '')}".strip()
            or verified.get("username")
            or None
        )
        user = await repo.get_or_create_user_by_telegram_id(
            db, telegram_id=tg_id, display_name=display_name
        )
        request.session["user_id"] = str(user.id)
        return RedirectResponse("/app", status_code=status.HTTP_302_FOUND)

    @app.post("/auth/dev")
    async def dev_login(
        request: Request, payload: DevLoginPayload, db: AsyncSession = Depends(get_db)
    ) -> dict:
        """Local-testing-only bypass — creates or finds a user by
        telegram_id without Telegram verification."""
        if not settings.web_dev_auth:
            raise HTTPException(404, "dev auth disabled")
        user = await repo.get_or_create_user_by_telegram_id(
            db,
            telegram_id=payload.telegram_id,
            display_name=payload.display_name or "Dev User",
        )
        request.session["user_id"] = str(user.id)
        return {"user_id": str(user.id)}

    @app.post("/auth/logout")
    async def logout(request: Request) -> dict:
        request.session.clear()
        return {"ok": True}

    # -- API: user + projects + messages --------------------------------

    @app.get("/api/me")
    async def me(user: User = Depends(get_current_user)) -> dict:
        return {
            "id": str(user.id),
            "display_name": user.display_name,
            "telegram_id": user.telegram_id,
        }

    @app.get("/api/projects")
    async def list_projects(
        user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db),
    ) -> list[dict]:
        projects = await repo.list_projects(db, user.id)
        return [
            {"id": str(p.id), "name": p.name, "domain": p.domain} for p in projects
        ]

    @app.get("/api/projects/{project_id}/messages")
    async def get_messages(
        project_id: uuid.UUID,
        limit: int = 50,
        user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db),
    ) -> list[dict]:
        project = await repo.get_project(db, project_id)
        if project is None or project.user_id != user.id:
            raise HTTPException(404, "project not found")
        # repo.recent_messages returns newest-first; the UI wants
        # oldest-first so we reverse before serializing.
        recent = await repo.recent_messages(db, project_id, limit=limit)
        recent.reverse()
        return [
            {
                "role": m.role,
                "content": m.content or "",
                "occurred_at": m.occurred_at.isoformat(),
            }
            for m in recent
        ]

    @app.post("/api/projects/{project_id}/messages")
    async def send_message(
        project_id: uuid.UUID,
        payload: SendMessagePayload,
        user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db),
    ) -> dict:
        project = await repo.get_project(db, project_id)
        if project is None or project.user_id != user.id:
            raise HTTPException(404, "project not found")
        agent = SpecialistAgent(project_id=project_id)
        reply, sess = await agent.handle_message(db, payload.text)
        return {"content": reply, "session_id": str(sess.id)}

    @app.post("/api/projects/{project_id}/end")
    async def end_session_endpoint(
        project_id: uuid.UUID,
        user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db),
    ) -> dict:
        project = await repo.get_project(db, project_id)
        if project is None or project.user_id != user.id:
            raise HTTPException(404, "project not found")
        active = await repo.get_active_session(db, project_id)
        if active is None:
            return {"ok": True, "ended": None}
        summary = await end_session_with_summary(
            db, session_id=active.id, reason="explicit"
        )
        return {"ok": True, "ended": str(active.id), "summary": summary.content}

    return app


class DevLoginPayload(BaseModel):
    telegram_id: int
    display_name: str | None = None


class SendMessagePayload(BaseModel):
    text: str


# Module-level ASGI app instance — uvicorn imports `nexus.clients.web.app:app`.
app = create_app()
