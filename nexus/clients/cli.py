from __future__ import annotations

import asyncio
import uuid

import typer

from nexus import __version__
from nexus.config import get_settings
from nexus.db import repository as repo
from nexus.db.engine import dispose_engine, session_scope

app = typer.Typer(no_args_is_help=True, help="Nexus CLI")
user_app = typer.Typer(no_args_is_help=True, help="User commands")
project_app = typer.Typer(no_args_is_help=True, help="Project commands")
app.add_typer(user_app, name="user")
app.add_typer(project_app, name="project")


def _run(coro) -> None:
    async def _wrapped() -> None:
        try:
            await coro
        finally:
            await dispose_engine()

    asyncio.run(_wrapped())


@app.command()
def hello() -> None:
    """Smoke-test command: prints version and resolved env."""
    settings = get_settings()
    typer.echo(f"nexus {__version__} — env={settings.env} log_level={settings.log_level}")


@app.command()
def version() -> None:
    typer.echo(__version__)


@user_app.command("create")
def user_create(
    display_name: str | None = typer.Option(None, "--name", help="Human-readable name"),
    telegram_id: int | None = typer.Option(None, "--telegram-id"),
    email: str | None = typer.Option(None, "--email"),
) -> None:
    """Create a user. Prints the new user's UUID."""

    async def _do() -> None:
        async with session_scope() as session:
            user = await repo.create_user(
                session,
                display_name=display_name,
                telegram_id=telegram_id,
                email=email,
            )
            typer.echo(str(user.id))

    _run(_do())


@project_app.command("create")
def project_create(
    user_id: str = typer.Option(..., "--user-id", help="Owning user UUID"),
    name: str = typer.Option(..., "--name", help="Project display name"),
    domain: str = typer.Option(..., "--domain", help="e.g. language_learning, fitness"),
) -> None:
    """Create a project under a user. Prints the new project's UUID."""

    try:
        uid = uuid.UUID(user_id)
    except ValueError as exc:
        raise typer.BadParameter(f"--user-id is not a valid UUID: {user_id}") from exc

    async def _do() -> None:
        async with session_scope() as session:
            user = await repo.get_user(session, uid)
            if user is None:
                raise typer.BadParameter(f"no user with id {uid}")
            project = await repo.create_project(
                session, user_id=uid, name=name, domain=domain
            )
            typer.echo(str(project.id))

    _run(_do())


@project_app.command("list")
def project_list(
    user_id: str = typer.Option(..., "--user-id"),
) -> None:
    """List active projects for a user."""

    try:
        uid = uuid.UUID(user_id)
    except ValueError as exc:
        raise typer.BadParameter(f"--user-id is not a valid UUID: {user_id}") from exc

    async def _do() -> None:
        async with session_scope() as session:
            projects = await repo.list_projects(session, uid)
            if not projects:
                typer.echo("(none)")
                return
            for p in projects:
                typer.echo(f"{p.id}\t{p.domain}\t{p.name}")

    _run(_do())


if __name__ == "__main__":
    app()
