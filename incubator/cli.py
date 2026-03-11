"""CLI interface for the incubator pipeline."""

from __future__ import annotations

import asyncio

import typer
from rich.console import Console
from rich.table import Table

from incubator.config import get_settings

app = typer.Typer(name="incubator", help="Multi-agent idea incubation pipeline")
console = Console()


@app.command()
def incubate(
    title: str = typer.Argument(help="Idea title"),
    description: str = typer.Option("", "--desc", "-d", help="Idea description"),
) -> None:
    """Submit a new idea and run it through the pipeline."""
    if not description:
        description = typer.prompt("Describe your idea")

    settings = get_settings()

    async def _run():
        from incubator.orchestrator.orchestrator import Orchestrator

        orch = Orchestrator(settings)
        idea_id = await orch.incubate(title, description)
        console.print(f"[green]Idea '{idea_id}' submitted and pipeline started.[/green]")

    asyncio.run(_run())


@app.command()
def status(idea_id: str = typer.Argument(help="Idea slug")) -> None:
    """Show the status of an idea."""
    settings = get_settings()
    from incubator.core.blackboard import Blackboard

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
    from incubator.core.blackboard import Blackboard

    bb = Blackboard(settings.blackboard_dir)
    ideas = bb.list_ideas()
    if not ideas:
        console.print("[dim]No ideas yet. Use 'incubator incubate' to start.[/dim]")
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
    """Resume a paused idea."""
    settings = get_settings()

    async def _run():
        from incubator.orchestrator.orchestrator import Orchestrator

        orch = Orchestrator(settings)
        await orch.resume(idea_id)
        console.print(f"[green]Resumed '{idea_id}'.[/green]")

    asyncio.run(_run())


@app.command()
def kill(idea_id: str = typer.Argument(help="Idea slug to kill")) -> None:
    """Kill an idea."""
    settings = get_settings()

    async def _run():
        from incubator.orchestrator.orchestrator import Orchestrator

        orch = Orchestrator(settings)
        await orch.kill(idea_id)
        console.print(f"[red]Killed '{idea_id}'.[/red]")

    asyncio.run(_run())


@app.command()
def watch() -> None:
    """Start background watchers (competitive + research)."""
    settings = get_settings()

    async def _run():
        from incubator.orchestrator.orchestrator import Orchestrator
        from incubator.orchestrator.scheduler import Scheduler
        from incubator.agents.watchers.competitive import run_competitive_watcher
        from incubator.agents.watchers.research import run_research_watcher

        orch = Orchestrator(settings)
        scheduler = Scheduler(settings)

        watchers = [
            {
                "name": "competitive",
                "cron": settings.watcher_competitive_cron,
                "callback": lambda: run_competitive_watcher(orch),
            },
            {
                "name": "research",
                "cron": settings.watcher_research_cron,
                "callback": lambda: run_research_watcher(orch),
            },
        ]

        console.print("[green]Starting watchers...[/green]")
        await scheduler.start(watchers)

        try:
            await asyncio.Event().wait()  # Run forever
        except asyncio.CancelledError:
            await scheduler.stop()

    asyncio.run(_run())


@app.command()
def evolve() -> None:
    """Run evolution retrospective on agent learnings."""
    settings = get_settings()

    async def _run():
        from incubator.comms.telegram import TelegramNotifier
        from incubator.comms.notifications import NotificationDispatcher
        from incubator.orchestrator.evolution import EvolutionManager

        telegram = TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id)
        dispatcher = NotificationDispatcher(telegram)
        evo = EvolutionManager(settings.project_root, dispatcher)
        await evo.run_retrospective()

    asyncio.run(_run())


@app.command()
def run() -> None:
    """Start the worker pool (no web UI)."""
    settings = get_settings()

    async def _run():
        from incubator.orchestrator.pool import PoolManager

        pool = PoolManager(settings)
        console.print("[green]Starting worker pool...[/green]")
        console.print(f"[dim]Pool size: {settings.pool_size}, cycle time: {settings.cycle_time_minutes}m[/dim]")
        console.print("[dim]Press Ctrl+C to stop.[/dim]")
        await pool.run()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        console.print("\n[yellow]Pool stopped.[/yellow]")


@app.command()
def serve(
    host: str = typer.Option(None, help="Host to bind to"),
    port: int = typer.Option(None, help="Port to bind to"),
    no_pool: bool = typer.Option(False, "--no-pool", help="Disable worker pool"),
) -> None:
    """Start the web dashboard (and worker pool by default)."""
    settings = get_settings()
    import uvicorn

    if not no_pool:
        from incubator.web.api.app import set_pool_enabled
        set_pool_enabled(True)

    uvicorn.run(
        "incubator.web.api.app:create_app",
        host=host or settings.web_host,
        port=port or settings.web_port,
        factory=True,
        reload=False,
    )


if __name__ == "__main__":
    app()
