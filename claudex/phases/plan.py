"""Phase 2: Collaborative Planning — Claude and Codex debate the approach."""

from pathlib import Path

from ..models import SessionState, DebateRound, DebateMessage, PlanResult
from ..providers.base import LLMProvider
from ..consensus import ConsensusState
from ..roles import build_expert_prompt


def run_planning(
    state: SessionState,
    claude: LLMProvider,
    codex: LLMProvider,
    roles_dir: Path,
    max_rounds: int = 10,
    on_message=None,
) -> PlanResult:
    """Run collaborative planning between Claude and Codex.

    Claude proposes the approach, Codex evaluates and counter-proposes.
    Continue until genuine consensus or progress stalls.

    Args:
        state: Current session state (with analysis results)
        claude: Claude CLI provider
        codex: Codex CLI provider
        roles_dir: Path to role YAML files
        max_rounds: Safety cap on planning rounds
        on_message: Optional callback(agent, role, content) for live output
    """
    analysis = state.analysis
    consensus = ConsensusState()
    rounds: list[DebateRound] = []
    alternatives_rejected: list[str] = []

    # Build expert prompts
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

    # Context that both agents share
    shared_context = f"""TASK: {state.task}

TASK ANALYSIS:
{analysis.task_summary}

PROJECT CONTEXT:
{analysis.project_context}

COMPLEXITY: {analysis.complexity}
"""

    conversation_history = ""

    for round_num in range(1, max_rounds + 1):
        debate_round = DebateRound(round_number=round_num)

        # --- Claude's turn (Architect) ---
        if round_num == 1:
            claude_input = f"""{shared_context}

You are starting the planning phase. Propose a technical approach:
1. Architecture and file structure
2. Key implementation decisions
3. Libraries and patterns to use
4. Potential risks or challenges

Be specific and concrete. Your partner (Senior Developer) will evaluate your proposal."""
        else:
            claude_input = f"""{shared_context}

CONVERSATION SO FAR:
{conversation_history}

Continue the planning discussion. Respond to your partner's points.
If you agree with their assessment, say so with specific reasoning.
If you disagree, explain why with concrete alternatives."""

        claude_response = claude.send(claude_input, system_prompt=claude_prompt)
        if not claude_response.success:
            if on_message:
                on_message("claude", analysis.claude_role, f"[ERROR] {claude_response.error}")
            break

        claude_msg = DebateMessage(agent="claude", role=analysis.claude_role, content=claude_response.content)
        debate_round.messages.append(claude_msg)
        state.add_message("claude", analysis.claude_role, claude_response.content)
        conversation_history += f"\n\n[ARCHITECT (Claude)]: {claude_response.content}"

        if on_message:
            on_message("claude", analysis.claude_role, claude_response.content)

        # --- Codex's turn (Developer) ---
        codex_input = f"""{shared_context}

CONVERSATION SO FAR:
{conversation_history}

Evaluate the architect's proposal from an implementation perspective:
- Is this practical to implement?
- Are there simpler alternatives?
- What implementation challenges do you foresee?
- Do you agree with the approach?

Be honest. If the approach is solid, say so with reasoning.
If you see problems, explain them with specific alternatives."""

        codex_response = codex.send(codex_input, system_prompt=codex_prompt)
        if not codex_response.success:
            if on_message:
                on_message("codex", analysis.codex_role, f"[ERROR] {codex_response.error}")
            break

        codex_msg = DebateMessage(agent="codex", role=analysis.codex_role, content=codex_response.content)
        debate_round.messages.append(codex_msg)
        state.add_message("codex", analysis.codex_role, codex_response.content)
        conversation_history += f"\n\n[DEVELOPER (Codex)]: {codex_response.content}"

        if on_message:
            on_message("codex", analysis.codex_role, codex_response.content)

        # --- Check consensus ---
        claude_status = consensus.check_consensus(claude_response.content)
        codex_status = consensus.check_consensus(codex_response.content)

        all_concerns = claude_status["concerns"] + codex_status["concerns"]
        debate_round.remaining_concerns = all_concerns

        both_agree = claude_status["agreed"] and codex_status["agreed"]

        if both_agree and not all_concerns:
            debate_round.consensus_reached = True
            rounds.append(debate_round)
            break

        # Check for stall
        is_stalled = consensus.update(all_concerns)
        if is_stalled:
            if on_message:
                on_message("system", "Claudex", "Progress has stalled — same concerns repeating. Moving forward with current plan.")
            debate_round.consensus_reached = True  # forced consensus
            rounds.append(debate_round)
            break

        # Track rejected alternatives
        for concern in all_concerns:
            if "instead" in concern.lower() or "alternative" in concern.lower():
                alternatives_rejected.append(concern)

        rounds.append(debate_round)

    # Build the agreed plan from the last round's discussion
    agreed_plan = conversation_history

    return PlanResult(
        agreed_plan=agreed_plan,
        rounds=rounds,
        alternatives_rejected=alternatives_rejected,
    )
