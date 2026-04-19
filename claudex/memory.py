"""Memory system for Claudex — session logs, lessons, and project context."""

from __future__ import annotations

import json
import os
import re
import yaml
from datetime import datetime
from pathlib import Path

from .models import SessionState

# Default directories
CLAUDEX_HOME = Path(os.path.expanduser("~")) / ".claudex"
SESSIONS_DIR = CLAUDEX_HOME / "sessions"
LESSONS_FILE = CLAUDEX_HOME / "lessons.yaml"

# Injection budget constants
ANALYZE_LESSON_LIMIT = 5
ANALYZE_LESSON_CHARS = 800
CODE_LESSON_LIMIT = 3
CODE_LESSON_CHARS = 400

# Stopwords for relevance scoring (meta-tokens that appear in every lesson)
_STOPWORDS = frozenset(
    "the a an is was were be been being have has had do does did will would "
    "could should might shall may can for of in on at to from by with and or "
    "but not this that it its also very just more most some any all each "
    "audit issue low high critical medium concern consensus block task".split()
)


def ensure_dirs():
    """Create memory directories if they don't exist."""
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)


def _tokenize(text: str) -> set[str]:
    """Tokenize text for relevance scoring — lowercase, strip punctuation, remove stopwords."""
    words = re.findall(r'[a-z0-9]+', text.lower())
    return {w for w in words if w not in _STOPWORDS and len(w) > 2}


def _relevance_score(lesson_text: str, task_tokens: set[str]) -> float:
    """Score a lesson's relevance to the current task using Jaccard overlap."""
    lesson_tokens = _tokenize(lesson_text)
    if not lesson_tokens or not task_tokens:
        return 0.0
    intersection = lesson_tokens & task_tokens
    union = lesson_tokens | task_tokens
    return len(intersection) / len(union) if union else 0.0


def _is_duplicate(text: str, existing: list[dict], threshold: float = 0.8) -> bool:
    """Check if a lesson is a near-duplicate of any existing lesson."""
    new_tokens = _tokenize(text)
    if not new_tokens:
        return False
    for entry in existing:
        existing_tokens = _tokenize(entry.get("lesson", ""))
        if not existing_tokens:
            continue
        overlap = len(new_tokens & existing_tokens) / max(len(new_tokens | existing_tokens), 1)
        if overlap > threshold:
            return True
    return False


def select_relevant_lessons(
    task: str,
    project_dir: Path,
    category: str = "antipattern",
    limit: int = 5,
    max_chars: int = 800,
) -> list[dict]:
    """Select the most relevant lessons for a task, scoped by project.

    Scoring: same-project + keyword match > same-project recency
             > cross-project + keyword match > (drop)
    """
    lessons = load_lessons()
    pool = lessons.get(f"{category}s", [])
    if not pool:
        return []

    project_name = project_dir.name if project_dir else ""
    task_tokens = _tokenize(task)

    scored = []
    for entry in pool:
        relevance = _relevance_score(entry.get("lesson", ""), task_tokens)
        # Boost same-project lessons
        project_match = 1.0 if entry.get("project") == project_name else 0.0
        score = relevance + project_match
        scored.append((score, entry))

    # Sort by score descending
    scored.sort(key=lambda x: x[0], reverse=True)

    # Select top entries within char budget
    selected = []
    total_chars = 0
    for score, entry in scored:
        lesson_text = entry.get("lesson", "")
        if total_chars + len(lesson_text) > max_chars:
            break
        selected.append(entry)
        total_chars += len(lesson_text)
        if len(selected) >= limit:
            break

    return selected


def format_lessons_for_prompt(lessons: list[dict], header: str = "AVOID (past mistakes):") -> str:
    """Format selected lessons into a prompt section."""
    if not lessons:
        return ""
    lines = [header]
    for entry in lessons:
        lines.append(f"  - {entry['lesson']}")
    return "\n".join(lines)


# --- Session logs ---

def save_session(state: SessionState) -> Path:
    """Save a session log after a Claudex run."""
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
        },
        "code": {
            "files_generated": [
                {"path": f.path, "action": f.action, "lines": f.content.count("\n") + 1}
                for f in (state.code_result.files if state.code_result else [])
            ],
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
    }

    filename = f"{state.session_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    filepath = SESSIONS_DIR / filename

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(session_data, f, indent=2, ensure_ascii=False)

    return filepath


# --- Lessons ---

def load_lessons() -> dict:
    """Load lessons from the lessons file."""
    if not LESSONS_FILE.exists():
        return {"patterns": [], "antipatterns": []}

    with open(LESSONS_FILE, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    return {
        "patterns": data.get("patterns", []),
        "antipatterns": data.get("antipatterns", []),
    }


def save_lessons(lessons: dict):
    """Save lessons to the lessons file."""
    ensure_dirs()
    with open(LESSONS_FILE, "w", encoding="utf-8") as f:
        yaml.dump(lessons, f, default_flow_style=False, allow_unicode=True, sort_keys=False)


def add_lesson(category: str, lesson: str, project: str = ""):
    """Add a single lesson learned."""
    lessons = load_lessons()

    entry = {
        "lesson": lesson,
        "date": datetime.now().strftime("%Y-%m-%d"),
        "project": project or "general",
    }

    key = f"{category}s"
    if key not in lessons:
        lessons[key] = []

    # Dedupe check
    if _is_duplicate(lesson, lessons[key][-10:]):
        return

    lessons[key].append(entry)
    lessons[key] = lessons[key][-50:]  # keep last 50

    save_lessons(lessons)


def auto_learn(state: SessionState):
    """Automatically extract lessons from a completed session.

    Only saves critical/high severity issues to prevent antipattern pollution.
    """
    project_name = state.target_dir.name if state.target_dir else "unknown"

    # Learn from audit results — only critical and high severity
    if state.audit_results:
        last_audit = state.audit_results[-1]
        if last_audit.approved and len(state.audit_results) == 1:
            add_lesson("pattern", f"Code approved on first audit for: {state.task[:100]}", project_name)
        elif not last_audit.approved:
            for issue in last_audit.issues:
                if issue.severity in ("critical", "high"):
                    add_lesson("antipattern", f"{issue.issue[:200]}", project_name)


# --- Project context ---

def load_project_context(project_dir: Path) -> str:
    """Load per-project context from <project>/.claudex/context.md."""
    context_file = project_dir / ".claudex" / "context.md"
    if not context_file.exists():
        return ""

    content = context_file.read_text(encoding="utf-8", errors="replace").strip()
    if content:
        return f"\nPROJECT CONTEXT (from .claudex/context.md):\n{content}\n"
    return ""
