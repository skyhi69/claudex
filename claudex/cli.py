"""Claudex CLI — the user-facing entry point."""

import argparse
import os
import sys
from pathlib import Path

# Fix Windows Unicode output issues
if sys.platform == "win32":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt, Confirm
from rich.rule import Rule

from .config import load_config
from .orchestrator import Orchestrator


# Claudex install directory (for finding config.yaml and roles/)
CLAUDEX_HOME = Path(__file__).parent.parent

# Projects base directory
PROJECTS_DIR = Path(os.path.expanduser("~")) / "Claude_Projects"

console = Console(force_terminal=True)

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


def pick_project() -> Path:
    """Interactive project picker — list existing projects or create new."""
    console.print()
    console.print(Rule("[bold magenta]Claudex[/bold magenta]", style="magenta"))
    console.print()

    # Scan for existing projects
    projects = []
    if PROJECTS_DIR.exists():
        for item in sorted(PROJECTS_DIR.iterdir()):
            if item.is_dir() and not item.name.startswith("."):
                projects.append(item)

    # Display options
    console.print("[bold]Which project are we working on?[/bold]\n")
    for i, proj in enumerate(projects, 1):
        # Check for CLAUDE.md to show a description
        claude_md = proj / "CLAUDE.md"
        desc = ""
        if claude_md.exists():
            first_line = claude_md.read_text(encoding="utf-8", errors="replace").strip().split("\n")[0]
            desc = f" [dim]— {first_line.lstrip('# ').strip()[:60]}[/dim]"
        console.print(f"  [bold]{i}[/bold]. {proj.name}{desc}")

    console.print(f"  [bold]{len(projects) + 1}[/bold]. [green]Create a new project[/green]")
    console.print(f"  [bold]{len(projects) + 2}[/bold]. [dim]Use a custom directory[/dim]")
    console.print()

    choice = Prompt.ask(
        "Select",
        choices=[str(i) for i in range(1, len(projects) + 3)],
    )
    choice_num = int(choice)

    if choice_num <= len(projects):
        # Existing project
        return projects[choice_num - 1]
    elif choice_num == len(projects) + 1:
        # Create new project
        name = Prompt.ask("Project name")
        new_dir = PROJECTS_DIR / name
        new_dir.mkdir(parents=True, exist_ok=True)
        console.print(f"  Created [bold]{new_dir}[/bold]")
        return new_dir
    else:
        # Custom directory
        custom = Prompt.ask("Directory path")
        custom_path = Path(custom).resolve()
        custom_path.mkdir(parents=True, exist_ok=True)
        return custom_path


def get_task() -> str:
    """Prompt user for the task if not provided as argument."""
    console.print()
    task = Prompt.ask("[bold]What should we build?[/bold]")
    return task


def display_decision_brief(state):
    """Display the final decision brief to the user."""
    brief = state.decision_brief
    if not brief:
        return

    console.print()
    console.print(Rule("Decision Brief", style="bold magenta"))

    if brief.what_was_built:
        console.print(Panel(
            brief.what_was_built,
            title="[bold]What Was Built[/bold]",
            border_style="blue",
        ))

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

    if brief.alternatives_rejected:
        console.print(Panel(
            "\n".join(f"  - {a}" for a in brief.alternatives_rejected[:5]),
            title="[bold]Alternatives Considered & Rejected[/bold]",
            border_style="dim",
        ))

    if brief.unresolved_concerns:
        console.print(Panel(
            "\n".join(f"  ! {c}" for c in brief.unresolved_concerns),
            title="[bold yellow]Unresolved Concerns[/bold yellow]",
            border_style="yellow",
        ))

    if state.audit_results:
        last_audit = state.audit_results[-1]
        status = "[bold green]APPROVED[/bold green]" if last_audit.approved else "[bold red]NOT FULLY APPROVED[/bold red]"
        total_audits = len(state.audit_results)
        console.print(f"\n  Audit status: {status} (after {total_audits} review(s))")

    rounds = len(state.plan.rounds) if state.plan else 0
    iterations = state.resolve_iteration
    console.print(f"  Planning rounds: {rounds} | Fix iterations: {iterations}")

    _display_quota(state)
    console.print()


def _display_quota(state):
    """Show the per-provider quota ledger (subscription usage, never dollars)."""
    us = getattr(state, "usage_summary", None)
    if not us:
        return

    def toks(d):
        return int(d.get("input_tokens", 0)) + int(d.get("output_tokens", 0))

    c = us.get("claude", {})
    x = us.get("codex", {})
    c_tok, x_tok = toks(c), toks(x)
    total = c_tok + x_tok
    if total == 0 and not (c.get("calls") or x.get("calls")):
        return

    codex_share = (x_tok / total * 100) if total else 0
    lines = [
        f"  [cyan]Claude[/cyan]: {c.get('calls', 0)} calls · {c_tok:,} tok "
        f"(cache-read {int(c.get('cached_input_tokens', 0)):,})",
        f"  [green]Codex[/green] : {x.get('calls', 0)} calls · {x_tok:,} tok "
        f"(cache-read {int(x.get('cached_input_tokens', 0)):,})",
        f"  [dim]Codex carried {codex_share:.0f}% of token volume[/dim]",
    ]
    console.print(Panel(
        "\n".join(lines),
        title="[bold]Quota ledger[/bold] [dim](subscription usage, not $)[/dim]",
        border_style="magenta",
    ))


def prompt_approval() -> bool:
    """Ask the user whether to write files to disk."""
    console.print(Rule(style="dim"))
    return Confirm.ask("[bold]Write these files to disk?[/bold]", default=True)


def main(args=None):
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(
        prog="claudex",
        description="Claudex — Claude + Codex collaborative coding agent",
    )
    parser.add_argument(
        "task",
        nargs="?",
        default=None,
        help="The coding task (if omitted, you'll be asked interactively)",
    )
    parser.add_argument(
        "--dir", "-d",
        type=Path,
        default=None,
        help="Target project directory (if omitted, you'll pick interactively)",
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
        default=True,
        help="Show full debate transcripts (default: on)",
    )
    parser.add_argument(
        "--quiet", "-q",
        action="store_true",
        help="Only show status updates, not full debate",
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

    # --- Interactive startup ---

    # Pick project directory
    if parsed.dir:
        target_dir = parsed.dir.resolve()
    else:
        target_dir = pick_project()

    # Get the task
    if parsed.task:
        task = parsed.task
    else:
        task = get_task()

    # --- Config ---
    config_path = parsed.config
    if not config_path:
        candidates = [
            target_dir / "config.yaml",
            CLAUDEX_HOME / "config.yaml",
        ]
        for candidate in candidates:
            if candidate.exists():
                config_path = candidate
                break

    config = load_config(config_path)

    if parsed.max_rounds:
        config.planning_max_rounds = parsed.max_rounds
    if parsed.max_iterations:
        config.resolve_max_iterations = parsed.max_iterations

    # Find roles directory
    roles_dir = CLAUDEX_HOME / "roles"
    if not roles_dir.exists():
        console.print("[red]ERROR: roles/ directory not found.[/red]")
        sys.exit(1)

    # Banner
    console.print()
    console.print(Rule("[bold magenta]Claudex[/bold magenta]", style="magenta"))
    console.print(f"  Task: [bold]{task}[/bold]")
    console.print(f"  Project: [bold]{target_dir}[/bold]")
    console.print()

    # Run the pipeline
    verbose = parsed.verbose and not parsed.quiet
    message_handler = on_message if verbose else _quiet_handler
    orchestrator = Orchestrator(config, roles_dir, on_message=message_handler)

    try:
        state = orchestrator.run(task, target_dir)
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
