import typer

from nexus import __version__
from nexus.config import get_settings

app = typer.Typer(no_args_is_help=True, help="Nexus CLI")


@app.command()
def hello() -> None:
    """Smoke-test command: prints version and resolved env."""
    settings = get_settings()
    typer.echo(f"nexus {__version__} — env={settings.env} log_level={settings.log_level}")


@app.command()
def version() -> None:
    typer.echo(__version__)


if __name__ == "__main__":
    app()
