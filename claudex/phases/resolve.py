"""Phase 5: Resolution — discuss fixes, implement, re-audit until approved."""

from pathlib import Path

from ..models import SessionState, AuditResult
from ..providers.base import LLMProvider
from ..phases.code import run_coding
from ..phases.audit import run_audit


def run_resolution(
    state: SessionState,
    claude: LLMProvider,
    codex: LLMProvider,
    max_iterations: int = 5,
    on_message=None,
) -> bool:
    """Iterate fix → re-audit until approved or max iterations reached.

    Returns True if code was ultimately approved.

    The loop:
    1. Claude shares audit feedback with Codex
    2. Both briefly discuss the fix approach (1-2 messages)
    3. Codex implements the fix
    4. Claude re-audits
    5. Repeat if still rejected
    """
    for iteration in range(1, max_iterations + 1):
        state.resolve_iteration = iteration
        last_audit = state.audit_results[-1]

        if on_message:
            on_message("system", "Claudex", f"Resolution iteration {iteration}/{max_iterations}")

        # Step 1: Brief discussion about fixes
        fix_discussion = _discuss_fixes(state, claude, codex, last_audit, on_message)

        # Step 2: Codex implements fixes
        if on_message:
            on_message("system", "Claudex", "Codex implementing fixes...")

        state.code_result = run_coding(
            state, codex, on_message,
            feedback=last_audit.feedback_for_coder + "\n\nDISCUSSION:\n" + fix_discussion,
        )

        if not state.code_result.files:
            if on_message:
                on_message("system", "Claudex", "Code generation failed during fix iteration.")
            continue

        # Step 3: Claude re-audits
        if on_message:
            on_message("system", "Claudex", "Claude re-auditing...")

        audit_result = run_audit(state, claude, on_message)
        state.audit_results.append(audit_result)

        if audit_result.approved:
            if on_message:
                on_message("system", "Claudex", "Code approved after fixes!")
            return True

    if on_message:
        on_message("system", "Claudex",
                    f"Max iterations ({max_iterations}) reached. Presenting current state for your review.")
    return False


def _discuss_fixes(
    state: SessionState,
    claude: LLMProvider,
    codex: LLMProvider,
    last_audit: AuditResult,
    on_message=None,
) -> str:
    """Brief discussion between Claude and Codex about how to fix audit issues."""

    # Claude summarizes what needs fixing
    claude_prompt = f"""Your audit found these issues with the code:

{last_audit.assessment}

Provide a concise summary of what needs to change, prioritized by severity.
Focus on the specific fixes needed, not general commentary."""

    claude_response = claude.send(
        claude_prompt,
        system_prompt="You are a Technical Architect giving fix guidance to your developer partner. Be specific and actionable.",
    )

    if not claude_response.success:
        return last_audit.feedback_for_coder

    if on_message:
        on_message("claude", "Architect", claude_response.content)

    # Codex confirms understanding / pushes back
    codex_prompt = f"""The Technical Architect reviewed your code and found issues:

{claude_response.content}

Confirm you understand the fixes needed. If any fix suggestion is impractical or wrong,
explain why and propose an alternative. Be brief and specific."""

    codex_response = codex.send(
        codex_prompt,
        system_prompt="You are a Senior Developer receiving fix guidance. Confirm understanding or push back if the fix is wrong.",
    )

    if codex_response.success:
        if on_message:
            on_message("codex", "Developer", codex_response.content)
        return f"ARCHITECT: {claude_response.content}\n\nDEVELOPER: {codex_response.content}"

    return claude_response.content
