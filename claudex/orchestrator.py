"""Claudex orchestrator — graph-based state machine for the multi-agent pipeline."""

import os
from pathlib import Path

from .models import SessionState, NodeType, DecisionBrief
from .config import ClaudexConfig
from .providers.claude import ClaudeProvider
from .providers.codex import CodexProvider
from .phases.analyze import run_analysis
from .phases.plan import run_planning
from .phases.code import run_coding
from .phases.audit import run_audit
from .phases.resolve import run_resolution
from .file_writer import write_files


class Orchestrator:
    """Drives the Claudex pipeline: Analyze → Plan → Code → Audit → Resolve → Done."""

    def __init__(self, config: ClaudexConfig, roles_dir: Path, on_message=None):
        self.config = config
        self.roles_dir = roles_dir
        self.claude = ClaudeProvider()
        self.codex = CodexProvider()
        self.on_message = on_message or (lambda *a: None)

    def run(self, task: str, target_dir: Path) -> SessionState:
        """Execute the full Claudex pipeline.

        Args:
            task: The coding task description
            target_dir: Directory to write files to

        Returns:
            Final SessionState with all results
        """
        state = SessionState(task=task, target_dir=target_dir)

        # Set project dir for codex provider (needs a git repo to run in)
        os.environ["CLAUDEX_PROJECT_DIR"] = str(target_dir)

        # Walk the state machine
        while state.current_node not in (NodeType.DONE, NodeType.FAILED):
            state = self._step(state)

        return state

    def _step(self, state: SessionState) -> SessionState:
        """Execute one step of the state machine."""

        if state.current_node == NodeType.INIT:
            return self._handle_init(state)
        elif state.current_node == NodeType.ANALYZE:
            return self._handle_analyze(state)
        elif state.current_node == NodeType.PLAN:
            return self._handle_plan(state)
        elif state.current_node == NodeType.CODE:
            return self._handle_code(state)
        elif state.current_node == NodeType.AUDIT:
            return self._handle_audit(state)
        elif state.current_node == NodeType.RESOLVE:
            return self._handle_resolve(state)
        else:
            state.current_node = NodeType.FAILED
            return state

    def _handle_init(self, state: SessionState) -> SessionState:
        """Validate prerequisites and move to analysis."""
        self.on_message("system", "Claudex", f"Session {state.session_id} started")

        # Check providers are available
        if not self.claude.is_available():
            self.on_message("system", "Claudex", "ERROR: Claude CLI not found. Is it installed and authenticated?")
            state.current_node = NodeType.FAILED
            return state

        if not self.codex.is_available():
            self.on_message("system", "Claudex", "ERROR: Codex CLI not found. Run: npm install -g @openai/codex")
            state.current_node = NodeType.FAILED
            return state

        # Ensure target directory exists
        state.target_dir.mkdir(parents=True, exist_ok=True)

        state.current_node = NodeType.ANALYZE
        return state

    def _handle_analyze(self, state: SessionState) -> SessionState:
        """Phase 1: Analyze the task."""
        self.on_message("system", "Claudex", "Phase 1: Analyzing task...")

        state.analysis = run_analysis(state, self.claude)

        self.on_message("system", "Claudex",
                        f"Task complexity: {state.analysis.complexity}")
        self.on_message("system", "Claudex",
                        f"Claude role: {state.analysis.claude_role}")
        self.on_message("system", "Claudex",
                        f"Codex role: {state.analysis.codex_role}")

        state.current_node = NodeType.PLAN
        return state

    def _handle_plan(self, state: SessionState) -> SessionState:
        """Phase 2: Collaborative planning."""
        self.on_message("system", "Claudex", "Phase 2: Collaborative planning...")

        state.plan = run_planning(
            state, self.claude, self.codex,
            roles_dir=self.roles_dir,
            max_rounds=self.config.planning_max_rounds,
            on_message=self.on_message,
        )

        rounds_used = len(state.plan.rounds)
        consensus = state.plan.rounds[-1].consensus_reached if state.plan.rounds else False
        self.on_message("system", "Claudex",
                        f"Planning complete: {rounds_used} rounds, consensus: {consensus}")

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

        file_count = len(state.code_result.files)
        self.on_message("system", "Claudex", f"Generated {file_count} file(s)")

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
        else:
            issue_count = len(audit_result.issues)
            self.on_message("system", "Claudex",
                            f"Code REJECTED — {issue_count} issue(s) found. Moving to resolution...")
            state.current_node = NodeType.RESOLVE

        return state

    def _handle_resolve(self, state: SessionState) -> SessionState:
        """Phase 5: Resolution loop."""
        self.on_message("system", "Claudex", "Phase 5: Resolution...")

        approved = run_resolution(
            state, self.claude, self.codex,
            max_iterations=self.config.resolve_max_iterations,
            on_message=self.on_message,
        )

        state.decision_brief = self._build_brief(state)

        if approved:
            state.current_node = NodeType.DONE
        else:
            # Still present results even if not fully approved
            self.on_message("system", "Claudex",
                            "Resolution did not reach full approval. Presenting current state.")
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
        unresolved = []
        if last_audit and not last_audit.approved:
            unresolved = [i.issue for i in last_audit.issues]

        return DecisionBrief(
            what_was_built=state.code_result.explanation if state.code_result else "",
            why_this_approach=state.plan.agreed_plan[:500] if state.plan else "",
            alternatives_rejected=state.plan.alternatives_rejected if state.plan else [],
            unresolved_concerns=unresolved,
            files_summary=files_summary,
        )

    def write_approved_files(self, state: SessionState) -> list[str]:
        """Write the approved files to disk."""
        if not state.code_result or not state.code_result.files:
            return ["No files to write."]

        return write_files(
            state.code_result.files,
            state.target_dir,
            backup=self.config.backup_files,
        )
