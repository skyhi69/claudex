"""Memory system for Claudex — session logs, lessons, and project context."""

from __future__ import annotations

import json
import os
import yaml
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

from .models import SessionState, DebateRound

# Default directories
CLAUDEX_HOME = Path(os.path.expanduser("~")) / ".claudex"
SESSIONS_DIR = CLAUDEX_HOME / "sessions"
LESSONS_FILE = CLAUDEX_HOME / "lessons.yaml"


def ensure_dirs():
    """Create memory directories if they don't exist."""
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)


def save_session(state: SessionState) -> Path:
    """Save a session log after a Claudex run.

    Saves the debate transcript, code generated, audit results,
    and decisions made to a JSON file.
    """
    ensure_dirs()

    session_data = {
        "session_id": state.session_id,
        "timestamp": datetime.now().isoformat(),
        "task": state.task,
        "target_dir": str(state.target_dir),
        "complexity": state.analysis.complexity if state.analysis else "unknown",
        "expertise": state.analysis.required_expertise if state.analysis else [],
        "planning": {
            "rounds": len(state.plan.rounds) if state.plan else 0,
            "consensus_reached": (
                state.plan.rounds[-1].consensus_reached
                if state.plan and state.plan.rounds else False
            ),
            "agreed_plan": state.plan.agreed_plan[:2000] if state.plan else "",
            "alternatives_rejected": state.plan.alternatives_rejected if state.plan else [],
        },
        "code": {
            "files_generated": [
                {"path": f.path, "action": f.action, "lines": f.content.count("\n") + 1}
                for f in (state.code_result.files if state.code_result else [])
            ],
            "explanation": state.code_result.explanation if state.code_result else "",
        },
        "audit": {
            "total_reviews": len(state.audit_results),
            "final_approved": (
                state.audit_results[-1].approved
                if state.audit_results else False
            ),
            "issues_found": [
                {"severity": i.severity, "issue": i.issue}
                for audit in state.audit_results
                for i in audit.issues
            ],
        },
        "resolve_iterations": state.resolve_iteration,
        "transcript": [
            {
                "agent": msg.agent,
                "role": msg.role,
                "content": msg.content[:500],  # truncate for storage
            }
            for msg in state.transcript
        ],
    }

    filename = f"{state.session_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    filepath = SESSIONS_DIR / filename

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(session_data, f, indent=2, ensure_ascii=False)

    return filepath


def load_lessons() -> dict:
    """Load lessons from the lessons file.

    Returns a dict with:
        - patterns: list of things that worked well
        - antipatterns: list of things that didn't work
        - project_notes: dict of per-project notes
    """
    if not LESSONS_FILE.exists():
        return {"patterns": [], "antipatterns": [], "project_notes": {}}

    with open(LESSONS_FILE, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    return {
        "patterns": data.get("patterns", []),
        "antipatterns": data.get("antipatterns", []),
        "project_notes": data.get("project_notes", {}),
    }


def save_lessons(lessons: dict):
    """Save lessons to the lessons file."""
    ensure_dirs()

    with open(LESSONS_FILE, "w", encoding="utf-8") as f:
        yaml.dump(lessons, f, default_flow_style=False, allow_unicode=True, sort_keys=False)


def add_lesson(category: str, lesson: str, project: str = ""):
    """Add a single lesson learned.

    Args:
        category: "pattern" (what worked) or "antipattern" (what didn't)
        lesson: The lesson text
        project: Optional project name for project-specific notes
    """
    lessons = load_lessons()

    entry = {
        "lesson": lesson,
        "date": datetime.now().strftime("%Y-%m-%d"),
        "project": project or "general",
    }

    if category == "pattern":
        lessons["patterns"].append(entry)
    elif category == "antipattern":
        lessons["antipatterns"].append(entry)

    # Keep last 50 lessons per category
    lessons["patterns"] = lessons["patterns"][-50:]
    lessons["antipatterns"] = lessons["antipatterns"][-50:]

    save_lessons(lessons)


def auto_learn(state: SessionState):
    """Automatically extract lessons from a completed session.

    Called after each Claudex run to learn from what happened.
    """
    project_name = state.target_dir.name if state.target_dir else "unknown"

    # Learn from audit results
    if state.audit_results:
        last_audit = state.audit_results[-1]
        if last_audit.approved and len(state.audit_results) == 1:
            add_lesson("pattern", f"Code approved on first audit for: {state.task[:100]}", project_name)
        elif not last_audit.approved:
            for issue in last_audit.issues:
                add_lesson("antipattern", f"Audit issue ({issue.severity}): {issue.issue[:200]}", project_name)

    # Learn from planning
    if state.plan and state.plan.rounds:
        rounds_used = len(state.plan.rounds)
        if rounds_used == 1:
            add_lesson("pattern", f"Quick consensus (1 round) for: {state.task[:100]}", project_name)
        elif rounds_used >= 5:
            add_lesson("antipattern", f"Slow consensus ({rounds_used} rounds) for: {state.task[:100]}", project_name)

    # Learn from resolution
    if state.resolve_iteration >= 3:
        add_lesson("antipattern", f"Needed {state.resolve_iteration} fix iterations for: {state.task[:100]}", project_name)


def get_lessons_prompt(project_dir: Path) -> str:
    """Build a prompt section with relevant lessons for injection into agent prompts."""
    lessons = load_lessons()
    project_name = project_dir.name

    parts = []

    # Recent patterns (what works)
    recent_patterns = [p for p in lessons["patterns"][-10:]]
    if recent_patterns:
        parts.append("LESSONS - What has worked well in past sessions:")
        for p in recent_patterns:
            parts.append(f"  - {p['lesson']}")

    # Recent antipatterns (what to avoid)
    recent_antipatterns = [a for a in lessons["antipatterns"][-10:]]
    if recent_antipatterns:
        parts.append("\nLESSONS - What to avoid (past issues):")
        for a in recent_antipatterns:
            parts.append(f"  - {a['lesson']}")

    # Project-specific notes
    project_notes = lessons.get("project_notes", {}).get(project_name, [])
    if project_notes:
        parts.append(f"\nPROJECT NOTES for {project_name}:")
        for note in project_notes[-5:]:
            parts.append(f"  - {note}")

    return "\n".join(parts) if parts else ""


def load_project_context(project_dir: Path) -> str:
    """Load per-project context from <project>/.claudex/context.md.

    This file is user-editable and gets injected into the analyze phase
    so both agents understand the project before starting work.
    """
    context_file = project_dir / ".claudex" / "context.md"
    if not context_file.exists():
        return ""

    content = context_file.read_text(encoding="utf-8", errors="replace").strip()
    if content:
        return f"\nPROJECT CONTEXT (from .claudex/context.md):\n{content}\n"
    return ""


def get_recent_sessions(project_dir: Path, limit: int = 3) -> str:
    """Get a summary of recent sessions for this project.

    Helps agents understand what was previously done in this project.
    """
    if not SESSIONS_DIR.exists():
        return ""

    project_name = str(project_dir)
    recent = []

    for filepath in sorted(SESSIONS_DIR.glob("*.json"), reverse=True):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("target_dir") == project_name:
                recent.append(data)
                if len(recent) >= limit:
                    break
        except (json.JSONDecodeError, KeyError):
            continue

    if not recent:
        return ""

    parts = ["RECENT SESSIONS for this project:"]
    for session in recent:
        approved = session.get("audit", {}).get("final_approved", False)
        status = "approved" if approved else "not approved"
        files = session.get("code", {}).get("files_generated", [])
        file_list = ", ".join(f["path"] for f in files[:3])
        parts.append(
            f"  - [{session['timestamp'][:10]}] {session['task'][:80]} "
            f"({status}, files: {file_list or 'none'})"
        )

    return "\n".join(parts)
