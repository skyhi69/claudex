"""Phase 4: Blind Audit — Claude reviews code without seeing the coder's explanation first."""

import json
import re

from ..models import SessionState, AuditResult, AuditIssue
from ..providers.base import LLMProvider


def run_audit(
    state: SessionState,
    claude: LLMProvider,
    on_message=None,
) -> AuditResult:
    """Claude audits the tested git DIFF + verification evidence (not full files).

    Evidence-based and blind: Claude reviews exactly what changed plus the
    test/smoke output claudex captured by RUNNING it. Approval is HARD-GATED on
    verification — even a glowing review cannot approve if the gate did not pass
    ("Done = ran and observed").
    """
    diff = state.diff.strip() or "(no diff captured)"
    plan = state.plan.agreed_plan[-2000:] if state.plan else "(no plan — fast path)"
    evidence = state.verification_output.strip() or "(no verification output)"
    label = state.verification_label or ("passed" if state.verification_passed else "unknown")

    prompt = f"""BLIND DIFF REVIEW

You are reviewing the change a Senior Developer made for the task below. You see the
git diff and the verification (test/smoke) output that claudex captured by RUNNING
it — form your own opinion.

ORIGINAL TASK: {state.task}

AGREED PLAN:
{plan}

CHANGED FILES:
{state.name_status or '(none)'}

GIT DIFF:
{diff}

VERIFICATION EVIDENCE (claudex ran this — result: {label}):
{evidence}

Review for: correctness/logic bugs, security vulnerabilities, completeness vs the
plan, quality, and whether any tests truly exercise behavior (not just assert what
the mocks return).

IMPORTANT: After your prose review, END with a SINGLE fenced JSON verdict block,
exactly in this shape and as the last thing in your response:
```json
{{"approved": true, "issues": [{{"severity": "critical|high|medium|low", "file": "path/to/file", "issue": "what is wrong", "fix": "how to fix it"}}], "assessment": "one-paragraph summary"}}
```
Rules: set "approved": false if there is ANY critical or high severity issue.
Use [] for "issues" when there are none.
"""

    system_prompt = f"""You are a Technical Architect doing an evidence-based review of a diff.
Judge what the diff DOES, not what mocks return. Be rigorous but fair — focus on
correctness and security over style. Expert domains: {', '.join(state.analysis.required_expertise)}"""

    response = claude.send(prompt, system_prompt=system_prompt)

    if not response.success:
        if on_message:
            on_message("claude", "Architect", f"[ERROR] {response.error}")
        return AuditResult(
            approved=False,
            assessment=f"Audit failed: {response.error}",
            feedback_for_coder="Audit could not be completed due to an error.",
        )

    if on_message:
        on_message("claude", "Architect (Audit)", response.content)

    approved, issues, assessment, parsed_ok = _parse_verdict(response.content)

    if on_message and not parsed_ok:
        on_message(
            "system", "Claudex",
            "Audit verdict JSON missing/malformed — failing closed (treated as REJECTED).",
        )

    # HARD verification gate: never approve a change whose tests/smoke did not pass,
    # no matter how positive the prose review ("ran and observed", not "looks fine").
    if not state.verification_passed:
        if approved and on_message:
            on_message("system", "Claudex",
                       f"Overriding APPROVED -> REJECTED: verification did not pass ({label}).")
        approved = False
        issues = issues + [AuditIssue(
            severity="high", file="",
            issue=f"Verification did not pass ({label}).",
            suggested_fix="Fix the failing tests / smoke check before approval.")]

    feedback = "" if approved else f"{response.content}\n\nVERIFICATION OUTPUT:\n{evidence}"

    return AuditResult(
        approved=approved,
        assessment=assessment,
        issues=issues,
        feedback_for_coder=feedback,
    )


_VALID_SEVERITIES = frozenset({"critical", "high", "medium", "low"})


def _parse_verdict(text: str, allow_legacy_verdict: bool = False) -> tuple[bool, list[AuditIssue], str, bool]:
    """Parse the structured JSON verdict. Fail closed.

    Returns (approved, issues, assessment, parsed_ok).
      - Primary: a fenced or raw JSON object containing an "approved" key.
      - Structural guard: any critical/high issue forces approved=False,
        even if the model set approved=true (anti-sycophancy).
      - Missing/malformed JSON: REJECT. The legacy `VERDICT: APPROVED` line is
        honored ONLY when allow_legacy_verdict=True (e.g. re-parsing old saved
        transcripts) — never for active audits, so a model cannot skip the
        required JSON contract and still pass via legacy text.
    """
    verdict = _extract_verdict_json(text)
    if verdict is not None:
        approved = bool(verdict.get("approved", False))
        issues = _issues_from_json(verdict.get("issues", []))
        assessment = str(verdict.get("assessment", "")).strip() or text.strip()
        if any(i.severity in ("critical", "high") for i in issues):
            approved = False
        return approved, issues, assessment, True

    if allow_legacy_verdict:
        return _detect_verdict_line(text), [], text.strip(), False
    return False, [], text.strip(), False


def _extract_verdict_json(text: str) -> dict | None:
    """Return the last JSON object containing an "approved" key, or None.

    Prefers fenced ```json blocks; falls back to raw brace scanning. Malformed
    JSON is skipped (contributes nothing), so a broken block fails closed.
    """
    candidates: list[dict] = []

    for match in re.finditer(r"```json\s*\n?(.*?)\n?\s*```", text, re.DOTALL):
        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and "approved" in data:
            candidates.append(data)

    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", text):
        try:
            data, _ = decoder.raw_decode(text, match.start())
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(data, dict) and "approved" in data:
            candidates.append(data)

    return candidates[-1] if candidates else None


def _issues_from_json(raw) -> list[AuditIssue]:
    """Build AuditIssue list from the verdict's structured issues array."""
    issues: list[AuditIssue] = []
    if not isinstance(raw, list):
        return issues
    for item in raw:
        if not isinstance(item, dict):
            continue
        issue_text = str(item.get("issue", "")).strip()
        if not issue_text:
            continue
        severity = str(item.get("severity", "medium")).strip().lower()
        if severity not in _VALID_SEVERITIES:
            severity = "medium"
        fix = str(item.get("fix", "") or item.get("suggested_fix", "")).strip()
        issues.append(AuditIssue(
            severity=severity,
            file=str(item.get("file", "")).strip(),
            issue=issue_text,
            suggested_fix=fix,
        ))
    return issues


def _detect_verdict_line(text: str) -> bool:
    """Fail-closed fallback: True only on an explicit `VERDICT: APPROVED` line."""
    matches = list(re.finditer(r'VERDICT:\s*(APPROVED|REJECTED)', text, re.IGNORECASE))
    if matches:
        return matches[-1].group(1).upper() == "APPROVED"
    return False
