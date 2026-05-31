"""Tests for Codex provider JSONL usage parsing + spawn-refresh retry (Wave 1.3)."""

import types
import unittest
from pathlib import Path
from unittest import mock

from claudex.providers.codex import CodexProvider, _SPAWN_REFRESH, _MAX_ATTEMPTS


def _result(returncode=0, stdout="", stderr=""):
    return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


_JSONL_OK = "\n".join([
    '{"type":"thread.started","thread_id":"t"}',
    '{"type":"turn.started"}',
    '{"type":"item.completed","item":{"id":"i1","type":"agent_message","text":"Hello world"}}',
    '{"type":"turn.completed","usage":{"input_tokens":100,"cached_input_tokens":40,"output_tokens":20,"reasoning_output_tokens":5}}',
])

_JSONL_SPAWN = "\n".join([
    '{"type":"thread.started","thread_id":"t"}',
    '{"type":"turn.started"}',
])
# usage present but NO agent_message — forces content to come from the -o file.
_JSONL_USAGE_ONLY = "\n".join([
    '{"type":"thread.started","thread_id":"t"}',
    '{"type":"turn.completed","usage":{"input_tokens":7,"cached_input_tokens":3,"output_tokens":2}}',
])
_STDERR_SPAWN = f"2026-05-31 ERROR codex_core::exec: exec error: {_SPAWN_REFRESH}"


class TestParseJsonl(unittest.TestCase):

    def test_extracts_message_and_usage(self):
        last_msg, usage = CodexProvider._parse_jsonl(_JSONL_OK)
        self.assertEqual(last_msg, "Hello world")
        self.assertEqual(usage["input_tokens"], 100)
        self.assertEqual(usage["cached_input_tokens"], 40)
        self.assertEqual(usage["output_tokens"], 20)

    def test_ignores_malformed_lines(self):
        noisy = "not json\n{bad\n" + _JSONL_OK + "\nplain text"
        last_msg, usage = CodexProvider._parse_jsonl(noisy)
        self.assertEqual(last_msg, "Hello world")
        self.assertEqual(usage["output_tokens"], 20)

    def test_zero_usage_when_absent(self):
        last_msg, usage = CodexProvider._parse_jsonl('{"type":"turn.started"}')
        self.assertEqual(last_msg, "")
        self.assertEqual(usage, {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0})


class TestCodexSend(unittest.TestCase):

    @mock.patch("claudex.providers.codex.subprocess.run")
    def test_success_parses_usage(self, run):
        run.return_value = _result(0, _JSONL_OK, "")
        resp = CodexProvider().send("do a thing")
        self.assertTrue(resp.success)
        self.assertEqual(resp.content, "Hello world")
        self.assertEqual(resp.input_tokens, 100)
        self.assertEqual(resp.cached_input_tokens, 40)
        self.assertEqual(resp.output_tokens, 20)
        self.assertEqual(run.call_count, 1)

    @mock.patch("claudex.providers.codex.subprocess.run")
    def test_retries_on_spawn_refresh_then_succeeds(self, run):
        run.side_effect = [
            _result(0, _JSONL_SPAWN, _STDERR_SPAWN),  # transient, empty content
            _result(0, _JSONL_OK, ""),                # recovers
        ]
        resp = CodexProvider().send("do a thing")
        self.assertTrue(resp.success)
        self.assertEqual(resp.content, "Hello world")
        self.assertEqual(run.call_count, 2)

    @mock.patch("claudex.providers.codex.subprocess.run")
    def test_no_retry_when_content_present(self, run):
        # spawn-refresh noise present but we still got a message → don't retry.
        run.return_value = _result(0, _JSONL_OK, _STDERR_SPAWN)
        resp = CodexProvider().send("do a thing")
        self.assertTrue(resp.success)
        self.assertEqual(run.call_count, 1)

    @mock.patch("claudex.providers.codex.subprocess.run")
    def test_retry_reads_from_output_file_on_second_attempt(self, run):
        # Attempt 1: transient, no output file written → retry.
        # Attempt 2: codex writes the final message to its fresh -o file (JSONL
        # carries usage but NO agent_message) → content must come from the FILE.
        calls = {"n": 0}

        def fake_run(cmd, **kwargs):
            calls["n"] += 1
            out_path = cmd[cmd.index("-o") + 1]
            if calls["n"] == 1:
                return _result(0, _JSONL_SPAWN, _STDERR_SPAWN)
            Path(out_path).write_text("FINAL FROM FILE", encoding="utf-8")
            return _result(0, _JSONL_USAGE_ONLY, "")

        run.side_effect = fake_run
        resp = CodexProvider().send("do a thing")
        self.assertTrue(resp.success)
        self.assertEqual(resp.content, "FINAL FROM FILE")
        self.assertEqual(resp.output_tokens, 2)       # usage came from attempt 2's JSONL
        self.assertEqual(run.call_count, 2)

    @mock.patch("claudex.providers.codex.subprocess.run")
    def test_fails_after_max_attempts(self, run):
        run.return_value = _result(0, _JSONL_SPAWN, _STDERR_SPAWN)  # always empty
        resp = CodexProvider().send("do a thing")
        self.assertFalse(resp.success)
        self.assertEqual(run.call_count, _MAX_ATTEMPTS)


if __name__ == "__main__":
    unittest.main()
