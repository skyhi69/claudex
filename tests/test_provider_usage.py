"""Tests for the base-provider quota ledger (Wave 1.4)."""

import unittest

from claudex.providers.base import LLMProvider, LLMResponse


class FakeProvider(LLMProvider):
    def __init__(self, responses):
        super().__init__()
        self._responses = list(responses)
        self.seen_kwargs = []

    @property
    def name(self):
        return "fake"

    def _cli_command(self):
        return "fake"

    def _send(self, prompt, system_prompt="", **kwargs):
        self.seen_kwargs.append(kwargs)
        return self._responses.pop(0)


def _resp(i=0, ci=0, o=0):
    return LLMResponse(content="x", provider="fake", success=True,
                       input_tokens=i, cached_input_tokens=ci, output_tokens=o)


class TestProviderLedger(unittest.TestCase):

    def test_accumulates_calls_and_tokens(self):
        p = FakeProvider([_resp(100, 40, 20), _resp(10, 5, 2)])
        p.send("a")
        p.send("b", system_prompt="sys")
        self.assertEqual(p.call_count, 2)
        self.assertEqual(p.usage_totals["input_tokens"], 110)
        self.assertEqual(p.usage_totals["cached_input_tokens"], 45)
        self.assertEqual(p.usage_totals["output_tokens"], 22)

    def test_counts_zero_usage_calls(self):
        p = FakeProvider([_resp()])
        p.send("a")
        self.assertEqual(p.call_count, 1)
        self.assertEqual(p.usage_totals, {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0})

    def test_passes_kwargs_to_impl(self):
        p = FakeProvider([_resp()])
        p.send("a", cwd="/some/dir")
        self.assertEqual(p.seen_kwargs[-1], {"cwd": "/some/dir"})


if __name__ == "__main__":
    unittest.main()
