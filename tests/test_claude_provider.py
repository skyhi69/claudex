"""Tests for Claude provider usage parsing (Wave 1.3)."""

import json
import unittest

from claudex.providers.claude import ClaudeProvider


class TestClaudeParseUsage(unittest.TestCase):

    def setUp(self):
        self.provider = ClaudeProvider()

    def test_maps_real_envelope(self):
        # Shape verified live from `claude -p --output-format json`.
        raw = json.dumps({
            "result": "hi",
            "total_cost_usd": 0.012,  # must be ignored — quota, not dollars
            "usage": {
                "input_tokens": 1200,
                "cache_read_input_tokens": 800,
                "cache_creation_input_tokens": 50,
                "output_tokens": 64,
            },
        })
        u = self.provider._parse_usage(raw)
        self.assertEqual(u["input_tokens"], 1200)
        self.assertEqual(u["cached_input_tokens"], 800)
        self.assertEqual(u["output_tokens"], 64)

    def test_missing_usage_is_zeros(self):
        u = self.provider._parse_usage(json.dumps({"result": "hi"}))
        self.assertEqual(u, {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0})

    def test_non_json_is_zeros(self):
        u = self.provider._parse_usage("not json at all")
        self.assertEqual(u, {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0})


if __name__ == "__main__":
    unittest.main()
