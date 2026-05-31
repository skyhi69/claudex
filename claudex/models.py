"""Data models for Claudex sessions, phases, and results."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional


class NodeType(Enum):
    """State machine nodes for the Claudex pipeline."""

    INIT = "init"
    ANALYZE = "analyze"
    PLAN = "plan"
    CODE = "code"
    AUDIT = "audit"
    RESOLVE = "resolve"
    DONE = "done"
    FAILED = "failed"


@dataclass
class FileOutput:
    """A single file to be written."""

    path: str
    content: str
    action: str  # "create" | "modify" | "delete"


@dataclass
class DebateMessage:
    """A single message in a debate round."""

    agent: str  # "claude" or "codex"
    role: str
    content: str
    timestamp: float = field(default_factory=time.time)


@dataclass
class DebateRound:
    """One round of back-and-forth in the planning phase."""

    round_number: int
    messages: list[DebateMessage] = field(default_factory=list)
    consensus_reached: bool = False
    remaining_concerns: list = field(default_factory=list)
    rolling_summary: str = ""  # compact summary for history trimming


@dataclass
class AnalysisResult:
    """Output of the task analysis phase."""

    task_summary: str
    required_expertise: list
    claude_role: str
    codex_role: str
    project_context: str
    complexity: str  # "simple" | "moderate" | "complex"


@dataclass
class PlanResult:
    """Output of the collaborative planning phase."""

    agreed_plan: str
    rounds: list[DebateRound] = field(default_factory=list)
    alternatives_rejected: list = field(default_factory=list)


@dataclass
class CodeResult:
    """Output of the code generation phase."""

    files: list[FileOutput] = field(default_factory=list)
    explanation: str = ""


@dataclass
class AuditIssue:
    """A specific issue found during audit."""

    severity: str  # "critical" | "high" | "medium" | "low"
    file: str
    issue: str
    suggested_fix: str


@dataclass
class AuditResult:
    """Output of the audit phase."""

    approved: bool
    assessment: str
    issues: list[AuditIssue] = field(default_factory=list)
    feedback_for_coder: str = ""


@dataclass
class DecisionBrief:
    """Final summary presented to the user."""

    what_was_built: str
    why_this_approach: str
    alternatives_rejected: list
    unresolved_concerns: list
    files_summary: list


@dataclass
class SessionState:
    """Full state of a Claudex session."""

    session_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    task: str = ""
    target_dir: Optional[Path] = None
    current_node: NodeType = NodeType.INIT

    analysis: Optional[AnalysisResult] = None
    plan: Optional[PlanResult] = None
    code_result: Optional[CodeResult] = None
    audit_results: list[AuditResult] = field(default_factory=list)
    decision_brief: Optional[DecisionBrief] = None

    resolve_iteration: int = 0

    # Per-provider quota ledger (Wave 1.4): {"claude": {calls, input_tokens,
    # cached_input_tokens, output_tokens}, "codex": {...}}. Quota gauge, not $.
    usage_summary: dict = field(default_factory=dict)

    transcript: list[DebateMessage] = field(default_factory=list)

    def add_message(self, agent: str, role: str, content: str) -> None:
        """Add a message to the session transcript."""
        self.transcript.append(
            DebateMessage(agent=agent, role=role, content=content)
        )
