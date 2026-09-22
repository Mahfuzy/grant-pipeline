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


@sources_app.command("sync")
def sources_sync(
    directory: Path = typer.Option(
        Path("sources"), "--dir", exists=True, file_okay=False, help="Directory of source YAML."
    ),
) -> None:
    """Add or update every source YAML in a directory."""
    from fundscout.db.session import session_scope
    from fundscout.sources.spec import load_source_spec, upsert_source

    files = sorted(directory.glob("*.yaml"))
    with session_scope() as session:
        for file in files:
            spec = load_source_spec(file)
            _, created = upsert_source(session, spec)
            typer.echo(f"{'Added' if created else 'Updated'} source {spec.name!r}.")
            if not spec.terms_reviewed:
                typer.echo(
                    f"warning: {spec.name}: terms_reviewed is false; the scheduler skips it.",
                    err=True,
                )


async def _run(names: list[str], force: bool, limit: int | None, extract: bool = True) -> bool:
    from fundscout.db.models import RunStatus
    from fundscout.db.session import get_sessionmaker
    from fundscout.fetch.storage import LocalRawStorage
    from fundscout.pipeline.extraction import make_llm_factory
    from fundscout.pipeline.runner import SourceNotRunnable, build_http_client, run_source

    settings = get_settings()
    if settings.contact_email == "change-me@example.com":
        typer.echo(
            "warning: CONTACT_EMAIL is the placeholder; set a real contact address in .env "
            "so site owners can reach you (it is sent in the User-Agent).",
            err=True,
        )
    storage = LocalRawStorage(settings.raw_storage_dir)
    llm_factory = make_llm_factory(settings) if extract else None
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
                        extract=extract,
                        llm_factory=llm_factory,
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
                    f"errors {run.errors} ({run.items_fetched} network fetches); "
                    f"grants created {run.grants_created}, updated {run.grants_updated}"
                )
                ok = ok and run.status != RunStatus.FAILED
    return ok


@app.command("run")
def run_command(
    source: str = typer.Option(..., "--source", help="Source name."),
    force: bool = typer.Option(False, "--force", help="Run even if disabled/terms not reviewed."),
    limit: int | None = typer.Option(None, "--limit", min=1, help="Process at most N items."),
    extract: bool = typer.Option(True, "--extract/--no-extract", help="Extract new documents."),
) -> None:
    """Run one source now."""
    if force:
        typer.echo("warning: --force bypasses the enabled/terms_reviewed checks.", err=True)
    if not asyncio.run(_run([source], force, limit, extract)):
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


@app.command("reprocess")
def reprocess_command(
    source: str = typer.Option(..., "--source", help="Source name."),
    limit: int | None = typer.Option(None, "--limit", min=1, help="At most N documents."),
) -> None:
    """Re-extract a source's stored documents (latest snapshot per URL); no fetching."""
    from fundscout.db.models import Source
    from fundscout.db.session import get_sessionmaker
    from fundscout.fetch.storage import LocalRawStorage
    from fundscout.pipeline.extraction import make_llm_factory, reprocess_source

    settings = get_settings()
    with get_sessionmaker()() as session:
        src = session.scalars(select(Source).where(Source.name == source)).one_or_none()
        if src is None:
            typer.echo(f"error: no source named {source!r}", err=True)
            raise typer.Exit(1)
        stats = asyncio.run(
            reprocess_source(
                session,
                src,
                storage=LocalRawStorage(settings.raw_storage_dir),
                settings=settings,
                llm_factory=make_llm_factory(settings),
                limit=limit,
            )
        )
    typer.echo(
        f"{source}: extracted {stats.documents} documents ({stats.grants} grants), "
        f"failed {stats.failed}, skipped {stats.skipped + stats.llm_not_configured}"
        + (" (LLM not configured)" if stats.llm_not_configured else "")
        + f", tokens in/out {stats.usage['input_tokens']}/{stats.usage['output_tokens']}"
        + f"; grants created {stats.store.created}, updated {stats.store.updated}"
    )
    if stats.failed:
        raise typer.Exit(1)


@app.command("refresh-status")
def refresh_status_command() -> None:
    """Recompute grant statuses as dates pass (the worker also does this daily)."""
    from fundscout.db.session import session_scope
    from fundscout.pipeline.store import refresh_statuses

    with session_scope() as session:
        changed = refresh_statuses(session)
    typer.echo(f"{changed} grant statuses changed.")


@app.command("worker")
def worker_command() -> None:
    """Start the scheduler: runs each source on its cron schedule (SPEC §9: only enabled
    sources whose terms have been reviewed)."""
    from fundscout.pipeline.scheduler import run_worker

    asyncio.run(run_worker(get_settings()))


@app.command("api")
def api_command(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8000, "--port"),
) -> None:
    """Serve the internal API (and the built admin UI, if present)."""
    import uvicorn

    settings = get_settings()
    if settings.api_key is None:
        typer.echo(
            "warning: API_KEY is not set; every endpoint except /health will refuse.", err=True
        )
    uvicorn.run("fundscout.api.main:app", host=host, port=port, log_config=None)


@app.command("eval-extraction")
def eval_extraction_command(
    labels: Path = typer.Option(
        Path("tests/fixtures/eval/labels.yaml"), "--labels", exists=True, dir_okay=False
    ),
    sources_dir: Path = typer.Option(Path("sources"), "--sources-dir", exists=True),
    no_llm: bool = typer.Option(False, "--no-llm", help="Skip documents that need the LLM."),
    force_llm: bool = typer.Option(
        False, "--force-llm", help="Send every document through the LLM (costs tokens)."
    ),
) -> None:
    """Run extraction on the hand-labelled set and report per-field accuracy."""
    from fundscout.extract.evaluate import run_eval
    from fundscout.pipeline.extraction import make_llm_factory

    settings = get_settings()
    llm_factory = None if no_llm else make_llm_factory(settings)
    report = run_eval(labels, sources_dir, llm_factory=llm_factory, force_llm=force_llm)
    typer.echo(report.render())


if __name__ == "__main__":
    app()
