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

        Looks for structured markers in the response. If the agent
        includes a JSON block with consensus fields, we parse it.
        Otherwise, we infer from the text.

        Returns dict with:
            - agreed: bool
            - concerns: list[str]
            - position_summary: str
        """
        # Try to extract a JSON block from the response
        json_block = self._extract_json(response_text)
        if json_block:
            return {
                "agreed": json_block.get("agreed", False),
                "concerns": json_block.get("concerns", []),
                "position_summary": json_block.get("position", response_text[:200]),
            }

        # Fall back to text-based inference
        return self._infer_from_text(response_text)

    def update(self, concerns: list[str]) -> bool:
        """Update stall detection with latest concerns. Returns True if stalled."""
        self.concerns_history.append(concerns)

        if len(self.concerns_history) >= 2:
            prev = set(self.concerns_history[-2])
            curr = set(concerns)
            # If concerns are substantially the same, increment stall counter
            if prev and curr and len(prev & curr) / max(len(prev | curr), 1) > 0.7:
                self.stall_count += 1
            else:
                self.stall_count = 0

        return self.stall_count >= self.stall_threshold

    def _extract_json(self, text: str) -> dict | None:
        """Try to extract a JSON block from the response text.

        Agents are prompted to include a consensus block like:
        ```json
        {"agreed": true, "concerns": [], "position": "summary..."}
        ```
        """
        # Look for ```json ... ``` blocks
        patterns = [
            r'```json\s*\n?(.*?)\n?\s*```',
            r'```\s*\n?(.*?)\n?\s*```',
        ]
        for pattern in patterns:
            match = re.search(pattern, text, re.DOTALL)
            if match:
                try:
                    data = json.loads(match.group(1))
                    if isinstance(data, dict) and "agreed" in data:
                        return data
                except json.JSONDecodeError:
                    continue

        # Look for inline JSON with consensus fields
        try:
            # Find anything that looks like a JSON object with "agreed"
            match = re.search(r'\{[^{}]*"agreed"[^{}]*\}', text)
            if match:
                data = json.loads(match.group(0))
                if isinstance(data, dict):
                    return data
        except (json.JSONDecodeError, AttributeError):
            pass

        return None

    def _infer_from_text(self, text: str) -> dict:
        """Infer consensus status from natural language response."""
        lower = text.lower()

        # Strong agreement signals
        agree_signals = [
            "i agree", "looks good", "that works", "consensus reached",
            "i'm on board", "let's go with", "approved", "no concerns",
            "solid approach", "well thought out",
        ]

        # Strong disagreement signals
        disagree_signals = [
            "i disagree", "concern", "issue with", "problem with",
            "however", "but i think", "instead we should", "won't work",
            "better approach", "reconsider",
        ]

        agree_score = sum(1 for s in agree_signals if s in lower)
        disagree_score = sum(1 for s in disagree_signals if s in lower)

        agreed = agree_score > disagree_score and disagree_score == 0

        # Extract concerns (sentences containing concern-like keywords)
        concerns = []
        for sentence in text.split("."):
            sentence = sentence.strip()
            if any(kw in sentence.lower() for kw in ["concern", "issue", "problem", "risk", "worry"]):
                if sentence:
                    concerns.append(sentence)

        return {
            "agreed": agreed,
            "concerns": concerns,
            "position_summary": text[:200],
        }
