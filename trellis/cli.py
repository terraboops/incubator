"""CLI interface for the Trellis pipeline."""

from __future__ import annotations

import asyncio
import os
import signal
import time

import typer
from rich.console import Console
from rich.table import Table

from importlib.metadata import version as pkg_version

from trellis.config import get_settings


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(pkg_version("trellis"))
        raise typer.Exit()


app = typer.Typer(
    name="trellis",
    help="Agentic pipeline platform — design agent teams that take ideas from concept to launch",
)


@app.callback(invoke_without_command=True)
def main(
    version: bool = typer.Option(
        False, "--version", "-v", callback=_version_callback, is_eager=True, help="Show version"
    ),
) -> None:
    pass


console = Console()


@app.command()
def init(
    directory: str = typer.Argument(".", help="Directory to create project in"),
    force: bool = typer.Option(False, "--force", help="Overwrite existing project"),
) -> None:
    """Scaffold a new Trellis project directory."""
    import json
    import shutil
    from datetime import datetime, timezone
    from pathlib import Path

    from trellis.config import Settings

    target = Path(directory).resolve()
    marker = target / ".trellis"

    if marker.exists() and not force:
        console.print(f"[red]Already a Trellis project: {target}[/red]")
        console.print("[dim]Use --force to overwrite.[/dim]")
        raise typer.Exit(1)

    settings = Settings()
    defaults = settings.defaults_dir

    if not defaults.exists():
        console.print("[red]Package defaults not found. Reinstall trellis.[/red]")
        raise typer.Exit(1)

    target.mkdir(parents=True, exist_ok=True)

    for item in defaults.iterdir():
        dest = target / item.name
        if item.is_dir():
            shutil.copytree(item, dest, dirs_exist_ok=True)
        else:
            shutil.copy2(item, dest)

    (target / "workspace").mkdir(exist_ok=True)
    (target / "pool").mkdir(exist_ok=True)

    marker.write_text(
        json.dumps(
            {
                "version": "0.2.0",
                "created": datetime.now(timezone.utc).isoformat(),
            },
            indent=2,
        )
    )

    console.print(f"[green]Created Trellis project in {target}[/green]")
    try:
        rel = target.relative_to(Path.cwd())
    except ValueError:
        rel = target
    if rel != Path("."):
        console.print(f"\n  cd {rel}")
    console.print("  trellis serve          # start the web dashboard")
    console.print('  trellis incubate "your idea here"')


@app.command()
def incubate(
    title: str = typer.Argument(help="Idea title"),
    description: str = typer.Option("", "--desc", "-d", help="Idea description"),
) -> None:
    """Submit a new idea and start the pool to process it."""
    if not description:
        description = typer.prompt("Describe your idea")

    settings = get_settings()

    async def _run():
        from trellis.orchestrator.orchestrator import Orchestrator
        from trellis.orchestrator.pool import PoolManager

        orch = Orchestrator(settings)
        idea_id = await orch.incubate(title, description)
        console.print(f"[green]Idea '{idea_id}' submitted.[/green]")

        pool = PoolManager(settings)
        console.print("[dim]Starting pool to process idea...[/dim]")
        console.print("[dim]Press Ctrl+C to stop.[/dim]")
        await pool.run()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        console.print("\n[yellow]Pool stopped.[/yellow]")


@app.command()
def status(idea_id: str = typer.Argument(help="Idea slug")) -> None:
    """Show the status of an idea."""
    settings = get_settings()
    from trellis.core.blackboard import Blackboard

    bb = Blackboard(settings.blackboard_dir)
    try:
        s = bb.get_status(idea_id)
    except FileNotFoundError:
        console.print(f"[red]Idea '{idea_id}' not found.[/red]")
        raise typer.Exit(1)

    console.print(f"[bold]{s['title']}[/bold] ({s['id']})")
    console.print(f"Phase: [cyan]{s['phase']}[/cyan]")
    console.print(f"Cost: ${s.get('total_cost_usd', 0):.2f}")
    console.print(f"Iterations: {s.get('iteration_count', 0)}")
    if s.get("phase_recommendation"):
        console.print(f"Recommendation: {s['phase_recommendation']}")


@app.command(name="list")
def list_ideas() -> None:
    """List all ideas."""
    settings = get_settings()
    from trellis.core.blackboard import Blackboard

    bb = Blackboard(settings.blackboard_dir)
    ideas = bb.list_ideas()
    if not ideas:
        console.print("[dim]No ideas yet. Use 'trellis incubate' to start.[/dim]")
        return

    table = Table(title="Ideas")
    table.add_column("ID", style="cyan")
    table.add_column("Title")
    table.add_column("Phase", style="green")
    table.add_column("Cost", justify="right")

    for idea_id in sorted(ideas):
        s = bb.get_status(idea_id)
        table.add_row(s["id"], s["title"], s["phase"], f"${s.get('total_cost_usd', 0):.2f}")

    console.print(table)


@app.command()
def resume(idea_id: str = typer.Argument(help="Idea slug to resume")) -> None:
    """Resume a paused idea (pool will pick it up)."""
    settings = get_settings()

    async def _run():
        from trellis.orchestrator.orchestrator import Orchestrator

        orch = Orchestrator(settings)
        await orch.resume(idea_id)
        console.print(f"[green]Resumed '{idea_id}'. Pool will pick it up on next cycle.[/green]")

    asyncio.run(_run())


@app.command()
def retry(
    idea_id: str = typer.Argument(help="Idea slug"),
    role: str = typer.Argument(help="Role name whose `failed` state to clear"),
    note: str = typer.Option("", "--note", "-n", help="Reason for retry (logged)"),
) -> None:
    """Reset a failed role to pending so the scheduler re-queues it."""
    import os

    from trellis.core.blackboard import Blackboard

    settings = get_settings()
    bb = Blackboard(settings.blackboard_dir)
    try:
        bb.clear_failed_role(
            idea_id,
            role,
            actor=f"human:{os.environ.get('USER', 'cli')}",
            note=note or None,
        )
    except (FileNotFoundError, ValueError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    console.print(f"[green]Reset '{role}' on '{idea_id}' to pending.[/green]")


@app.command()
def kill(idea_id: str = typer.Argument(help="Idea slug to kill")) -> None:
    """Kill an idea."""
    settings = get_settings()

    async def _run():
        from trellis.orchestrator.orchestrator import Orchestrator

        orch = Orchestrator(settings)
        await orch.kill(idea_id)
        console.print(f"[red]Killed '{idea_id}'.[/red]")

    asyncio.run(_run())


@app.command()
def evolve(
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show what would change without applying"
    ),
    agent: str = typer.Option("", "--agent", help="Curate a single agent's knowledge"),
    no_llm: bool = typer.Option(False, "--no-llm", help="Print stats only, no LLM curation"),
) -> None:
    """Run LLM-powered knowledge curation on agent learnings."""
    settings = get_settings()

    async def _run():
        from trellis.orchestrator.evolution import EvolutionManager

        dispatcher = None
        if not no_llm and not dry_run:
            try:
                from trellis.comms.telegram import TelegramNotifier
                from trellis.comms.notifications import NotificationDispatcher

                telegram = TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id)
                dispatcher = NotificationDispatcher(telegram)
            except Exception:
                console.print("[dim]Telegram not configured, running without approval gate.[/dim]")

        evo = EvolutionManager(settings.project_root, dispatcher)

        if no_llm:
            stats = evo.get_stats(agent_filter=agent or None)
            if not stats:
                console.print("[dim]No knowledge entries found.[/dim]")
                return
            table = Table(title="Knowledge Stats")
            table.add_column("Agent")
            table.add_column("Entries", justify="right")
            table.add_column("No Justification", justify="right")
            table.add_column("No Predicates", justify="right")
            for a, s in stats.items():
                table.add_row(
                    a, str(s["count"]), str(s["no_justification"]), str(s["no_predicates"])
                )
            console.print(table)
            return

        actions = await evo.run_retrospective(
            agent_filter=agent or None,
            dry_run=dry_run,
        )

        if not actions:
            console.print("[dim]No curation actions taken.[/dim]")
        else:
            for a, acts in actions.items():
                keeps = sum(1 for x in acts if x.get("action") == "keep")
                merges = sum(1 for x in acts if x.get("action") == "merge")
                drops = sum(1 for x in acts if x.get("action") == "drop")
                console.print(f"[bold]{a}[/bold]: {keeps} kept, {merges} merged, {drops} dropped")

    asyncio.run(_run())


@app.command(name="migrate-knowledge")
def migrate_knowledge(
    registry: str = typer.Option("", "--registry", help="Project root path (defaults to CWD)"),
) -> None:
    """Migrate learnings.md files to structured Knowledge Objects."""
    from pathlib import Path
    from trellis.tools.knowledge_io import migrate_md_to_objects

    project_root = Path(registry) if registry else get_settings().project_root
    agents_dir = project_root / "agents"

    if not agents_dir.exists():
        console.print(f"[red]No agents/ directory at {project_root}[/red]")
        raise typer.Exit(1)

    total = 0
    for agent_dir in sorted(agents_dir.iterdir()):
        if not agent_dir.is_dir() or agent_dir.name.startswith("_"):
            continue
        knowledge_dir = agent_dir / "knowledge"
        if not (knowledge_dir / "learnings.md").exists():
            continue
        count = migrate_md_to_objects(knowledge_dir)
        if count > 0:
            console.print(
                f"  [green]{agent_dir.name}[/green]: {count} objects created, original backed up to learnings.md.bak"
            )
            total += count
        else:
            console.print(f"  [dim]{agent_dir.name}[/dim]: no sections found")

    if total:
        console.print(f"\n[bold green]{total} total knowledge objects created.[/bold green]")
    else:
        console.print("[dim]No learnings.md files found to migrate.[/dim]")


@app.command()
def run(
    human_readable: str = typer.Option(
        None,
        "--human-readable",
        help=(
            "LLM-narrated log output. Optionally specify a format name "
            "(default: explanatory). Use --list-log-formats to see all formats."
        ),
        is_flag=False,
        flag_value="explanatory",
    ),
    list_log_formats: bool = typer.Option(
        False, "--list-log-formats", help="List available human-readable log formats and exit."
    ),
) -> None:
    """Start the worker pool (no web UI)."""
    if list_log_formats:
        _print_log_formats()
        return

    from trellis.log_setup import configure_logging

    configure_logging(human_readable=human_readable)

    settings = get_settings()

    async def _run():
        from trellis.orchestrator.pool import PoolManager

        pool = PoolManager(settings)
        console.print("[green]Starting worker pool...[/green]")
        console.print(
            f"[dim]Pool size: {settings.pool_size}, job timeout: {settings.job_timeout_minutes}m[/dim]"
        )
        console.print("[dim]Press Ctrl+C to stop.[/dim]")
        await pool.run()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        console.print("\n[yellow]Pool stopped.[/yellow]")


def _start_daemon(settings, host, port, no_pool):
    """Relaunch trellis serve in the background."""
    import subprocess
    import sys

    pool_dir = settings.project_root / "pool"
    pool_dir.mkdir(exist_ok=True)
    log_path = pool_dir / "trellis.log"
    pid_path = pool_dir / "trellis.pid"

    cmd = [sys.executable, "-m", "trellis.cli", "serve"]
    if host:
        cmd += ["--host", host]
    if port:
        cmd += ["--port", str(port)]
    if no_pool:
        cmd.append("--no-pool")

    log_file = open(log_path, "a")
    proc = subprocess.Popen(cmd, stdout=log_file, stderr=log_file, start_new_session=True)
    pid_path.write_text(str(proc.pid))
    console.print(f"[green]Started trellis (PID {proc.pid})[/green]")
    console.print(f"  Log: {log_path}")
    console.print("  Stop: trellis serve --stop")


def _stop_daemon(settings):
    """Stop a backgrounded trellis serve."""
    pid_path = settings.project_root / "pool" / "trellis.pid"
    if not pid_path.exists():
        console.print("[yellow]No PID file found. Is trellis running?[/yellow]")
        raise typer.Exit(1)

    pid = int(pid_path.read_text().strip())
    try:
        os.kill(pid, signal.SIGTERM)
        console.print(f"Sent SIGTERM to PID {pid}, waiting...")
    except OSError:
        console.print(f"[yellow]Process {pid} not running. Cleaning up.[/yellow]")
        pid_path.unlink(missing_ok=True)
        return

    for _ in range(10):
        time.sleep(1)
        try:
            os.kill(pid, 0)
        except OSError:
            console.print("[green]Stopped.[/green]")
            pid_path.unlink(missing_ok=True)
            return

    console.print("[yellow]Still running after 10s, sending SIGKILL...[/yellow]")
    os.kill(pid, signal.SIGKILL)
    pid_path.unlink(missing_ok=True)
    console.print("[green]Killed.[/green]")


def _print_log_formats() -> None:
    from trellis.human_log import list_formats

    fmts = list_formats()
    table = Table(title="Human-readable log formats")
    table.add_column("Name", style="cyan")
    table.add_column("Description")
    for name, desc in sorted(fmts.items()):
        table.add_row(name, desc)
    console.print(table)
    console.print("\n[dim]Add custom formats as log-formats/<name>.md in your project root.[/dim]")


@app.command()
def serve(
    host: str = typer.Option(None, help="Host to bind to"),
    port: int = typer.Option(None, help="Port to bind to"),
    no_pool: bool = typer.Option(False, "--no-pool", help="Disable worker pool"),
    background: bool = typer.Option(False, "--background", help="Run as background daemon"),
    stop: bool = typer.Option(False, "--stop", help="Stop background daemon"),
    human_readable: str = typer.Option(
        None,
        "--human-readable",
        help=(
            "LLM-narrated log output. Optionally specify a format name "
            "(default: explanatory). Use --list-log-formats to see all formats."
        ),
        is_flag=False,
        flag_value="explanatory",
    ),
    list_log_formats: bool = typer.Option(
        False, "--list-log-formats", help="List available human-readable log formats and exit."
    ),
) -> None:
    """Start the web dashboard (and worker pool by default)."""
    if list_log_formats:
        _print_log_formats()
        return

    settings = get_settings()

    if stop:
        _stop_daemon(settings)
        return

    if background:
        _start_daemon(settings, host, port, no_pool)
        return

    from trellis.log_setup import configure_logging

    configure_logging(human_readable=human_readable)

    import uvicorn

    from trellis.web.api.app import set_pool_enabled, create_app

    if not no_pool:
        set_pool_enabled(True)

    uvicorn.run(
        create_app(),
        host=host or settings.web_host,
        port=port or settings.web_port,
        log_config=None,  # suppress uvicorn's own logging config
    )


# --- Agent subcommands ---

agent_app = typer.Typer(name="agent", help="Agent management commands")
app.add_typer(agent_app)


@agent_app.command()
def upgrade(
    all_: bool = typer.Option(False, "--all", help="Accept all changes without prompting"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show changes without applying"),
) -> None:
    """Update agent configs from the installed package version."""
    import difflib
    import shutil
    import sys

    from trellis.config import Settings, find_project_root

    settings = Settings()
    defaults_agents = settings.defaults_dir / "agents"
    try:
        project_root = find_project_root()
    except FileNotFoundError:
        console.print("[red]Not a Trellis project. Run 'trellis init' first.[/red]")
        raise typer.Exit(1)
    project_agents = project_root / "agents"

    if not project_agents.exists():
        console.print("[red]No agents/ directory. Is this a Trellis project?[/red]")
        raise typer.Exit(1)

    is_tty = sys.stdin.isatty() and not dry_run

    # Updatable files (never touch knowledge/learnings.md or .claude/ sessions)
    UPDATABLE = {"prompt.py", "CLAUDE.md"}

    for default_agent in sorted(defaults_agents.iterdir()):
        if not default_agent.is_dir():
            continue
        name = default_agent.name
        project_agent = project_agents / name

        if not project_agent.exists():
            console.print(f"\n[cyan]New agent: {name}[/cyan]")
            if dry_run:
                console.print("  Would add (--dry-run)")
                continue
            if all_ or (is_tty and typer.confirm(f"  Add {name}?")):
                shutil.copytree(default_agent, project_agent)
                console.print(f"  [green]Added {name}[/green]")
            continue

        # Compare updatable files
        for default_file in default_agent.rglob("*"):
            if not default_file.is_file():
                continue
            rel = default_file.relative_to(default_agent)
            # Skip non-updatable
            if rel.name == "learnings.md":
                continue
            if ".claude" in rel.parts and rel.name != "CLAUDE.md":
                continue
            if rel.name not in UPDATABLE:
                continue

            project_file = project_agent / rel
            if not project_file.exists():
                continue

            default_content = default_file.read_text()
            project_content = project_file.read_text()
            if default_content == project_content:
                continue

            diff = difflib.unified_diff(
                project_content.splitlines(keepends=True),
                default_content.splitlines(keepends=True),
                fromfile=f"project/{name}/{rel}",
                tofile=f"package/{name}/{rel}",
            )
            diff_text = "".join(diff)

            console.print(f"\n[yellow]{name}/{rel}[/yellow] has changes:")
            if dry_run:
                console.print(diff_text)
                continue
            if all_:
                project_file.write_text(default_content)
                console.print("  [green]Updated[/green]")
            elif is_tty:
                console.print(diff_text)
                if typer.confirm("  Apply this change?"):
                    project_file.write_text(default_content)
                    console.print("  [green]Updated[/green]")

    console.print("\n[green]Agent upgrade complete.[/green]")


@app.command(name="pipelines-to-prose")
def pipelines_to_prose(
    dry_run: bool = typer.Option(False, "--dry-run", help="Show generated .prose without writing"),
) -> None:
    """Convert YAML pipeline templates to Prose format."""
    from pathlib import Path

    from trellis.config import find_project_root
    from trellis.core.pipeline_format import load_pipeline, save_pipeline
    from trellis.core.prose_parser import emit_pipeline_prose

    try:
        project_root = find_project_root()
    except FileNotFoundError:
        console.print("[red]Not a Trellis project. Run 'trellis init' first.[/red]")
        raise typer.Exit(1)

    templates_dir = project_root / "pipeline-templates"
    if not templates_dir.exists():
        console.print("[dim]No pipeline-templates/ directory found.[/dim]")
        return

    yaml_files = sorted(list(templates_dir.glob("*.yaml")) + list(templates_dir.glob("*.yml")))
    if not yaml_files:
        console.print("[dim]No YAML pipeline templates found.[/dim]")
        return

    for yaml_path in yaml_files:
        data = load_pipeline(yaml_path)
        prose_text = emit_pipeline_prose(data)
        prose_path = templates_dir / f"{yaml_path.stem}.prose"

        if dry_run:
            console.print(f"\n[bold]{yaml_path.name}[/bold] → {prose_path.name}:")
            console.print(f"[dim]{prose_text}[/dim]")
        else:
            save_pipeline(prose_path, data, fmt="prose")
            bak_path = yaml_path.with_suffix(yaml_path.suffix + ".bak")
            yaml_path.rename(bak_path)
            console.print(f"  [green]✓[/green] {yaml_path.name} → {prose_path.name} (original → {bak_path.name})")

    if not dry_run:
        console.print(f"\n[bold green]{len(yaml_files)} template(s) converted.[/bold green]")


@app.command()
def migrate(
    registry: str = typer.Option(
        "", "--registry", "-r", help="Path to registry.yaml (default: project registry)"
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show what would change without applying"
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Auto-apply mechanical migrations without prompting"
    ),
) -> None:
    """Check and apply registry.yaml migrations for new Trellis versions."""
    import sys
    from pathlib import Path
    from trellis.config import find_project_root
    from trellis.core.migrations import check_all, load_registry_data, run_migrations

    if registry:
        registry_path = Path(registry)
    else:
        try:
            project_root = find_project_root()
            registry_path = project_root / "registry.yaml"
        except FileNotFoundError:
            console.print("[red]Not a Trellis project. Run 'trellis init' first.[/red]")
            raise typer.Exit(1)

    if not registry_path.exists():
        console.print(f"[red]Registry not found: {registry_path}[/red]")
        raise typer.Exit(1)

    data = load_registry_data(registry_path)
    needed = check_all(data)

    if not needed:
        console.print("[green]✓ Registry is up to date — no migrations needed.[/green]")
        return

    console.print(f"[yellow]{len(needed)} migration(s) needed for {registry_path}:[/yellow]\n")
    for migration, check in needed:
        badge = "[cyan][LLM][/cyan]" if migration.llm_assisted else "[dim][auto][/dim]"
        console.print(f"  {badge} [bold]{migration.version}[/bold] — {migration.description}")
        if check.affected_agents:
            console.print(f"       Affects: {', '.join(check.affected_agents)}")
    console.print("")

    if dry_run:
        console.print("[dim]--dry-run: no changes written.[/dim]")
        return

    is_tty = sys.stdin.isatty()

    async def confirm(action: str, details: str) -> bool:
        console.print(f"\n[cyan]{action}[/cyan]")
        console.print(details)
        if yes and not details.startswith("LLM"):
            return True
        if is_tty:
            return typer.confirm("Apply this migration?")
        return False

    async def _run():
        results = await run_migrations(
            registry_path=registry_path,
            confirm=confirm,
            dry_run=False,
            auto_yes=yes,
        )
        for result in results:
            if result.success:
                if result.agents_modified:
                    console.print(f"[green]✓ {result.message}[/green]")
                else:
                    console.print(f"[dim]  {result.message}[/dim]")
            else:
                console.print(f"[red]✗ {result.message}[/red]")
                if result.errors:
                    for err in result.errors:
                        console.print(f"  [red]{err}[/red]")

    asyncio.run(_run())
    console.print("\n[green]Migration complete.[/green]")


@app.command(name="migrate-project")
def migrate_project(
    directory: str = typer.Argument(".", help="Project directory to migrate"),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show what would change without applying"
    ),
) -> None:
    """Migrate an incubator project to Trellis.

    Renames .incubator → .trellis, updates pool files, registry paths,
    and reinstalls the package in the project venv if one exists.
    """
    import subprocess
    from pathlib import Path

    target = Path(directory).resolve()
    old_marker = target / ".incubator"
    new_marker = target / ".trellis"

    if new_marker.exists():
        console.print(f"[green]Already a Trellis project: {target}[/green]")
        raise typer.Exit(0)

    if not old_marker.exists():
        console.print(f"[red]No .incubator marker found in {target}[/red]")
        console.print("[dim]This doesn't look like an incubator project.[/dim]")
        raise typer.Exit(1)

    steps = []

    # 1. Rename marker
    steps.append((".incubator → .trellis", old_marker, new_marker))

    # 2. Pool files
    pool_dir = target / "pool"
    old_log = pool_dir / "incubator.log"
    new_log = pool_dir / "trellis.log"
    old_pid = pool_dir / "incubator.pid"
    new_pid = pool_dir / "trellis.pid"

    # 3. Registry claude_home paths
    registry_path = target / "registry.yaml"
    registry_needs_update = False
    if registry_path.exists():
        registry_text = registry_path.read_text()
        if "incubator/agents" in registry_text:
            registry_needs_update = True

    # 4. Venv
    venv_dir = target / ".venv"
    venv_python = venv_dir / "bin" / "python"
    has_venv = venv_python.exists()

    # Show plan
    console.print(f"\n[bold]Migrating {target}[/bold]\n")
    console.print("  [cyan]1.[/cyan] Rename .incubator → .trellis")
    if old_log.exists():
        console.print("  [cyan]2.[/cyan] Rename pool/incubator.log → pool/trellis.log")
    if old_pid.exists():
        console.print("  [cyan]3.[/cyan] Rename pool/incubator.pid → pool/trellis.pid")
        console.print("     [yellow]Stop any running incubator serve first![/yellow]")
    if registry_needs_update:
        console.print("  [cyan]4.[/cyan] Update registry.yaml claude_home paths")
    if has_venv:
        console.print("  [cyan]5.[/cyan] Reinstall trellis in .venv (replaces incubator package)")
    console.print("")

    if dry_run:
        console.print("[dim]--dry-run: no changes applied.[/dim]")
        return

    # Execute
    # 1. Marker
    old_marker.rename(new_marker)
    console.print("  [green]✓[/green] .incubator → .trellis")

    # 2. Pool log
    if old_log.exists():
        old_log.rename(new_log)
        console.print("  [green]✓[/green] pool/incubator.log → pool/trellis.log")

    # 3. Pool PID
    if old_pid.exists():
        old_pid.rename(new_pid)
        console.print("  [green]✓[/green] pool/incubator.pid → pool/trellis.pid")

    # 4. Registry
    if registry_needs_update:
        updated = registry_text.replace("incubator/agents", "trellis/agents")
        registry_path.write_text(updated)
        console.print("  [green]✓[/green] registry.yaml claude_home paths updated")

    # 5. Venv
    if has_venv:
        console.print("  [dim]Reinstalling trellis in .venv...[/dim]")
        # Find where trellis is installed from (this running package)
        package_root = Path(__file__).resolve().parent.parent
        result = subprocess.run(
            [str(venv_python), "-m", "pip", "install", "--quiet", str(package_root)],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            console.print("  [green]✓[/green] trellis installed in .venv")
            # Uninstall old incubator package if present
            subprocess.run(
                [str(venv_python), "-m", "pip", "uninstall", "-y", "--quiet", "incubator"],
                capture_output=True,
                text=True,
            )
        else:
            console.print("  [yellow]⚠ venv install failed — run manually:[/yellow]")
            console.print("    pip install trellis")
            if result.stderr:
                for line in result.stderr.strip().split("\n")[-3:]:
                    console.print(f"    [dim]{line}[/dim]")

    console.print("\n[bold green]Migration complete.[/bold green]")
    console.print(f"\n  cd {target}")
    console.print("  trellis serve")


if __name__ == "__main__":
    app()
