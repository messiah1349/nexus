"""Shared pytest fixtures.

Integration tests open a single connection per test, begin a transaction,
hand the bound session to the test, and roll back on teardown. Nothing
from a test ever lands in the DB. Repository functions only flush, so the
outer rollback is sufficient.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


@pytest.fixture(scope="session", autouse=True)
def _isolate_test_env(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """Hermetic env for the test session.

    Three isolations, all session-scoped:

    - ``$HOME`` → tmpdir, so ``nexus.settings`` doesn't load the developer's
      real ``~/.zshrc``.
    - cwd → tmpdir, so the project's ``.env`` (which carries real keys in
      development) doesn't leak into Settings.
    - Settings-relevant env vars (``LLM_*``, API keys) cleared.

    Tests that need a specific shell rc / .env set them up themselves on top
    of this baseline.
    """
    saved_env: dict[str, str] = {}
    keys_to_isolate = (
        "HOME",
        "GEMINI_API_KEY",
        "ANTHROPIC_API_KEY",
        "TELEGRAM_BOT_TOKEN",
        "LLM_PROVIDER",
        "LLM_MODEL",
    )
    for key in keys_to_isolate:
        if key in os.environ:
            saved_env[key] = os.environ[key]
            del os.environ[key]
    os.environ["HOME"] = str(tmp_path_factory.mktemp("hermetic_home"))

    saved_cwd = os.getcwd()
    os.chdir(tmp_path_factory.mktemp("hermetic_cwd"))

    yield

    os.chdir(saved_cwd)
    for key in keys_to_isolate:
        os.environ.pop(key, None)
        if key in saved_env:
            os.environ[key] = saved_env[key]


from nexus.settings import get_settings  # noqa: E402 — imported after env isolation


@pytest_asyncio.fixture(scope="session")
async def engine() -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(get_settings().postgres_url, future=True)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest_asyncio.fixture
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    async with engine.connect() as conn:
        trans = await conn.begin()
        sessionmaker = async_sessionmaker(bind=conn, expire_on_commit=False)
        async with sessionmaker() as s:
            yield s
        await trans.rollback()
