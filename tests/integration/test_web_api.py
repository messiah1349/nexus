"""End-to-end-ish tests for the FastAPI web client.

Async-native: httpx.AsyncClient + ASGITransport runs the app on the
session-scoped event loop, so DB setup/cleanup is plain `await
session_scope()` — no thread/portal juggling, no asyncpg
"another operation is in progress" conflicts.

The SpecialistAgent's LLM client is patched with a ScriptedLLM so tests
don't depend on a provider being configured.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from unittest.mock import patch

import httpx
import pytest_asyncio
from httpx import ASGITransport

from nexus.clients.web.app import create_app
from nexus.db import repository as repo
from nexus.db.engine import session_scope
from nexus.settings import Settings
from tests.integration._scripted_llm import ScriptedLLM


def _settings(**overrides) -> Settings:
    env = {
        "WEB_SESSION_SECRET": "test-secret",
        "WEB_DEV_AUTH": "true",
    }
    env.update({k.upper(): str(v) for k, v in overrides.items()})
    with patch.dict("os.environ", env, clear=False):
        return Settings()


@pytest_asyncio.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(_settings())
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


async def _delete_user(telegram_id: int) -> None:
    async with session_scope() as session:
        u = await repo.get_user_by_telegram_id(session, telegram_id)
        if u is not None:
            await session.delete(u)


# ---------------------------------------------------------------------------
# Auth pages + config endpoint
# ---------------------------------------------------------------------------


async def test_root_redirects_to_login_when_anonymous(
    client: httpx.AsyncClient,
) -> None:
    r = await client.get("/", follow_redirects=False)
    assert r.status_code in (302, 307)
    assert r.headers["location"] == "/login"


async def test_login_page_renders_when_anonymous(
    client: httpx.AsyncClient,
) -> None:
    r = await client.get("/login", follow_redirects=False)
    assert r.status_code == 200
    assert "Nexus" in r.text


async def test_app_page_redirects_to_login_when_anonymous(
    client: httpx.AsyncClient,
) -> None:
    r = await client.get("/app", follow_redirects=False)
    assert r.status_code in (302, 307)
    assert r.headers["location"] == "/login"


async def test_auth_config_exposes_dev_flag(client: httpx.AsyncClient) -> None:
    r = await client.get("/api/auth/config")
    assert r.status_code == 200
    body = r.json()
    assert body["dev_auth_enabled"] is True
    assert body["telegram_bot_username"] is None


# ---------------------------------------------------------------------------
# Dev login + protected routes
# ---------------------------------------------------------------------------


async def test_protected_route_requires_session(
    client: httpx.AsyncClient,
) -> None:
    assert (await client.get("/api/projects")).status_code == 401


async def test_dev_login_creates_user_and_session(
    client: httpx.AsyncClient,
) -> None:
    try:
        r = await client.post(
            "/auth/dev",
            json={"telegram_id": 700_100, "display_name": "TestyMcTestface"},
        )
        assert r.status_code == 200, r.text
        assert uuid.UUID(r.json()["user_id"])
        me = await client.get("/api/me")
        assert me.status_code == 200
        assert me.json()["telegram_id"] == 700_100
    finally:
        await _delete_user(700_100)


async def test_dev_login_disabled_returns_404() -> None:
    app = create_app(_settings(WEB_DEV_AUTH="false"))
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        r = await c.post("/auth/dev", json={"telegram_id": 1})
    assert r.status_code == 404


async def test_logout_clears_session(client: httpx.AsyncClient) -> None:
    try:
        await client.post("/auth/dev", json={"telegram_id": 700_200})
        assert (await client.get("/api/me")).status_code == 200
        await client.post("/auth/logout")
        assert (await client.get("/api/me")).status_code == 401
    finally:
        await _delete_user(700_200)


# ---------------------------------------------------------------------------
# Projects + messages
# ---------------------------------------------------------------------------


async def test_projects_list_returns_user_projects(
    client: httpx.AsyncClient,
) -> None:
    async with session_scope() as session:
        user = await repo.create_user(
            session, telegram_id=700_300, display_name="HasProjects"
        )
        project = await repo.create_project(
            session, user_id=user.id, name="Spanish", domain="language_learning"
        )
        project_id = project.id

    try:
        await client.post("/auth/dev", json={"telegram_id": 700_300})
        r = await client.get("/api/projects")
        assert r.status_code == 200
        items = r.json()
        assert len(items) == 1
        assert items[0]["name"] == "Spanish"
        assert items[0]["domain"] == "language_learning"
        assert uuid.UUID(items[0]["id"]) == project_id
    finally:
        await _delete_user(700_300)


async def test_send_message_dispatches_to_specialist(
    client: httpx.AsyncClient,
) -> None:
    from nexus.domains.base import DomainConfig, Profile, SummaryConfig

    cfg = DomainConfig(
        domain="language_learning",
        profile=Profile.model_validate({"language": "spanish"}),
        summary=SummaryConfig(prompt_style="language_learning"),
    ).model_dump()

    async with session_scope() as session:
        user = await repo.create_user(
            session, telegram_id=700_400, display_name="ChatUser"
        )
        project = await repo.create_project(
            session,
            user_id=user.id,
            name="Spanish",
            domain="language_learning",
            config=cfg,
        )
        project_id = project.id

    try:
        await client.post("/auth/dev", json={"telegram_id": 700_400})
        scripted = ScriptedLLM(replies=["hola! ready?"])
        with patch("nexus.specialist.agent.get_llm_client", return_value=scripted):
            r = await client.post(
                f"/api/projects/{project_id}/messages",
                json={"text": "hi there"},
            )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["content"] == "hola! ready?"
        assert uuid.UUID(body["session_id"])

        h = await client.get(f"/api/projects/{project_id}/messages?limit=10")
        assert h.status_code == 200
        history = h.json()
        # Both messages persisted. NOTE: within a single transaction the
        # default `now()` resolves to txn-start, so user+assistant share an
        # occurred_at and the visual order via `recent_messages` is
        # undefined. Asserting on content rather than position here; the
        # underlying ordering issue is tracked separately.
        assert len(history) == 2
        contents = {m["content"] for m in history}
        roles = {m["role"] for m in history}
        assert contents == {"hi there", "hola! ready?"}
        assert roles == {"user", "assistant"}
    finally:
        await _delete_user(700_400)


async def test_send_message_rejects_someone_elses_project(
    client: httpx.AsyncClient,
) -> None:
    """Auth: the session-user must own the project_id in the URL."""
    async with session_scope() as session:
        owner = await repo.create_user(
            session, telegram_id=700_500, display_name="Owner"
        )
        await repo.create_user(
            session, telegram_id=700_501, display_name="Attacker"
        )
        project = await repo.create_project(
            session, user_id=owner.id, name="Private", domain="language_learning"
        )
        project_id = project.id

    try:
        await client.post("/auth/dev", json={"telegram_id": 700_501})
        r = await client.post(
            f"/api/projects/{project_id}/messages", json={"text": "hello"}
        )
        assert r.status_code == 404
    finally:
        await _delete_user(700_500)
        await _delete_user(700_501)
