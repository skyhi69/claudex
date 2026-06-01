"""Claudex orchestrator - graph-based state machine for the multi-agent pipeline."""

from __future__ import annotations

import os
from pathlib import Path

from . import worktree
from .build import run_build, record_build
from .config import ClaudexConfig
from .memory import save_session, auto_learn
from .models import DecisionBrief, NodeType, SessionState
from .phases.analyze import run_analysis
from .phases.audit import run_audit
from .phases.plan import run_planning, run_fast_plan
from .phases.resolve import run_resolution
from .providers.claude import ClaudeProvider
from .providers.codex import CodexProvider


class Orchestrator:
    """Drives the Claudex pipeline: Analyze -> Plan -> Code -> Audit -> Resolve -> Done."""

    def __init__(self, config: ClaudexConfig, roles_dir: Path, on_message=None):
        self.config = config
        self.roles_dir = roles_dir
        self.claude = ClaudeProvider()
        self.codex = CodexProvider()
        self.on_message = on_message or (lambda *args: None)

    def run(self, task: str, target_dir: Path) -> SessionState:
        """Execute the full Claudex pipeline."""
        state = SessionState(task=task, target_dir=target_dir)
        os.environ["CLAUDEX_PROJECT_DIR"] = str(target_dir)

        try:
            while state.current_node not in (NodeType.DONE, NodeType.FAILED):
                state = self._step(state)
        except BaseException:
            # Never leak the throwaway worktree if the pipeline crashes or is
            # aborted (KeyboardInterrupt) mid-run. On normal completion the CLI
            # owns cleanup (after apply-on-approval).
            self.cleanup(state)
            raise

        # Record the session quota ledger (Wave 1.4) before saving/learning.
        state.usage_summary = {
            "claude": {"calls": self.claude.call_count, **self.claude.usage_totals},
            "codex": {"calls": self.codex.call_count, **self.codex.usage_totals},
        }

        # Save session and learn from it
        try:
            session_path = save_session(state)
            self.on_message("system", "Claudex", f"Session saved to {session_path.name}")
            auto_learn(state)
        except Exception as e:
            self.on_message("system", "Claudex", f"Warning: could not save session: {e}")

        return state

    def _step(self, state: SessionState) -> SessionState:
        """Execute one step of the state machine."""
        if state.current_node == NodeType.INIT:
            return self._handle_init(state)
        if state.current_node == NodeType.ANALYZE:
            return self._handle_analyze(state)
        if state.current_node == NodeType.PLAN:
            return self._handle_plan(state)
        if state.current_node == NodeType.CODE:
            return self._handle_code(state)
        if state.current_node == NodeType.AUDIT:
            return self._handle_audit(state)
        if state.current_node == NodeType.RESOLVE:
            return self._handle_resolve(state)

        state.current_node = NodeType.FAILED
        return state

    def _handle_init(self, state: SessionState) -> SessionState:
        """Validate prerequisites and move to analysis."""
        self.on_message("system", "Claudex", f"Session {state.session_id} started")

        if not self.claude.is_available():
            self.on_message("system", "Claudex", "ERROR: Claude CLI not found. Is it installed and authenticated?")
            state.current_node = NodeType.FAILED
            return state

        if not self.codex.is_available():
            self.on_message("system", "Claudex", "ERROR: Codex CLI not found. Run: npm install -g @openai/codex")
            state.current_node = NodeType.FAILED
            return state

        state.target_dir.mkdir(parents=True, exist_ok=True)
        state.current_node = NodeType.ANALYZE
        return state

    def _handle_analyze(self, state: SessionState) -> SessionState:
        """Phase 1: Analyze the task."""
        self.on_message("system", "Claudex", "Phase 1: Analyzing task...")

        state.analysis = run_analysis(state, self.claude)

        self.on_message("system", "Claudex", f"Task complexity: {state.analysis.complexity}")
        self.on_message("system", "Claudex", f"Claude role: {state.analysis.claude_role}")
        self.on_message("system", "Claudex", f"Codex role: {state.analysis.codex_role}")

        state.current_node = NodeType.PLAN
        return state

    def _handle_plan(self, state: SessionState) -> SessionState:
        """Phase 2: Planning, routed by complexity (B3) to conserve Claude quota.

        simple   -> one-shot Claude plan, no debate (no Codex planning calls)
        moderate -> short debate (<=3 rounds)
        complex  -> full debate (config cap)
        """
        complexity = state.analysis.complexity if state.analysis else "moderate"

        if complexity == "simple":
            self.on_message("system", "Claudex", "Phase 2: Simple task — fast plan (skipping debate)...")
            state.plan = run_fast_plan(state, self.claude, on_message=self.on_message)
            self.on_message("system", "Claudex", "Fast plan ready.")
            state.current_node = NodeType.CODE
            return state

        max_rounds = 3 if complexity == "moderate" else self.config.planning_max_rounds
        self.on_message("system", "Claudex",
                        f"Phase 2: Collaborative planning ({complexity}, up to {max_rounds} rounds)...")

        state.plan = run_planning(
            state,
            self.claude,
            self.codex,
            roles_dir=self.roles_dir,
            max_rounds=max_rounds,
            stall_threshold=self.config.stall_threshold,
            on_message=self.on_message,
        )

        rounds_used = len(state.plan.rounds)
        consensus = state.plan.rounds[-1].consensus_reached if state.plan.rounds else False
        self.on_message("system", "Claudex", f"Planning complete: {rounds_used} rounds, consensus: {consensus}")

        state.current_node = NodeType.CODE
        return state

    def _handle_code(self, state: SessionState) -> SessionState:
        """Phase 3: Codex builds in an isolated git worktree; claudex applies + verifies."""
        self.on_message("system", "Claudex", "Phase 3: Codex building in an isolated worktree...")
        target = state.target_dir

        try:
            worktree.ensure_repo(target, auto_git_init=self.config.auto_git_init, on_message=self.on_message)
        except worktree.GitError as e:
            self.on_message("system", "Claudex", f"ERROR: cannot prepare git worktree: {e}")
            state.current_node = NodeType.FAILED
            return state

        if worktree.is_git_repo(target) and not worktree.is_clean(target):
            self.on_message(
                "system", "Claudex",
                "WARNING: target has uncommitted changes — claudex builds against the last "
                "commit, and the tested diff won't auto-apply until you commit or stash them.")

        state.stage_dir = worktree.create_worktree(target, state.session_id)

        build = run_build(
            state.stage_dir,
            state.plan.agreed_plan if state.plan else "",
            state.analysis.project_context if state.analysis else "",
            self.codex,
            configured_test=self.config.test_command,
            on_message=self.on_message,
        )
        record_build(state, build)

        if not build.edits_applied:
            self.on_message("system", "Claudex", f"ERROR: build failed: {build.error}")
            state.current_node = NodeType.FAILED
            return state

        if not state.diff.strip():
            self.on_message("system", "Claudex", "ERROR: Codex produced no changes.")
            state.current_node = NodeType.FAILED
            return state

        self.on_message("system", "Claudex", f"Build applied. Verification: {state.verification_label}")
        state.current_node = NodeType.AUDIT
        return state

    def _handle_audit(self, state: SessionState) -> SessionState:
        """Phase 4: Blind audit."""
        self.on_message("system", "Claudex", "Phase 4: Claude auditing code (blind review)...")

        audit_result = run_audit(state, self.claude, self.on_message)
        state.audit_results.append(audit_result)

        if audit_result.approved:
            self.on_message("system", "Claudex", "Code APPROVED")
            state.decision_brief = self._build_brief(state)
            state.current_node = NodeType.DONE
            return state

        issue_count = len(audit_result.issues)
        self.on_message("system", "Claudex", f"Code REJECTED - {issue_count} issue(s) found. Moving to resolution...")
        state.current_node = NodeType.RESOLVE
        return state

    def _handle_resolve(self, state: SessionState) -> SessionState:
        """Phase 5: Resolution loop."""
        self.on_message("system", "Claudex", "Phase 5: Resolution...")

        approved = run_resolution(
            state,
            self.claude,
            self.codex,
            max_iterations=self.config.resolve_max_iterations,
            on_message=self.on_message,
            config=self.config,
        )

        state.decision_brief = self._build_brief(state)

        if approved:
            state.current_node = NodeType.DONE
        else:
            self.on_message("system", "Claudex", "Resolution did not reach full approval. Presenting current state.")
            state.current_node = NodeType.DONE

        return state

    def _build_brief(self, state: SessionState) -> DecisionBrief:
        """Build the decision brief for the user."""
        files_summary = []
        for line in (state.name_status or "").splitlines():
            parts = line.split("\t", 1)
            if len(parts) == 2:
                status, path = parts
                action = {"A": "create", "M": "modify", "D": "delete"}.get(status[0], status)
                files_summary.append({"path": path.strip(), "action": action, "lines": 0})

        last_audit = state.audit_results[-1] if state.audit_results else None
        unresolved = [i.issue for i in last_audit.issues] if last_audit and not last_audit.approved else []

        # Honest "what was built": Codex's summary, else the actual changed files —
        # NOT the analysis essay (which describes intent, not the result).
        what_was_built = state.build_explanation.strip()
        if not what_was_built:
            changed = [f["path"] for f in files_summary]
            what_was_built = ("Changed: " + ", ".join(changed)) if changed else "(no changes recorded)"

        return DecisionBrief(
            what_was_built=what_was_built,
            why_this_approach=state.plan.agreed_plan[:500] if state.plan else "",
            alternatives_rejected=state.plan.alternatives_rejected if state.plan else [],
            unresolved_concerns=unresolved,
            files_summary=files_summary,
        )

    def apply_on_approval(self, state: SessionState) -> list:
        """Apply the tested diff to the real project (D1 git-native); keep a backup branch."""
        if not state.stage_dir or not state.diff.strip():
            return ["No changes to apply."]
        summaries = []
        try:
            if worktree.commit_stage(state.stage_dir):
                summaries.append(f"  Backup branch: claudex/{worktree._sanitize_ref(state.session_id)}")
            ok = worktree.apply_patch(state.target_dir, state.diff, require_clean=True)
        except worktree.GitError as e:
            return [f"  Could not apply changes: {e}"]
        if ok:
            summaries.append("  Applied the tested diff to your working tree "
                             "(review with `git status` / `git diff`).")
        else:
            summaries.append("  ERROR: patch did not apply cleanly; the backup branch holds the changes.")
        return summaries

    def cleanup(self, state: SessionState) -> None:
        """Remove the throwaway worktree (the backup branch persists). Safe to call once."""
        if state.stage_dir:
            try:
                worktree.remove_worktree(state.target_dir, state.stage_dir)
            finally:
                state.stage_dir = None
