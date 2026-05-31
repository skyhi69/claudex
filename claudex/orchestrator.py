"""Claudex orchestrator - graph-based state machine for the multi-agent pipeline."""

from __future__ import annotations

import os
from pathlib import Path

from .config import ClaudexConfig
from .file_writer import write_files, UnsafePathError
from .memory import save_session, auto_learn
from .models import DecisionBrief, NodeType, SessionState
from .phases.analyze import run_analysis
from .phases.audit import run_audit
from .phases.code import run_coding
from .phases.plan import run_planning
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

        while state.current_node not in (NodeType.DONE, NodeType.FAILED):
            state = self._step(state)

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
        """Phase 2: Collaborative planning."""
        self.on_message("system", "Claudex", "Phase 2: Collaborative planning...")

        state.plan = run_planning(
            state,
            self.claude,
            self.codex,
            roles_dir=self.roles_dir,
            max_rounds=self.config.planning_max_rounds,
            stall_threshold=self.config.stall_threshold,
            on_message=self.on_message,
        )

        rounds_used = len(state.plan.rounds)
        consensus = state.plan.rounds[-1].consensus_reached if state.plan.rounds else False
        self.on_message("system", "Claudex", f"Planning complete: {rounds_used} rounds, consensus: {consensus}")

        state.current_node = NodeType.CODE
        return state

    def _handle_code(self, state: SessionState) -> SessionState:
        """Phase 3: Code generation."""
        self.on_message("system", "Claudex", "Phase 3: Codex generating code...")

        state.code_result = run_coding(state, self.codex, self.on_message)

        if not state.code_result.files:
            self.on_message("system", "Claudex", "ERROR: No files generated.")
            state.current_node = NodeType.FAILED
            return state

        self.on_message("system", "Claudex", f"Generated {len(state.code_result.files)} file(s)")
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
        for f in (state.code_result.files if state.code_result else []):
            files_summary.append({
                "path": f.path,
                "action": f.action,
                "lines": f.content.count("\n") + 1,
            })

        last_audit = state.audit_results[-1] if state.audit_results else None
        unresolved = [i.issue for i in last_audit.issues] if last_audit and not last_audit.approved else []

        return DecisionBrief(
            what_was_built=state.code_result.explanation if state.code_result else "",
            why_this_approach=state.plan.agreed_plan[:500] if state.plan else "",
            alternatives_rejected=state.plan.alternatives_rejected if state.plan else [],
            unresolved_concerns=unresolved,
            files_summary=files_summary,
        )

    def write_approved_files(self, state: SessionState) -> list:
        """Write the approved files to disk."""
        if not state.code_result or not state.code_result.files:
            return ["No files to write."]

        try:
            return write_files(
                state.code_result.files,
                state.target_dir,
                backup=self.config.backup_files,
            )
        except UnsafePathError as e:
            # Path confinement (Wave 1.1): refuse the whole batch, fail loudly,
            # never write a partial set outside the target directory.
            self.on_message("system", "Claudex", f"ERROR: unsafe path — no files written. {e}")
            return [f"REJECTED — no files written (unsafe path detected). {e}"]
