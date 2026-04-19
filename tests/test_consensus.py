"""Regression tests for consensus parsing behavior."""

import unittest

from claudex.consensus import ConsensusState


class TestConsensusJSON(unittest.TestCase):
    """Tests for strict JSON-based consensus parsing."""

    def test_agreed_with_sentinel(self):
        response = '''Some analysis here.
```json
{"consensus_block": true, "agreed": true, "concerns": ["minor note"], "position": "Looks good."}
```'''
        state = ConsensusState()
        result = state.check_consensus(response)
        self.assertTrue(result["agreed"])
        self.assertEqual(result["source"], "json")
        self.assertEqual(result["concerns"], ["minor note"])

    def test_last_block_wins_over_template_echo(self):
        response = '''Template: {"agreed": false, "concerns": ["old concern"], "position": "template"}
Actual verdict:
```json
{"consensus_block": true, "agreed": true, "concerns": ["final note"], "position": "Ship it."}
```'''
        state = ConsensusState()
        result = state.check_consensus(response)
        self.assertTrue(result["agreed"])
        self.assertEqual(result["position_summary"], "Ship it.")

    def test_missing_block_returns_not_agreed(self):
        response = "I think the approach is fine. Let's proceed."
        state = ConsensusState()
        result = state.check_consensus(response)
        self.assertFalse(result["agreed"])
        self.assertEqual(result["source"], "missing_json")
        self.assertIn("CONSENSUS_BLOCK_MISSING", result["concerns"])

    def test_agreed_false(self):
        response = '''I disagree.
```json
{"consensus_block": true, "agreed": false, "concerns": ["Security flaw in auth design"], "position": "Needs rework."}
```'''
        state = ConsensusState()
        result = state.check_consensus(response)
        self.assertFalse(result["agreed"])
        self.assertEqual(len(result["concerns"]), 1)

    def test_final_plan_extraction(self):
        response = '''Agreed.
```json
{"consensus_block": true, "agreed": true, "concerns": [], "position": "Ready.", "final_plan": "Create app.py with Flask routes."}
```'''
        state = ConsensusState()
        result = state.check_consensus(response)
        self.assertTrue(result["agreed"])
        self.assertEqual(result["final_plan"], "Create app.py with Flask routes.")

    def test_extract_block_public_method(self):
        response = '''Text.
```json
{"consensus_block": true, "agreed": true, "concerns": [], "position": "ok"}
```'''
        state = ConsensusState()
        block = state.extract_block(response)
        self.assertIsNotNone(block)
        self.assertTrue(block["agreed"])


class TestStallDetection(unittest.TestCase):
    """Tests for concern-based stall detection."""

    def test_no_stall_on_different_concerns(self):
        state = ConsensusState(stall_threshold=2)
        self.assertFalse(state.update(["auth issue"]))
        self.assertFalse(state.update(["perf issue"]))
        self.assertFalse(state.update(["deploy issue"]))

    def test_stall_on_repeated_concerns(self):
        state = ConsensusState(stall_threshold=2)
        state.update(["auth concern", "perf concern"])
        state.update(["auth concern", "perf concern"])
        stalled = state.update(["auth concern", "perf concern"])
        self.assertTrue(stalled)

    def test_no_stall_on_empty_concerns(self):
        state = ConsensusState(stall_threshold=2)
        self.assertFalse(state.update([]))
        self.assertFalse(state.update([]))


if __name__ == "__main__":
    unittest.main()
