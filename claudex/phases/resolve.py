"""Phase 5: Resolution — Codex fixes in the worktree, claudex re-verifies, Claude re-audits."""

from ..build import run_build, record_build
from ..models import SessionState
from ..phases.audit import run_audit
from ..providers.base import LLMProvider


def run_resolution(
    state: SessionState,
    claude: LLMProvider,
    codex: LLMProvider,
    max_iterations: int = 5,
    on_message=None,
    config=None,
) -> bool:
    """Iterate fix -> re-verify -> re-audit until approved or max iterations.

    Codex fixes IN THE SAME worktree (grounded in the current code + the reviewer's
    feedback + the captured verification output); claudex re-runs the tests and
    Claude re-audits the new diff. Returns True if ultimately approved.
    """
    configured_test = config.test_command if config else ""

    for iteration in range(1, max_iterations + 1):
        state.resolve_iteration = iteration
        last_audit = state.audit_results[-1]

        if on_message:
            on_message("system", "Claudex", f"Resolution iteration {iteration}/{max_iterations}")

        feedback = last_audit.feedback_for_coder or "\n".join(i.issue for i in last_audit.issues)

        if on_message:
            on_message("system", "Claudex", "Codex applying fixes in the worktree...")

        build = run_build(
            state.stage_dir,
            state.plan.agreed_plan if state.plan else "",
            state.grounded_context(),
            codex,
            configured_test=configured_test,
            feedback=feedback,
            on_message=on_message,
        )
        record_build(state, build)

        if not build.edits_applied:
            if on_message:
                on_message("system", "Claudex", f"Fix attempt failed to apply: {build.error}")
            continue

        if on_message:
            on_message("system", "Claudex", "Claude re-auditing the updated diff...")

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
