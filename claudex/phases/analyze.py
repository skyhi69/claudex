"""Phase 1: Task Analysis — Claude analyzes the task and assigns expert roles."""

import os
from pathlib import Path

from ..memory import load_project_context, select_relevant_lessons, format_lessons_for_prompt, ANALYZE_LESSON_LIMIT, ANALYZE_LESSON_CHARS
from ..models import AnalysisResult, SessionState
from ..providers.base import LLMProvider
from ..roles import detect_expertise, get_role_description


def _scan_project(target_dir: Path) -> str:
    """Build a summary of existing project files for context."""
    if not target_dir.exists():
        return "(empty directory — new project)"

    files = []
    for root, dirs, filenames in os.walk(target_dir):
        # Skip hidden dirs, node_modules, __pycache__, .git
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ("node_modules", "__pycache__", ".git", "venv", "env")]
        for f in filenames:
            rel = os.path.relpath(os.path.join(root, f), target_dir)
            files.append(rel)
        if len(files) > 50:
            break

    if not files:
        return "(empty directory — new project)"

    return "Existing project files:\n" + "\n".join(f"  {f}" for f in sorted(files)[:50])


def run_analysis(state: SessionState, claude: LLMProvider) -> AnalysisResult:
    """Analyze the task, detect required expertise, scan project context.

    This phase uses Claude to understand the task and determine what
    expertise both agents need for this specific task.
    """
    # Detect expertise from the task description
    expertise = detect_expertise(state.task)
    role_desc = get_role_description(expertise)

    # Scan project directory
    project_context = _scan_project(state.target_dir)

    # Load memory — lightweight, relevance-scored
    memory_context = load_project_context(state.target_dir)

    relevant = select_relevant_lessons(
        state.task, state.target_dir,
        category="antipattern",
        limit=ANALYZE_LESSON_LIMIT,
        max_chars=ANALYZE_LESSON_CHARS,
    )
    lessons_section = format_lessons_for_prompt(relevant, "LESSONS - Avoid these past mistakes:")

    # Ask Claude for task analysis
    prompt = f"""Analyze this coding task and provide a brief assessment.

TASK: {state.task}

PROJECT CONTEXT:
{project_context}
{memory_context}
{lessons_section}

DETECTED EXPERTISE NEEDED: {role_desc}

Provide:
1. A clear one-paragraph summary of what needs to be built
2. The complexity level (simple / moderate / complex)
3. Any additional expertise domains needed beyond what was detected
4. Key decisions that will need to be made during planning

Keep your response concise — this is just the initial analysis, not the full plan."""

    response = claude.send(prompt, system_prompt="You are a senior technical architect analyzing a coding task.")

    if not response.success:
        # Fallback: use keyword-based analysis without LLM
        return AnalysisResult(
            task_summary=state.task,
            required_expertise=expertise,
            claude_role=f"Technical Architect specializing in {role_desc}",
            codex_role=f"Senior Developer specializing in {role_desc}",
            project_context=project_context,
            complexity="moderate",
        )

    # Determine complexity from response
    lower = response.content.lower()
    if "complex" in lower:
        complexity = "complex"
    elif "simple" in lower or "straightforward" in lower:
        complexity = "simple"
    else:
        complexity = "moderate"

    return AnalysisResult(
        task_summary=response.content,
        required_expertise=expertise,
        claude_role=f"Technical Architect specializing in {role_desc}",
        codex_role=f"Senior Developer specializing in {role_desc}",
        project_context=project_context,
        complexity=complexity,
    )
