"""Claudex CLI — the user-facing entry point."""

import argparse
import sys
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.text import Text
from rich.rule import Rule

from .config import load_config
from .orchestrator import Orchestrator


console = Console()

# Agent display colors
AGENT_STYLES = {
    "claude": "bold cyan",
    "codex": "bold green",
    "system": "bold yellow",
}

AGENT_LABELS = {
    "claude": "Claude",
    "codex": "Codex",
    "system": "Claudex",
}


def on_message(agent: str, role: str, content: str):
    """Callback to display agent messages in the terminal."""
    style = AGENT_STYLES.get(agent, "white")
    label = AGENT_LABELS.get(agent, agent)

    if agent == "system":
        console.print(f"\n[{style}][{label}][/{style}] {content}")
    else:
        header = f"{label} — {role}"
        console.print()
        console.print(Panel(
            content,
            title=f"[{style}]{header}[/{style}]",
            border_style=style.replace("bold ", ""),
            padding=(1, 2),
        ))


def display_decision_brief(state):
    """Display the final decision brief to the user."""
    brief = state.decision_brief
    if not brief:
        return

    console.print()
    console.print(Rule("Decision Brief", style="bold magenta"))

    # What was built
    if brief.what_was_built:
        console.print(Panel(
            brief.what_was_built,
            title="[bold]What Was Built[/bold]",
            border_style="blue",
        ))

    # Files to be written
    if brief.files_summary:
        file_lines = []
        for f in brief.files_summary:
            symbol = "+" if f["action"] == "create" else "~" if f["action"] == "modify" else "-"
            file_lines.append(f"  {symbol} {f['path']} ({f['lines']} lines, {f['action']})")
        console.print(Panel(
            "\n".join(file_lines),
            title="[bold]Files[/bold]",
            border_style="green",
        ))

    # Alternatives rejected
    if brief.alternatives_rejected:
        console.print(Panel(
            "\n".join(f"  - {a}" for a in brief.alternatives_rejected[:5]),
            title="[bold]Alternatives Considered & Rejected[/bold]",
            border_style="dim",
        ))

    # Unresolved concerns
    if brief.unresolved_concerns:
        console.print(Panel(
            "\n".join(f"  ! {c}" for c in brief.unresolved_concerns),
            title="[bold yellow]Unresolved Concerns[/bold yellow]",
            border_style="yellow",
        ))

    # Audit status
    if state.audit_results:
        last_audit = state.audit_results[-1]
        status = "[bold green]APPROVED[/bold green]" if last_audit.approved else "[bold red]NOT FULLY APPROVED[/bold red]"
        total_audits = len(state.audit_results)
        console.print(f"\n  Audit status: {status} (after {total_audits} review(s))")

    # Session stats
    rounds = len(state.plan.rounds) if state.plan else 0
    iterations = state.resolve_iteration
    console.print(f"  Planning rounds: {rounds} | Fix iterations: {iterations}")
    console.print()


def prompt_approval() -> bool:
    """Ask the user whether to write files to disk."""
    console.print(Rule(style="dim"))
    response = console.input("[bold]Write these files to disk? [/bold][dim](y/n)[/dim] ").strip().lower()
    return response in ("y", "yes")


def main(args=None):
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(
        prog="claudex",
        description="Claudex — Claude + Codex collaborative coding agent",
    )
    parser.add_argument(
        "task",
        help="The coding task to accomplish",
    )
    parser.add_argument(
        "--dir", "-d",
        type=Path,
        default=Path.cwd(),
        help="Target project directory (default: current directory)",
    )
    parser.add_argument(
        "--config", "-c",
        type=Path,
        default=None,
        help="Path to config.yaml (default: auto-detect)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Show full debate transcripts",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run debate + code + audit but don't write files",
    )
    parser.add_argument(
        "--max-rounds",
        type=int,
        default=None,
        help="Override max planning rounds",
    )
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=None,
        help="Override max resolution iterations",
    )

    parsed = parser.parse_args(args)

    # Find config file
    config_path = parsed.config
    if not config_path:
        # Look for config.yaml in project dir, then claudex install dir
        candidates = [
            parsed.dir / "config.yaml",
            Path(__file__).parent.parent / "config.yaml",
        ]
        for candidate in candidates:
            if candidate.exists():
                config_path = candidate
                break

    config = load_config(config_path)

    # Apply CLI overrides
    if parsed.max_rounds:
        config.planning_max_rounds = parsed.max_rounds
    if parsed.max_iterations:
        config.resolve_max_iterations = parsed.max_iterations

    # Find roles directory
    roles_dir = Path(__file__).parent.parent / "roles"
    if not roles_dir.exists():
        console.print("[red]ERROR: roles/ directory not found.[/red]")
        sys.exit(1)

    # Banner
    console.print()
    console.print(Rule("[bold magenta]Claudex[/bold magenta]", style="magenta"))
    console.print(f"  Task: [bold]{parsed.task}[/bold]")
    console.print(f"  Target: {parsed.dir.resolve()}")
    console.print()

    # Run the pipeline
    message_handler = on_message if parsed.verbose else _quiet_handler
    orchestrator = Orchestrator(config, roles_dir, on_message=message_handler)

    try:
        state = orchestrator.run(parsed.task, parsed.dir.resolve())
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted by user.[/yellow]")
        sys.exit(1)

    # Display decision brief
    display_decision_brief(state)

    # Write files (unless dry-run)
    if parsed.dry_run:
        console.print("[dim]Dry run — no files written.[/dim]")
    elif state.code_result and state.code_result.files:
        if config.require_approval:
            if prompt_approval():
                summaries = orchestrator.write_approved_files(state)
                console.print("[bold green]Files written:[/bold green]")
                for s in summaries:
                    console.print(s)
            else:
                console.print("[dim]No files written.[/dim]")
        else:
            summaries = orchestrator.write_approved_files(state)
            console.print("[bold green]Files written:[/bold green]")
            for s in summaries:
                console.print(s)
    else:
        console.print("[dim]No files to write.[/dim]")

    console.print()
    console.print(f"[dim]Session {state.session_id} complete.[/dim]")


def _quiet_handler(agent: str, role: str, content: str):
    """Minimal output handler — only shows system messages."""
    if agent == "system":
        on_message(agent, role, content)


if __name__ == "__main__":
    main()
