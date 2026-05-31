"""Phase 2: Collaborative Planning - Claude and Codex debate the approach."""

from __future__ import annotations

from pathlib import Path

from ..consensus import ConsensusState
from ..models import DebateMessage, DebateRound, PlanResult, SessionState
from ..providers.base import LLMProvider
from ..roles import build_expert_prompt

RECENT_ROUNDS_FULL = 2


def _build_summary_line(consensus: ConsensusState, round_number: int, message: DebateMessage) -> str:
    """Build a compact structured summary line for an older round message."""
    block = consensus.extract_block(message.content) or {}
    position = block.get("position")
    if not isinstance(position, str) or not position.strip():
        position = message.content[:150].replace("\n", " ").strip()

    concerns = block.get("concerns", [])
    if not isinstance(concerns, list):
        concerns = []

    label = "ARCHITECT" if message.agent == "claude" else "DEVELOPER"
    concern_tail = ""
    concise_concerns = [str(c).strip() for c in concerns[:2] if str(c).strip()]
    if concise_concerns:
        concern_tail = f" | concerns: {'; '.join(concise_concerns)}"

    return f"[{label} R{round_number}]: {position.strip()}{concern_tail}"


def _trim_history(rounds: list[DebateRound]) -> str:
    """Trim conversation history to prevent context bloat.

    Older rounds use rolling summaries. Recent rounds remain verbatim.
    """
    if not rounds:
        return ""

    if len(rounds) <= RECENT_ROUNDS_FULL:
        recent_parts = []
        for debate_round in rounds:
            for message in debate_round.messages:
                label = "ARCHITECT (Claude)" if message.agent == "claude" else "DEVELOPER (Codex)"
                recent_parts.append(f"\n\n[{label}]: {message.content}")
        return "".join(recent_parts).strip()

    # Older rounds: use rolling summaries
    summary_lines = []
    for debate_round in rounds[:-RECENT_ROUNDS_FULL]:
        if debate_round.rolling_summary:
            summary_lines.append(debate_round.rolling_summary)

    # Recent rounds: verbatim
    recent_parts = []
    for debate_round in rounds[-RECENT_ROUNDS_FULL:]:
        for message in debate_round.messages:
            label = "ARCHITECT (Claude)" if message.agent == "claude" else "DEVELOPER (Codex)"
            recent_parts.append(f"\n\n[{label}]: {message.content}")

    parts = []
    if summary_lines:
        parts.append("EARLIER ROUNDS (summarized):\n" + "\n".join(summary_lines))
    if recent_parts:
        parts.append("RECENT DISCUSSION:" + "".join(recent_parts))

    return "\n\n".join(parts).strip()


def _consensus_block_instructions(include_final_plan: bool) -> str:
    """Return the standard consensus block instructions for planning prompts."""
    final_plan_line = ', "final_plan": "concrete implementation plan"' if include_final_plan else ""
    return (
        "End with a consensus block:\n"
        "```json\n"
        '{"consensus_block": true, "agreed": true/false, "concerns": ["list any concerns"], '
        f'"position": "one-line summary"{final_plan_line}}}\n'
        "```"
    )


def _missing_block_reminder(agent_label: str, was_missing: bool) -> str:
    """Return a terse reminder when the previous response missed the JSON block."""
    if not was_missing:
        return ""
    return (
        f"\nFORMAT REMINDER: Your previous {agent_label} response lacked the required JSON "
        "consensus block. You MUST include it at the end of this response.\n"
    )


def run_planning(
    state: SessionState,
    claude: LLMProvider,
    codex: LLMProvider,
    roles_dir: Path,
    max_rounds: int = 10,
    stall_threshold: int = 2,
    on_message=None,
) -> PlanResult:
    """Run collaborative planning between Claude and Codex."""
    analysis = state.analysis
    consensus = ConsensusState(stall_threshold=stall_threshold)
    rounds: list[DebateRound] = []
    alternatives_rejected: list = []

    claude_prompt = build_expert_prompt(
        roles_dir / "architect.yaml",
        analysis.required_expertise,
        state.task,
    )
    codex_prompt = build_expert_prompt(
        roles_dir / "developer.yaml",
        analysis.required_expertise,
        state.task,
    )

    shared_context = f"""TASK: {state.task}

TASK ANALYSIS:
{analysis.task_summary}

PROJECT CONTEXT:
{analysis.project_context}

COMPLEXITY: {analysis.complexity}
"""

    final_plan = ""
    claude_missed_block = False
    codex_missed_block = False

    for round_num in range(1, max_rounds + 1):
        debate_round = DebateRound(round_number=round_num)
        conversation_history = _trim_history(rounds)

        # --- Claude's turn (Architect) ---
        if round_num == 1:
            claude_input = f"""{shared_context}

You are starting the planning phase. Propose a technical approach:
1. Architecture and file structure
2. Key implementation decisions
3. Libraries and patterns to use
4. Potential risks or challenges

Be specific and concrete. Your partner (Senior Developer) will evaluate your proposal.
{_missing_block_reminder("architect", claude_missed_block)}
{_consensus_block_instructions(include_final_plan=False)}"""
        else:
            claude_input = f"""{shared_context}

CONVERSATION SO FAR:
{conversation_history}

Continue the planning discussion. Respond to your partner's points.
If you agree, say so with specific reasoning.
If you disagree, explain why with concrete alternatives.
If consensus is reached, say so clearly and include a concrete final_plan.
{_missing_block_reminder("architect", claude_missed_block)}
{_consensus_block_instructions(include_final_plan=True)}"""

        claude_response = claude.send(claude_input, system_prompt=claude_prompt)
        if not claude_response.success:
            if on_message:
                on_message("claude", analysis.claude_role, f"[ERROR] {claude_response.error}")
                on_message("system", "Claudex",
                           f"Planning halted at round {round_num}: architect call failed — {claude_response.error}")
            break

        claude_msg = DebateMessage(agent="claude", role=analysis.claude_role, content=claude_response.content)
        debate_round.messages.append(claude_msg)
        state.add_message("claude", analysis.claude_role, claude_response.content)

        if on_message:
            on_message("claude", analysis.claude_role, claude_response.content)

        # --- Codex's turn (Developer) ---
        # Build history including Claude's latest message
        codex_history = _trim_history(rounds + [debate_round])

        codex_input = f"""{shared_context}

CONVERSATION SO FAR:
{codex_history}

Evaluate the architect's proposal from an implementation perspective:
- Is this practical to implement?
- Are there simpler alternatives?
- What implementation challenges do you foresee?
- Do you agree with the approach?

Be honest. If the approach is solid, say so with reasoning.
If you see problems, explain them with specific alternatives.
If you agree, don't keep listing concerns just to seem thorough.
{_missing_block_reminder("developer", codex_missed_block)}
{_consensus_block_instructions(include_final_plan=True)}"""

        codex_response = codex.send(codex_input, system_prompt=codex_prompt)
        if not codex_response.success:
            if on_message:
                on_message("codex", analysis.codex_role, f"[ERROR] {codex_response.error}")
                on_message("system", "Claudex",
                           f"Planning halted at round {round_num}: developer call failed — {codex_response.error}")
            break

        codex_msg = DebateMessage(agent="codex", role=analysis.codex_role, content=codex_response.content)
        debate_round.messages.append(codex_msg)
        state.add_message("codex", analysis.codex_role, codex_response.content)

        if on_message:
            on_message("codex", analysis.codex_role, codex_response.content)

        # --- Build rolling summary for this round ---
        summary_parts = []
        for msg in debate_round.messages:
            summary_parts.append(_build_summary_line(consensus, round_num, msg))
        debate_round.rolling_summary = "\n".join(summary_parts)

        # --- Check consensus ---
        claude_status = consensus.check_consensus(claude_response.content)
        codex_status = consensus.check_consensus(codex_response.content)

        # Track missing blocks for reminders
        claude_missed_block = claude_status.get("source") == "missing_json"
        codex_missed_block = codex_status.get("source") == "missing_json"

        all_concerns = claude_status["concerns"] + codex_status["concerns"]
        # Filter out the missing-block sentinel
        real_concerns = [c for c in all_concerns if c != "CONSENSUS_BLOCK_MISSING"]
        debate_round.remaining_concerns = real_concerns

        both_agree = claude_status["agreed"] and codex_status["agreed"]

        # Capture final_plan if provided
        for status in (claude_status, codex_status):
            if status.get("final_plan"):
                final_plan = status["final_plan"]

        # Extract rejected alternatives
        for resp_text in (claude_response.content, codex_response.content):
            block = consensus.extract_block(resp_text) or {}
            rejected = block.get("rejected_alternatives", [])
            if isinstance(rejected, list):
                alternatives_rejected.extend(str(r) for r in rejected)

        if both_agree:
            debate_round.consensus_reached = True
            rounds.append(debate_round)
            if on_message and real_concerns:
                on_message("system", "Claudex", f"Consensus reached with {len(real_concerns)} noted concern(s).")
            break

        # Check for stall
        is_stalled = consensus.update(real_concerns)
        if is_stalled:
            if on_message:
                on_message("system", "Claudex", "Progress has stalled. Moving forward with current plan.")
            debate_round.consensus_reached = True
            rounds.append(debate_round)
            break

        rounds.append(debate_round)

    # Build agreed plan — prefer final_plan from consensus, fall back to trimmed history
    agreed_plan = final_plan if final_plan else _trim_history(rounds)

    return PlanResult(
        agreed_plan=agreed_plan,
        rounds=rounds,
        alternatives_rejected=alternatives_rejected,
    )
