"""edm CLI — operational and demo commands.

Examples:
  edm db init                     -- run schema migration
  edm ingest github --max-prs 50  -- pull PRs from TARGET_REPO
  edm process                     -- extract + reason over unprocessed sources
  edm findings                    -- list open contradiction findings
  edm graph export out.html       -- render the decision graph to a static HTML
  edm serve                       -- run web + webhook server
"""

from __future__ import annotations

import os
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy import text

from edm.db import session_scope
from edm.logging import configure_logging

app = typer.Typer(help="Engineering Decision Memory")
db_app = typer.Typer(help="Database operations")
ingest_app = typer.Typer(help="Source ingestors")
graph_app = typer.Typer(help="Graph operations")
app.add_typer(db_app, name="db")
app.add_typer(ingest_app, name="ingest")
app.add_typer(graph_app, name="graph")

console = Console()


@app.callback()
def _root() -> None:
    configure_logging()


@db_app.command("init")
def db_init(migration: Path = typer.Option(Path("migrations/001_init.sql"))) -> None:
    """Apply the initial schema migration."""
    from edm.db import get_engine

    sql = migration.read_text(encoding="utf-8")
    engine = get_engine()
    raw = engine.raw_connection()
    try:
        cur = raw.cursor()
        cur.execute(sql)
        raw.commit()
    finally:
        raw.close()
    console.print(f"[green]applied[/green] {migration}")


@ingest_app.command("github")
def ingest_github(
    repo: str = typer.Option(None, help="Override TARGET_REPO."),
    max_prs: int = typer.Option(50),
    state: str = typer.Option("all", help="open | closed | all"),
) -> None:
    from edm.config import get_settings
    from edm.ingest.github import ingest_repo

    settings = get_settings()
    repo_full = repo or settings.target_repo
    if not repo_full:
        raise typer.BadParameter("Provide --repo or set TARGET_REPO")
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise typer.BadParameter("Set GITHUB_TOKEN env var (PAT) for non-app ingestion")
    n = ingest_repo(token, repo_full, max_prs=max_prs, state=state)
    console.print(f"[green]ingested[/green] {n} new source rows from {repo_full}")


@app.command("process")
def process(limit: int = typer.Option(50)) -> None:
    """Extract decisions and run contradiction detection on unprocessed sources."""
    from edm.pipeline import process_unprocessed

    summaries = process_unprocessed(limit=limit)
    console.print(f"[green]processed[/green] {len(summaries)} sources")
    for s in summaries:
        if s.get("findings"):
            console.print(f"  [yellow]contradictions:[/yellow] {len(s['findings'])} on source {s['source_id']}")


@app.command("findings")
def findings(status: str = typer.Option("open")) -> None:
    """List contradiction findings."""
    with session_scope() as session:
        rows = session.execute(
            text(
                """
                SELECT cf.id, cf.severity, cf.rationale, cf.created_at,
                       d.title AS prior_title,
                       s.url AS triggering_url, s.title AS triggering_title
                FROM conflict_findings cf
                JOIN decisions d ON d.id = cf.conflicting_decision_id
                JOIN sources s ON s.id = cf.triggering_source_id
                WHERE cf.status = :status
                ORDER BY cf.created_at DESC
                """
            ),
            {"status": status},
        ).mappings().all()

    table = Table(title=f"Conflict findings ({status})")
    table.add_column("Severity")
    table.add_column("Triggering source")
    table.add_column("Conflicts with prior decision")
    table.add_column("Rationale", overflow="fold")
    for r in rows:
        table.add_row(
            r["severity"],
            f"{r['triggering_title']} ({r['triggering_url']})",
            r["prior_title"],
            r["rationale"],
        )
    console.print(table)


@graph_app.command("export")
def graph_export(out: Path = typer.Argument(Path("graph.html"))) -> None:
    """Render the decision graph to an interactive HTML."""
    from edm.web.render import render_graph_to_file

    path = render_graph_to_file(out)
    console.print(f"[green]wrote[/green] {path}")


@app.command("setup")
def setup_cli() -> None:
    """First-run setup: create the admin, choose an LLM provider, optionally connect GitHub."""
    import getpass
    import webbrowser

    from edm import setup_flow, users as users_repo
    from edm.ingest import github_oauth
    from edm import install_config, audit

    if setup_flow.is_setup_complete():
        console.print("[yellow]Already set up.[/yellow] Use the web UI at /settings to make changes.")
        raise typer.Exit(code=0)

    if not users_repo.has_active_admin():
        console.print("[bold]Step 1/3 — create admin account[/bold]")
        username = typer.prompt("Admin username")
        password = getpass.getpass("Admin password: ")
        confirm = getpass.getpass("Confirm password: ")
        if password != confirm:
            console.print("[red]Passwords don't match.[/red]")
            raise typer.Exit(code=1)
        admin = setup_flow.create_admin(username, password)
        console.print(f"[green]Created admin: {admin.username}[/green]")
    else:
        admin = next((u for u in users_repo.list_users() if u.role == "admin" and u.active), None)
        console.print(f"Admin already exists: {admin.username if admin else '(none)'}")

    if not install_config.get_llm_config().provider:
        console.print("\n[bold]Step 2/3 — pick an LLM provider[/bold]")
        console.print("  1) gemini   (default model: gemini-2.5-flash)")
        console.print("  2) anthropic (default model: claude-sonnet-4-5)")
        console.print("  3) openai   (default model: gpt-4o-mini)")
        console.print("  4) openrouter (default model: openai/gpt-4o-mini)")
        choice = typer.prompt("Choose 1-4", default="1")
        mapping = {"1": ("gemini", "gemini-2.5-flash"),
                   "2": ("anthropic", "claude-sonnet-4-5"),
                   "3": ("openai", "gpt-4o-mini"),
                   "4": ("openrouter", "openai/gpt-4o-mini")}
        provider, default_model = mapping.get(choice, ("gemini", "gemini-2.5-flash"))
        model = typer.prompt(f"Model id", default=default_model)
        api_key = getpass.getpass(f"{provider} API key: ")
        result = setup_flow.validate_and_save_llm(provider=provider, model=model, api_key=api_key, configured_by=admin.id)
        if not result["ok"]:
            console.print(f"[red]Validation failed: {result['error']}[/red]")
            raise typer.Exit(code=1)
        console.print(f"[green]LLM configured: {provider}/{model}[/green]")
    else:
        console.print("LLM already configured; skipping.")

    if typer.confirm("\nConnect GitHub now? (you can skip and do it later from /settings/github)", default=True):
        device = github_oauth.request_device_code()
        console.print(f"\n[bold]Open[/bold] {device.verification_uri}")
        console.print(f"[bold]Enter code:[/bold] [cyan]{device.user_code}[/cyan]")
        try:
            webbrowser.open(device.verification_uri)
        except Exception:
            pass
        console.print("Polling for authorization...")
        token = github_oauth.poll_for_token(device)
        me = github_oauth.get_authenticated_user(token.access_token)
        console.print(f"[green]Authenticated as @{me['login']}[/green]")
        orgs = github_oauth.list_user_orgs(token.access_token)
        if not orgs:
            console.print("[yellow]No orgs visible to this token. Skipping org pick.[/yellow]")
            org = None
        else:
            console.print("Pick an org:")
            for i, o in enumerate(orgs, 1):
                console.print(f"  {i}) {o['login']}")
            idx = int(typer.prompt("Choice", default="1"))
            org = orgs[idx - 1]["login"]
        repo = typer.prompt("Repo (org/name) or leave blank", default="")
        install_config.save_github_install(
            github_login=me["login"], access_token=token.access_token, scope=token.scope,
            org_login=org, repo_full=repo or None, connected_by=admin.id,
        )
        audit.log(user_id=admin.id, action="github.connect", target=org or "(no-org)")
        console.print(f"[green]GitHub connected: {org or '(personal)'} {('/' + repo) if repo else ''}[/green]")
    else:
        audit.log(user_id=admin.id, action="setup.skip_github")
        console.print("Skipped GitHub. You can connect later from /settings/github.")

    console.print("\n[green bold]Setup complete.[/green bold] Run [cyan]edm serve[/cyan] to start the UI.")


@app.command("serve")
def serve(host: str | None = None, port: int | None = None) -> None:
    """Run the FastAPI server (graph view + GitHub webhook)."""
    import uvicorn
    from edm.config import get_settings

    s = get_settings()
    uvicorn.run("edm.api.server:app", host=host or s.host, port=port or s.port, reload=False)


if __name__ == "__main__":
    app()
