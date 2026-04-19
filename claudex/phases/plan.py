"""Phase 2: Collaborative Planning — Claude and Codex debate the approach."""

from pathlib import Path

from ..models import SessionState, DebateRound, DebateMessage, PlanResult
from ..providers.base import LLMProvider
from ..consensus import ConsensusState
from ..roles import build_expert_prompt

# Keep last N rounds verbatim, summarize older ones
RECENT_ROUNDS_FULL = 2


def _trim_history(rounds: list[DebateRound], current_history: str) -> str:
    """Trim conversation history to prevent context bloat.

    Keeps last RECENT_ROUNDS_FULL rounds verbatim. Older rounds get
    replaced with a one-line position summary from their consensus block.
    """
    if len(rounds) <= RECENT_ROUNDS_FULL:
        return current_history

    # Build a compact summary of older rounds
    summary_parts = []
    for r in rounds[:-RECENT_ROUNDS_FULL]:
        for msg in r.messages:
            # Extract position from the message (first 100 chars)
            short = msg.content[:150].replace("\n", " ").strip()
            label = "ARCHITECT" if msg.agent == "claude" else "DEVELOPER"
            summary_parts.append(f"[{label} Round {r.round_number}]: {short}...")

    # Build recent history from the last N rounds
    recent_parts = []
    for r in rounds[-RECENT_ROUNDS_FULL:]:
        for msg in r.messages:
            label = "ARCHITECT (Claude)" if msg.agent == "claude" else "DEVELOPER (Codex)"
            recent_parts.append(f"\n\n[{label}]: {msg.content}")

    trimmed = "EARLIER ROUNDS (summarized):\n" + "\n".join(summary_parts)
    trimmed += "\n\nRECENT DISCUSSION:" + "".join(recent_parts)
    return trimmed


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

        # Trim history for older rounds to prevent context bloat
        if rounds:
            conversation_history = _trim_history(rounds, conversation_history)

        # --- Claude's turn (Architect) ---
        if round_num == 1:
            claude_input = f"""{shared_context}

You are starting the planning phase. Propose a technical approach:
1. Architecture and file structure
2. Key implementation decisions
3. Libraries and patterns to use
4. Potential risks or challenges

Be specific and concrete. Your partner (Senior Developer) will evaluate your proposal.

End with a consensus block:
```json
{{"consensus_block": true, "agreed": true/false, "concerns": ["list any concerns"], "position": "one-line summary"}}
```"""
        else:
            claude_input = f"""{shared_context}

CONVERSATION SO FAR:
{conversation_history}

Continue the planning discussion. Respond to your partner's points.
If you agree with their assessment, say so with specific reasoning.
If you disagree, explain why with concrete alternatives.
If consensus is reached, say so clearly — don't keep debating for its own sake.

End with a consensus block:
```json
{{"consensus_block": true, "agreed": true/false, "concerns": ["list any concerns"], "position": "one-line summary"}}
```"""

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
If you see problems, explain them with specific alternatives.
If you agree, don't keep listing concerns just to seem thorough — say you agree and why.

End with a consensus block:
```json
{{"consensus_block": true, "agreed": true/false, "concerns": ["list any concerns or empty"], "position": "one-line summary"}}
```"""

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

        # KEY FIX: agreed=true means consensus, even if concerns are listed.
        # Concerns are informational notes, not blockers.
        if both_agree:
            debate_round.consensus_reached = True
            rounds.append(debate_round)
            if on_message and all_concerns:
                on_message("system", "Claudex", f"Consensus reached with {len(all_concerns)} noted concern(s) — proceeding.")
            break

        # Check for stall
        is_stalled = consensus.update(all_concerns)
        if is_stalled:
            if on_message:
                on_message("system", "Claudex", "Progress has stalled — same concerns repeating. Moving forward with current plan.")
            debate_round.consensus_reached = True  # forced consensus
            rounds.append(debate_round)
            break

        # Track rejected alternatives from the consensus JSON
        for status in (claude_status, codex_status):
            if isinstance(status, dict):
                # Check for rejected_alternatives in the JSON block
                json_block = consensus._extract_json(
                    claude_response.content if status == claude_status else codex_response.content
                )
                if json_block and "rejected_alternatives" in json_block:
                    alternatives_rejected.extend(json_block["rejected_alternatives"])

        rounds.append(debate_round)

    # Build the agreed plan — use trimmed history for the code phase
    agreed_plan = _trim_history(rounds, conversation_history) if rounds else conversation_history

    return PlanResult(
        agreed_plan=agreed_plan,
        rounds=rounds,
        alternatives_rejected=alternatives_rejected,
    )
