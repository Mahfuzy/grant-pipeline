"""Fundscout command-line interface."""

import asyncio
from pathlib import Path

import typer
from sqlalchemy import select
from sqlalchemy.engine import make_url

from fundscout import __version__
from fundscout.config import get_settings
from fundscout.logging import configure_logging

app = typer.Typer(help="Fundscout grant data pipeline.", no_args_is_help=True)
db_app = typer.Typer(help="Database commands.", no_args_is_help=True)
taxonomy_app = typer.Typer(help="Taxonomy commands.", no_args_is_help=True)
sources_app = typer.Typer(help="Source commands.", no_args_is_help=True)
app.add_typer(db_app, name="db")
app.add_typer(taxonomy_app, name="taxonomy")
app.add_typer(sources_app, name="sources")


@app.callback()
def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)


@app.command()
def version() -> None:
    """Print the installed Fundscout version."""
    typer.echo(__version__)


@app.command()
def config() -> None:
    """Show the effective configuration (secrets masked)."""
    settings = get_settings()
    values = settings.model_dump()
    values["database_url"] = make_url(settings.database_url).render_as_string(hide_password=True)
    for key, value in values.items():
        typer.echo(f"{key} = {value}")
    typer.echo(f"user_agent = {settings.user_agent}")


@db_app.command("upgrade")
def db_upgrade(revision: str = typer.Argument("head", help="Target revision.")) -> None:
    """Apply database migrations."""
    from fundscout.db import migrations

    migrations.upgrade(revision)
    typer.echo(f"Database upgraded to {revision}.")


@taxonomy_app.command("seed")
def taxonomy_seed() -> None:
    """Load taxonomy YAML files into the database (idempotent)."""
    from fundscout.db.session import session_scope
    from fundscout.taxonomy.loader import seed_taxonomy

    with session_scope() as session:
        results = seed_taxonomy(session)
    for kind, r in results.items():
        typer.echo(f"{kind}: {r.created} created, {r.updated} updated, {r.unchanged} unchanged")
        if r.not_in_yaml:
            typer.echo(
                f"  warning: in database but not in YAML (kept): {', '.join(r.not_in_yaml)}",
                err=True,
            )


@sources_app.command("list")
def sources_list() -> None:
    """List configured sources."""
    from fundscout.db.models import Source, SourceRun
    from fundscout.db.session import session_scope

    with session_scope() as session:
        sources = session.scalars(select(Source).order_by(Source.name)).all()
        if not sources:
            typer.echo("No sources. Add one with `fundscout sources add --file <yaml>`.")
            return
        for src in sources:
            last = session.scalars(
                select(SourceRun)
                .where(SourceRun.source_id == src.id)
                .order_by(SourceRun.started_at.desc())
                .limit(1)
            ).first()
            last_run = (
                f"{last.started_at:%Y-%m-%d %H:%M} {last.status.value} "
                f"(discovered {last.items_discovered}, fetched {last.items_fetched}, "
                f"unchanged {last.items_unchanged}, errors {last.errors})"
                if last
                else "never run"
            )
            flags = [
                "enabled" if src.enabled else "DISABLED",
                "terms reviewed" if src.terms_reviewed else "TERMS NOT REVIEWED",
            ]
            typer.echo(f"{src.name}  [{src.adapter}; {', '.join(flags)}]  last run: {last_run}")


@sources_app.command("add")
def sources_add(
    file: Path = typer.Option(..., "--file", exists=True, dir_okay=False, help="Source YAML."),
) -> None:
    """Add a source from YAML, or update it if one with that name exists."""
    from fundscout.db.session import session_scope
    from fundscout.sources.spec import load_source_spec, upsert_source

    spec = load_source_spec(file)
    with session_scope() as session:
        _, created = upsert_source(session, spec)
    typer.echo(f"{'Added' if created else 'Updated'} source {spec.name!r}.")
    if not spec.terms_reviewed:
        typer.echo("warning: terms_reviewed is false; the scheduler will not run it.", err=True)


async def _run(names: list[str], force: bool, limit: int | None) -> bool:
    from fundscout.db.models import RunStatus
    from fundscout.db.session import get_sessionmaker
    from fundscout.fetch.storage import LocalRawStorage
    from fundscout.pipeline.runner import SourceNotRunnable, build_http_client, run_source

    settings = get_settings()
    if settings.contact_email == "change-me@example.com":
        typer.echo(
            "warning: CONTACT_EMAIL is the placeholder; set a real contact address in .env "
            "so site owners can reach you (it is sent in the User-Agent).",
            err=True,
        )
    storage = LocalRawStorage(settings.raw_storage_dir)
    ok = True
    async with build_http_client(settings) as http:
        for name in names:
            with get_sessionmaker()() as session:
                try:
                    run = await run_source(
                        session,
                        name,
                        settings=settings,
                        http=http,
                        storage=storage,
                        force=force,
                        limit=limit,
                    )
                except SourceNotRunnable as exc:
                    typer.echo(f"error: {exc}", err=True)
                    ok = False
                    continue
                # Every item ends up unchanged, stored, or failed.
                stored = max(0, run.items_discovered - run.items_unchanged - run.errors)
                typer.echo(
                    f"{name}: {run.status.value} - discovered {run.items_discovered}, "
                    f"stored {stored} new/changed, unchanged {run.items_unchanged}, "
                    f"errors {run.errors} ({run.items_fetched} network fetches)"
                )
                ok = ok and run.status != RunStatus.FAILED
    return ok


@app.command("run")
def run_command(
    source: str = typer.Option(..., "--source", help="Source name."),
    force: bool = typer.Option(False, "--force", help="Run even if disabled/terms not reviewed."),
    limit: int | None = typer.Option(None, "--limit", min=1, help="Process at most N items."),
) -> None:
    """Run one source now."""
    if force:
        typer.echo("warning: --force bypasses the enabled/terms_reviewed checks.", err=True)
    if not asyncio.run(_run([source], force, limit)):
        raise typer.Exit(1)


@app.command("run-all")
def run_all_command(
    limit: int | None = typer.Option(None, "--limit", min=1, help="Process at most N items."),
) -> None:
    """Run every enabled source whose terms have been reviewed."""
    from fundscout.db.models import Source
    from fundscout.db.session import session_scope

    with session_scope() as session:
        names = list(
            session.scalars(
                select(Source.name)
                .where(Source.enabled.is_(True), Source.terms_reviewed.is_(True))
                .order_by(Source.name)
            )
        )
    if not names:
        typer.echo("No runnable sources.")
        return
    if not asyncio.run(_run(names, False, limit)):
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
