"""Grounded build loop (Wave 2A): Codex reads, claudex applies, claudex verifies.

Codex runs READ-ONLY inside the worktree (it can read the real files to ground
its changes but cannot write — proven impossible headless on Windows anyway).
It emits search/replace edits as text; claudex applies them (path-confined,
transactional) and then runs the project's verification itself. If edits fail to
apply (or none parse), Codex is re-prompted with the structured failures.

The git diff is produced by the caller (orchestrator) from the worktree after a
successful build — this module stays git-free so it is unit-testable with a fake
provider and a plain temp dir.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .edits import parse_edits, apply_edits, EditFailure
from .runner import run_verification, VerificationResult

_MAX_BUILD_ATTEMPTS = 3

_SYSTEM = (
    "You are a Senior Developer implementing an agreed plan. You are running "
    "READ-ONLY in the project directory: read any files you need to ground your "
    "changes, but do NOT modify files yourself. Return your changes ONLY as the "
    "edit blocks specified — claudex applies them and runs the tests."
)

_FORMAT = """OUTPUT FORMAT — emit ONLY these blocks (no markdown fences around them):

To modify an existing file (SEARCH must be the EXACT current text, appearing once):
=== EDIT: relative/path.py ===
<<<<<<< SEARCH
<exact existing lines, verbatim>
=======
<replacement lines>
>>>>>>> REPLACE
=== END EDIT ===

To create a new file (or fully replace a small one):
=== FILE: relative/path.py ===
<full file contents>
=== END FILE ===

Rules: paths are relative to this directory; each SEARCH must match exactly once
(include enough surrounding context to be unique); every block needs its closing
marker. You may add a short prose summary before the blocks."""


@dataclass
class BuildResult:
    edits_applied: bool            # Codex's edits parsed + applied cleanly to the worktree
    explanation: str = ""
    verification: VerificationResult | None = None
    edit_failures: list[EditFailure] = field(default_factory=list)
    attempts: int = 0
    error: str = ""

    @property
    def verified(self) -> bool:
        """True only if edits applied AND verification actually passed.

        Use THIS (not edits_applied) to decide ship-readiness — applied edits
        can still fail the test/smoke gate.
        """
        return self.edits_applied and self.verification is not None and self.verification.passed


def _format_failures(failures: list[EditFailure]) -> str:
    lines = ["YOUR PREVIOUS EDITS DID NOT APPLY — fix and resend ALL needed edits:"]
    for f in failures:
        where = f.path or "(no path)"
        lines.append(f"  - {where}: {f.reason}")
    lines.append(
        "Re-read the current file contents and make each SEARCH block match exactly "
        "once (add more surrounding context)."
    )
    return "\n".join(lines)


def _build_prompt(plan: str, project_context: str, feedback: str, failures: list[EditFailure]) -> str:
    parts = ["Implement the plan by editing the real files in this directory. "
             "Read the relevant existing files first to ground your edits.\n",
             f"PLAN:\n{plan}\n"]
    if project_context:
        parts.append(f"PROJECT CONTEXT:\n{project_context}\n")
    if feedback:
        parts.append(f"REVIEWER FEEDBACK (address this):\n{feedback}\n")
    if failures:
        parts.append(_format_failures(failures) + "\n")
    parts.append(_FORMAT)
    return "\n".join(parts)


def _extract_explanation(text: str) -> str:
    cleaned = re.sub(r"===\s*EDIT:.*?===\s*END\s*EDIT\s*===", "", text, flags=re.DOTALL)
    cleaned = re.sub(r"===\s*FILE:.*?===\s*END\s*FILE\s*===", "", cleaned, flags=re.DOTALL)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned[:500]


def run_build(
    stage,
    plan: str,
    project_context: str,
    codex,
    *,
    configured_test: str = "",
    feedback: str = "",
    max_attempts: int = _MAX_BUILD_ATTEMPTS,
    on_message=None,
) -> BuildResult:
    """Run Codex grounded → apply edits → verify. See module docstring."""
    stage = Path(stage)
    failures: list[EditFailure] = []
    explanation = ""
    attempts = 0
    applied = False

    for attempt in range(1, max_attempts + 1):
        attempts = attempt
        prompt = _build_prompt(plan, project_context, feedback, failures)
        resp = codex.send(prompt, system_prompt=_SYSTEM, cwd=str(stage), sandbox="read-only")

        if not resp.success:
            if on_message:
                on_message("codex", "Developer", f"[ERROR] {resp.error}")
            return BuildResult(edits_applied=False, explanation=explanation, attempts=attempt,
                               error=resp.error or "codex call failed")

        if on_message:
            on_message("codex", "Developer", resp.content)
        explanation = _extract_explanation(resp.content) or explanation

        ops = parse_edits(resp.content)
        if not ops:
            excerpt = resp.content.strip()[:200].replace("\n", " ")
            failures = [EditFailure(
                "", f"no parsable === EDIT === / === FILE === blocks found "
                    f"(and no closing markers). Your response began: {excerpt!r}")]
            if on_message:
                on_message("system", "Claudex", "Codex produced no parsable edits; re-prompting.")
            continue

        result = apply_edits(ops, stage)
        if result.ok:
            applied, failures = True, []
            break

        failures = result.failures
        if on_message:
            on_message("system", "Claudex",
                       f"{len(failures)} edit(s) failed to apply; re-prompting Codex (attempt {attempt}).")

    if not applied:
        return BuildResult(edits_applied=False, explanation=explanation, edit_failures=failures,
                           attempts=attempts, error="edits did not apply after retries")

    verification = run_verification(stage, configured=configured_test)
    return BuildResult(edits_applied=True, explanation=explanation,
                       verification=verification, attempts=attempts)
