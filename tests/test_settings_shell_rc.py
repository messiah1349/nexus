"""Tests for the ~/.zshrc env-loading fallback in nexus.settings."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point Path.home() at a tmp dir and re-import nexus.settings so the
    module-level _env_file_sources() is re-evaluated against the fake home.
    """
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # Clean any pre-existing keys so the .zshrc / .env values are the only
    # source.
    for var in ("GEMINI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    import nexus.settings as settings_mod

    importlib.reload(settings_mod)
    return tmp_path


def test_loads_export_from_zshrc(fake_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (fake_home / ".zshrc").write_text(
        "alias ll='ls -la'\n"
        "export GEMINI_API_KEY=from_zshrc\n"
        "if [ -f /etc/foo ]; then echo found; fi\n"
    )
    # Run from a directory with no .env to keep the test deterministic.
    monkeypatch.chdir(fake_home)
    import nexus.settings as settings_mod

    importlib.reload(settings_mod)
    s = settings_mod.get_settings()
    assert s.gemini_api_key == "from_zshrc"


def test_project_env_overrides_zshrc(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (fake_home / ".zshrc").write_text("export GEMINI_API_KEY=from_zshrc\n")
    project_dir = fake_home / "project"
    project_dir.mkdir()
    (project_dir / ".env").write_text("GEMINI_API_KEY=from_dotenv\n")
    monkeypatch.chdir(project_dir)
    import nexus.settings as settings_mod

    importlib.reload(settings_mod)
    s = settings_mod.get_settings()
    assert s.gemini_api_key == "from_dotenv"


def test_env_var_overrides_files(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (fake_home / ".zshrc").write_text("export GEMINI_API_KEY=from_zshrc\n")
    monkeypatch.setenv("GEMINI_API_KEY", "from_env")
    monkeypatch.chdir(fake_home)
    import nexus.settings as settings_mod

    importlib.reload(settings_mod)
    s = settings_mod.get_settings()
    assert s.gemini_api_key == "from_env"


def test_missing_zshrc_is_silently_skipped(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No .zshrc in fake home — settings should still construct without error.
    monkeypatch.chdir(fake_home)
    import nexus.settings as settings_mod

    importlib.reload(settings_mod)
    s = settings_mod.get_settings()
    assert s.gemini_api_key is None
    assert s.anthropic_api_key is None
