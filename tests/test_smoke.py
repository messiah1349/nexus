def test_package_imports() -> None:
    import nexus

    assert nexus.__version__


def test_cli_app_constructs() -> None:
    from nexus.clients.cli import app

    assert app is not None


def test_settings_load() -> None:
    from nexus.config import get_settings

    settings = get_settings()
    assert settings.postgres_url.startswith("postgresql")
