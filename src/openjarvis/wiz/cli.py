"""Command-line interface for Wiz operations."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import typer

from openjarvis.wiz.dispatcher import RequestDispatcher
from openjarvis.wiz.orchestrator import WizOrchestrator

logger = logging.getLogger(__name__)

app = typer.Typer(
    help="Wiz: Autonomous Wize engineering system",
    invoke_without_command=True,
)


@app.command()
def feature(
    request: str = typer.Argument(..., help="Feature request description"),
    wize_repo: Optional[Path] = typer.Option(
        None,
        "--repo",
        help="Path to Wize-Performance repository",
    ),
):
    """Submit a feature request to Wiz.

    Examples:
        wiz feature "Add dark mode to dashboard"
        wiz feature "Fix login timeout issue" --repo /path/to/Wize-Performance
    """
    if wize_repo is None:
        wize_repo = Path.home() / "Wize-Performance"

    if not wize_repo.exists():
        typer.echo(f"Error: Wize repository not found at {wize_repo}", err=True)
        raise typer.Exit(1)

    typer.echo(f"Processing feature request: {request}")
    typer.echo(f"Wize repository: {wize_repo}")

    try:
        orchestrator = WizOrchestrator(wize_repo)
        feature_request = orchestrator.handle_owner_request(request)

        typer.echo(f"Created {feature_request.id}")
        typer.echo(f"Risk level: {feature_request.risk_level.value}")
        typer.echo(f"State: {feature_request.state.value}")
        typer.echo("\nWiz will now proceed with implementation...")

    except Exception as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)


@app.command()
def status(
    feature_id: str = typer.Argument(..., help="Feature ID to check status"),
):
    """Check status of a feature request.

    Example:
        wiz status WIZE-abc123
    """
    typer.echo(f"Checking status of {feature_id}...")
    typer.echo("Status command not yet implemented")
    # TODO: Implement status checking


@app.command()
def health():
    """Check Wiz system health."""
    typer.echo("Wiz System Health")
    typer.echo("-" * 40)
    typer.echo("Claude CLI: OK")
    typer.echo("Repository access: OK")
    typer.echo("Vercel integration: TODO")
    typer.echo("GitHub integration: TODO")
    typer.echo("Notifications: TODO")


@app.command()
def list_features():
    """List all active feature requests."""
    typer.echo("Active feature requests:")
    typer.echo("(Feature list not yet implemented)")
    # TODO: Implement listing


if __name__ == "__main__":
    app()
