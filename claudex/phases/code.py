"""Phase 3: Code Generation — Codex implements the agreed plan."""

import json
import re

from ..memory import select_relevant_lessons, format_lessons_for_prompt, CODE_LESSON_LIMIT, CODE_LESSON_CHARS
from ..models import SessionState, CodeResult, FileOutput
from ..providers.base import LLMProvider


def _get_code_lessons(state: SessionState) -> str:
    """Get relevant antipatterns for the code phase (max 400 chars)."""
    if not state.target_dir:
        return ""
    relevant = select_relevant_lessons(
        state.task, state.target_dir,
        category="antipattern",
        limit=CODE_LESSON_LIMIT,
        max_chars=CODE_LESSON_CHARS,
    )
    return format_lessons_for_prompt(relevant)


def run_coding(
    state: SessionState,
    codex: LLMProvider,
    on_message=None,
    feedback: str = "",
) -> CodeResult:
    """Have Codex generate code based on the agreed plan.

    Args:
        state: Session state with completed plan
        codex: Codex CLI provider
        on_message: Optional callback for live output
        feedback: Optional feedback from a previous audit (for fix iterations)
    """
    plan = state.plan.agreed_plan

    if feedback:
        prompt = f"""You previously generated code that was reviewed and needs changes.

ORIGINAL PLAN:
{plan}

REVIEWER FEEDBACK:
{feedback}

PREVIOUS CODE:
{_format_previous_code(state.code_result)}

Fix the issues identified by the reviewer. Generate the corrected files.
"""
    else:
        prompt = f"""Implement the following plan. Generate complete, runnable code files.

PLAN:
{plan}

TARGET DIRECTORY: {state.target_dir}

PROJECT CONTEXT:
{state.analysis.project_context}

REQUIREMENTS:
- Generate complete files, not snippets or pseudocode
- Include all imports, all error handling
- No TODO stubs or placeholder comments
- Each file must be immediately runnable/importable

{_get_code_lessons(state)}
OUTPUT FORMAT:
For each file, use this exact format:

=== FILE: path/to/file.py ===
(file content here)
=== END FILE ===

Generate all files needed to complete the task.
"""

    system_prompt = f"""You are a Senior Developer implementing code from an agreed plan.
Your Technical Architect partner will review your code, so make it production-quality.
Write complete, runnable code. No shortcuts, no placeholders.

Expert domains for this task: {', '.join(state.analysis.required_expertise)}"""

    response = codex.send(prompt, system_prompt=system_prompt)

    if not response.success:
        if on_message:
            on_message("codex", "Developer", f"[ERROR] {response.error}")
        return CodeResult(files=[], explanation=f"Code generation failed: {response.error}")

    if on_message:
        on_message("codex", "Developer", response.content)

    # Parse the response into FileOutput objects
    files = _parse_file_outputs(response.content)
    explanation = _extract_explanation(response.content)

    return CodeResult(files=files, explanation=explanation)


def _parse_file_outputs(text: str) -> list[FileOutput]:
    """Parse file outputs from the response.

    Looks for the format:
    === FILE: path/to/file.py ===
    (content)
    === END FILE ===
    """
    files = []

    # Pattern: === FILE: path === ... === END FILE ===
    pattern = r'===\s*FILE:\s*(.+?)\s*===\s*\n(.*?)===\s*END\s*FILE\s*==='
    matches = re.findall(pattern, text, re.DOTALL)

    for path, content in matches:
        path = path.strip()
        content = content.strip()
        files.append(FileOutput(
            path=path,
            content=content,
            action="create",  # default; could be "modify" if file exists
        ))

    # Fallback: look for ```filename patterns
    if not files:
        # Try ```python filename.py or ```js filename.js
        code_blocks = re.findall(
            r'(?:#+\s*)?(?:`{3,}\w*\s*)?([\w./\\-]+\.\w+)\s*\n```\w*\n(.*?)```',
            text, re.DOTALL
        )
        for path, content in code_blocks:
            files.append(FileOutput(
                path=path.strip(),
                content=content.strip(),
                action="create",
            ))

    # Last fallback: if there's a single code block, use a default name
    if not files:
        code_match = re.search(r'```\w*\n(.*?)```', text, re.DOTALL)
        if code_match:
            content = code_match.group(1).strip()
            # Try to guess extension from content
            ext = ".py" if "def " in content or "import " in content else ".js"
            files.append(FileOutput(
                path=f"main{ext}",
                content=content,
                action="create",
            ))

    return files


def _extract_explanation(text: str) -> str:
    """Extract the explanation/commentary from the response (non-code parts)."""
    # Remove file blocks
    cleaned = re.sub(r'===\s*FILE:.*?===\s*END\s*FILE\s*===', '', text, flags=re.DOTALL)
    # Remove code blocks
    cleaned = re.sub(r'```\w*\n.*?```', '', cleaned, flags=re.DOTALL)
    # Clean up whitespace
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned).strip()
    return cleaned[:500] if cleaned else ""


def _format_previous_code(code_result: CodeResult | None) -> str:
    """Format previous code result for the fix prompt."""
    if not code_result or not code_result.files:
        return "(no previous code)"
    parts = []
    for f in code_result.files:
        parts.append(f"=== FILE: {f.path} ===\n{f.content}\n=== END FILE ===")
    return "\n\n".join(parts)
