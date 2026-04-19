"""Consensus detection and stall detection for multi-agent debate."""

import json
import re
from dataclasses import dataclass, field


@dataclass
class ConsensusState:
    """Tracks consensus progress across debate rounds."""
    concerns_history: list[list[str]] = field(default_factory=list)
    stall_count: int = 0
    stall_threshold: int = 2

    def check_consensus(self, response_text: str) -> dict:
        """Parse a response to determine consensus status.

        Returns dict with:
            - agreed: bool
            - concerns: list[str]
            - position_summary: str
            - source: "json" | "text"

        Key behavior: agreed=true WITH concerns is still agreement.
        Concerns are informational notes, not blockers — only agreed=false
        means the agent disagrees.
        """
        json_block = self._extract_json(response_text)
        if json_block:
            return {
                "agreed": json_block.get("agreed", False),
                "concerns": json_block.get("concerns", []),
                "position_summary": json_block.get("position", response_text[:200]),
                "source": "json",
            }

        return self._infer_from_text(response_text)

    def update(self, concerns: list[str]) -> bool:
        """Update stall detection with latest concerns. Returns True if stalled."""
        # Normalize concerns for comparison
        normalized = [self._normalize(c) for c in concerns]
        self.concerns_history.append(normalized)

        if len(self.concerns_history) >= 2:
            prev = set(self.concerns_history[-2])
            curr = set(normalized)
            if prev and curr:
                overlap = len(prev & curr) / max(len(prev | curr), 1)
                if overlap > 0.5:  # >50% overlap = stalling
                    self.stall_count += 1
                else:
                    self.stall_count = 0
            elif not prev and not curr:
                # Both empty = already agreed, not stalling
                self.stall_count = 0
            else:
                self.stall_count = 0

        return self.stall_count >= self.stall_threshold

    def _normalize(self, concern: str) -> str:
        """Normalize a concern string for comparison.

        Strips whitespace, lowercases, removes filler words to detect
        semantically duplicate concerns across rounds.
        """
        c = concern.lower().strip()
        # Remove common filler
        for word in ["should", "would", "could", "might", "also", "the", "a", "an"]:
            c = c.replace(f" {word} ", " ")
        # Collapse whitespace
        c = re.sub(r'\s+', ' ', c).strip()
        return c

    def _extract_json(self, text: str) -> dict | None:
        """Extract the LAST valid consensus JSON block from the response.

        Agents are prompted to include a block with consensus_block: true.
        We scan for the LAST matching block to avoid template echoes
        at the start of the response.
        """
        candidates = []

        # Look for ```json ... ``` blocks
        for match in re.finditer(r'```json\s*\n?(.*?)\n?\s*```', text, re.DOTALL):
            try:
                data = json.loads(match.group(1))
                if isinstance(data, dict) and "agreed" in data:
                    candidates.append(data)
            except json.JSONDecodeError:
                continue

        # Also scan for bare JSON objects with "agreed"
        # Use raw_decode to find valid JSON at each { position
        decoder = json.JSONDecoder()
        for match in re.finditer(r'\{', text):
            try:
                data, _ = decoder.raw_decode(text, match.start())
                if isinstance(data, dict) and "agreed" in data:
                    candidates.append(data)
            except (json.JSONDecodeError, ValueError):
                continue

        if not candidates:
            return None

        # Prefer blocks with consensus_block sentinel
        sentinel_blocks = [c for c in candidates if c.get("consensus_block")]
        if sentinel_blocks:
            return sentinel_blocks[-1]  # last sentinel block

        # Fall back to last block with "agreed"
        return candidates[-1]

    def _infer_from_text(self, text: str) -> dict:
        """Infer consensus status from natural language response.

        Used only when no JSON block is found. More lenient than before —
        hedging words like "however" don't automatically mean disagreement.
        """
        lower = text.lower()

        # Strong agreement signals
        agree_signals = [
            "i agree", "looks good", "that works", "consensus reached",
            "i'm on board", "let's go with", "approved", "no concerns",
            "solid approach", "well thought out", "the approach is solid",
            "the plan is solid", "proceed", "implementing as specified",
        ]

        # Strong disagreement signals (not hedging words)
        disagree_signals = [
            "i disagree", "i reject", "won't work", "fundamentally flawed",
            "cannot accept", "must change", "blocking concern",
            "this approach fails",
        ]

        agree_score = sum(1 for s in agree_signals if s in lower)
        disagree_score = sum(1 for s in disagree_signals if s in lower)

        # Ratio-based, not zero-tolerance
        agreed = agree_score > 0 and agree_score > disagree_score * 2

        # Extract concerns
        concerns = []
        for sentence in re.split(r'[.!?\n]', text):
            sentence = sentence.strip()
            if any(kw in sentence.lower() for kw in ["concern", "risk", "caveat", "guard"]):
                if sentence and len(sentence) > 10:
                    concerns.append(sentence)

        return {
            "agreed": agreed,
            "concerns": concerns,
            "position_summary": text[:200],
            "source": "text",
        }
