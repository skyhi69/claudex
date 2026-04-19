"""Consensus detection and stall detection for multi-agent debate."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field


@dataclass
class ConsensusState:
    """Tracks consensus progress across debate rounds."""

    concerns_history: list[list] = field(default_factory=list)
    stall_count: int = 0
    stall_threshold: int = 2

    def check_consensus(self, response_text: str) -> dict:
        """Parse a response to determine consensus status.

        Returns a dict with:
            - agreed: bool
            - concerns: list
            - position_summary: str
            - final_plan: str
            - source: "json" | "missing_json"

        The planning prompts require a JSON consensus block. If it is missing,
        treat that as prompt non-compliance rather than inferring agreement
        from free-form prose.
        """
        json_block = self._extract_json(response_text)
        if json_block is None:
            return {
                "agreed": False,
                "concerns": ["CONSENSUS_BLOCK_MISSING"],
                "position_summary": "Consensus block missing from response.",
                "final_plan": "",
                "source": "missing_json",
            }

        concerns = json_block.get("concerns", [])
        if not isinstance(concerns, list):
            concerns = []

        position = json_block.get("position", "")
        if not isinstance(position, str) or not position.strip():
            position = response_text[:200].replace("\n", " ").strip()

        final_plan = json_block.get("final_plan", "")
        if not isinstance(final_plan, str):
            final_plan = ""

        return {
            "agreed": bool(json_block.get("agreed", False)),
            "concerns": [str(c) for c in concerns if str(c).strip()],
            "position_summary": position.strip(),
            "final_plan": final_plan.strip(),
            "source": "json",
        }

    def update(self, concerns: list) -> bool:
        """Update stall detection with latest concerns. Returns True if stalled."""
        normalized = [self._normalize(c) for c in concerns]
        self.concerns_history.append(normalized)

        if len(self.concerns_history) >= 2:
            prev = set(self.concerns_history[-2])
            curr = set(normalized)
            if prev and curr:
                overlap = len(prev & curr) / max(len(prev | curr), 1)
                if overlap > 0.5:
                    self.stall_count += 1
                else:
                    self.stall_count = 0
            elif not prev and not curr:
                self.stall_count = 0
            else:
                self.stall_count = 0

        return self.stall_count >= self.stall_threshold

    def extract_block(self, response_text: str) -> dict | None:
        """Expose raw consensus JSON extraction for planning helpers."""
        return self._extract_json(response_text)

    def _normalize(self, concern: str) -> str:
        """Normalize a concern string for comparison."""
        normalized = concern.lower().strip()
        for word in ["should", "would", "could", "might", "also", "the", "a", "an"]:
            normalized = normalized.replace(f" {word} ", " ")
        normalized = re.sub(r"\s+", " ", normalized).strip()
        return normalized

    def _extract_json(self, text: str) -> dict | None:
        """Extract the last valid consensus JSON block from the response.

        Agents are prompted to include a block with consensus_block: true.
        We prefer the last matching sentinel block to avoid template echoes.
        """
        candidates: list[dict] = []

        for match in re.finditer(r"```json\s*\n?(.*?)\n?\s*```", text, re.DOTALL):
            try:
                data = json.loads(match.group(1))
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict) and "agreed" in data:
                candidates.append(data)

        decoder = json.JSONDecoder()
        for match in re.finditer(r"\{", text):
            try:
                data, _ = decoder.raw_decode(text, match.start())
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(data, dict) and "agreed" in data:
                candidates.append(data)

        if not candidates:
            return None

        sentinel_blocks = [c for c in candidates if c.get("consensus_block")]
        if sentinel_blocks:
            return sentinel_blocks[-1]

        return candidates[-1]
