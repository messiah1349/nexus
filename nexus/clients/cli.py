from __future__ import annotations

import asyncio
import uuid

import typer

from nexus import __version__
from nexus.architect import ArchitectInterview, persist_architect_output
from nexus.settings import get_settings
from nexus.db import repository as repo
from nexus.db.engine import dispose_engine, session_scope
from nexus.domains.registry import list_available_domains
from nexus.specialist import SpecialistAgent, end_session_with_summary
from sqlalchemy import select, desc as sa_desc
from nexus.db.models import Session as DBSession

app = typer.Typer(no_args_is_help=True, help="Nexus CLI")
user_app = typer.Typer(no_args_is_help=True, help="User commands")
project_app = typer.Typer(no_args_is_help=True, help="Project commands")
architect_app = typer.Typer(no_args_is_help=True, help="Architect (onboarding) commands")
session_app = typer.Typer(no_args_is_help=True, help="Session commands")
app.add_typer(user_app, name="user")
app.add_typer(project_app, name="project")
app.add_typer(architect_app, name="architect")
app.add_typer(session_app, name="session")


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


@architect_app.command("domains")
def architect_domains() -> None:
    """List domains the architect can onboard."""
    for d in list_available_domains():
        typer.echo(d)


@architect_app.command("run")
def architect_run(
    user_id: str = typer.Option(..., "--user-id"),
    domain: str = typer.Option(..., "--domain", help="e.g. language_learning"),
    no_persist: bool = typer.Option(
        False, "--no-persist", help="Run interview but skip DB writes (dry run)"
    ),
) -> None:
    """Interactive architect onboarding — interview the user and persist
    the resulting project + plans atomically at the end.
    """

    try:
        uid = uuid.UUID(user_id)
    except ValueError as exc:
        raise typer.BadParameter(f"--user-id is not a valid UUID: {user_id}") from exc

    if domain not in list_available_domains():
        raise typer.BadParameter(
            f"unknown domain '{domain}'. available: "
            f"{', '.join(list_available_domains()) or '(none)'}"
        )

    async def _do() -> None:
        interview = ArchitectInterview(domain=domain)
        typer.secho("Architect online. Ctrl-D or empty line to quit early.\n", fg="cyan")
        opener = await interview.kick_off()
        typer.secho(f"architect> {opener}\n", fg="green")

        while not interview.done:
            try:
                user_input = input("you> ").strip()
            except (EOFError, KeyboardInterrupt):
                typer.secho("\n(interview aborted)", fg="yellow")
                return
            if not user_input:
                typer.secho("(empty — aborting)", fg="yellow")
                return

            reply, done = await interview.turn(user_input)
            typer.secho(f"\narchitect> {reply}\n", fg="green")
            if done:
                break

        if interview.proposal is None:
            typer.secho("(no proposal produced — nothing to save)", fg="red")
            raise typer.Exit(code=1)

        if no_persist:
            typer.secho("--no-persist set; skipping DB writes.", fg="yellow")
            typer.echo(interview.proposal.model_dump_json(indent=2))
            return

        async with session_scope() as session:
            project, plans = await persist_architect_output(
                session, user_id=uid, domain=domain, proposal=interview.proposal
            )
            typer.secho("\nSaved.", fg="cyan")
            typer.echo(f"project_id\t{project.id}")
            for plan in plans:
                typer.echo(f"plan\t{plan.horizon}\t{plan.id}\t{plan.name}")

    _run(_do())


@app.command()
def chat(
    project_id: str = typer.Option(..., "--project-id"),
) -> None:
    """Interactive chat with the specialist. Type `/end` to close the session
    and trigger the summarizer."""

    try:
        pid = uuid.UUID(project_id)
    except ValueError as exc:
        raise typer.BadParameter(f"--project-id is not a valid UUID: {project_id}") from exc

    async def _do() -> None:
        agent = SpecialistAgent(project_id=pid)
        typer.secho(
            "Chat ready. `/end` to close + summarize the session, Ctrl-D to leave it open.\n",
            fg="cyan",
        )
        active_session_id: uuid.UUID | None = None
        while True:
            try:
                user_input = input("you> ").strip()
            except (EOFError, KeyboardInterrupt):
                typer.secho("\n(leaving session open)", fg="yellow")
                return
            if not user_input:
                continue
            if user_input.lower() in ("/end", "/end_lesson"):
                if active_session_id is None:
                    typer.secho("(no active session yet — nothing to summarize)", fg="yellow")
                    return
                typer.secho("\n(summarizing…)\n", fg="cyan")
                async with session_scope() as db:
                    summary = await end_session_with_summary(
                        db, session_id=active_session_id, reason="explicit"
                    )
                    typer.echo(f"summary_id\t{summary.id}")
                    typer.echo(f"focus_tags\t{', '.join(summary.focus_tags) or '—'}")
                    typer.echo(f"\n{summary.content}\n")
                return

            async with session_scope() as db:
                reply, sess = await agent.handle_message(db, user_input)
                active_session_id = sess.id
            typer.secho(f"\ncoach> {reply}\n", fg="green")

    _run(_do())


@app.command("bot")
def bot_run() -> None:
    """Start the Telegram bot (with in-process idle-timeout sweeper).

    Requires TELEGRAM_BOT_TOKEN in env / .env / ~/.zshrc. Blocks until
    interrupted (Ctrl-C).
    """
    from nexus.clients.telegram import NexusBot

    NexusBot().run()


@session_app.command("list")
def session_list(
    project_id: str = typer.Option(..., "--project-id"),
    limit: int = typer.Option(10, "--limit"),
) -> None:
    """List recent sessions for a project."""

    try:
        pid = uuid.UUID(project_id)
    except ValueError as exc:
        raise typer.BadParameter(f"--project-id is not a valid UUID: {project_id}") from exc

    async def _do() -> None:
        async with session_scope() as db:
            result = await db.execute(
                select(DBSession)
                .where(DBSession.project_id == pid)
                .order_by(sa_desc(DBSession.started_at))
                .limit(limit)
            )
            sessions = list(result.scalars().all())
            if not sessions:
                typer.echo("(no sessions)")
                return
            typer.echo("id\tstarted_at\tstatus\tend_reason\tplan_item_index")
            for s in sessions:
                typer.echo(
                    f"{s.id}\t{s.started_at.isoformat()}\t{s.status}\t"
                    f"{s.end_reason or '—'}\t{s.plan_item_index if s.plan_item_index is not None else '—'}"
                )

    _run(_do())


if __name__ == "__main__":
    app()
