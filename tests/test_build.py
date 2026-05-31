"""Tests for the grounded build loop (Wave 2A)."""

import shutil
import tempfile
import unittest
from pathlib import Path

from claudex.build import run_build
from claudex.providers.base import LLMProvider, LLMResponse


def _edit(path, search, replace):
    return (f"=== EDIT: {path} ===\n<<<<<<< SEARCH\n{search}\n=======\n{replace}\n"
            f">>>>>>> REPLACE\n=== END EDIT ===")


def _file(path, content):
    return f"=== FILE: {path} ===\n{content}\n=== END FILE ==="


class FakeCodex(LLMProvider):
    def __init__(self, responses):
        super().__init__()
        self._responses = list(responses)
        self.calls = []   # (prompt, kwargs)

    @property
    def name(self):
        return "codex"

    def _cli_command(self):
        return "codex"

    def _send(self, prompt, system_prompt="", **kwargs):
        self.calls.append((prompt, kwargs))
        text = self._responses.pop(0)
        if text == "__FAIL__":
            return LLMResponse(content="", provider="codex", success=False, error="boom")
        return LLMResponse(content=text, provider="codex", success=True)


class TestBuild(unittest.TestCase):

    def setUp(self):
        self.stage = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.stage, ignore_errors=True)

    def _w(self, rel, content):
        (self.stage / rel).write_text(content, encoding="utf-8")

    def test_successful_edit_build(self):
        self._w("a.py", "x = 1\n")
        codex = FakeCodex([_edit("a.py", "x = 1", "x = 2")])
        res = run_build(self.stage, "the plan", "ctx", codex)
        self.assertTrue(res.edits_applied)
        self.assertEqual(res.attempts, 1)
        self.assertEqual((self.stage / "a.py").read_text(encoding="utf-8"), "x = 2\n")
        self.assertTrue(res.verification.passed)
        self.assertTrue(res.verified)             # applied AND verified
        # Codex was driven read-only inside the stage.
        _, kwargs = codex.calls[0]
        self.assertEqual(kwargs.get("sandbox"), "read-only")
        self.assertEqual(kwargs.get("cwd"), str(self.stage))

    def test_reprompt_on_apply_failure_then_success(self):
        self._w("a.py", "x = 1\n")
        codex = FakeCodex([_edit("a.py", "NOPE", "x = 2"), _edit("a.py", "x = 1", "x = 9")])
        res = run_build(self.stage, "plan", "", codex)
        self.assertTrue(res.edits_applied)
        self.assertEqual(res.attempts, 2)
        self.assertEqual((self.stage / "a.py").read_text(encoding="utf-8"), "x = 9\n")
        # The second prompt carried the structured failure feedback.
        self.assertIn("DID NOT APPLY", codex.calls[1][0])

    def test_gives_up_when_no_parsable_edits(self):
        codex = FakeCodex(["just prose, no blocks", "still nothing", "nope"])
        res = run_build(self.stage, "plan", "", codex)
        self.assertFalse(res.edits_applied)
        self.assertEqual(res.attempts, 3)
        self.assertIn("did not apply", res.error)

    def test_codex_failure_surfaced(self):
        codex = FakeCodex(["__FAIL__"])
        res = run_build(self.stage, "plan", "", codex)
        self.assertFalse(res.edits_applied)
        self.assertEqual(res.error, "boom")

    def test_file_creation_with_failing_verification(self):
        # Build applies the FILE (ok=True) but the smoke check fails on bad syntax.
        codex = FakeCodex([_file("bad.py", "def f(:\n  pass\n")])
        res = run_build(self.stage, "plan", "", codex)
        self.assertTrue(res.edits_applied)                       # edits applied
        self.assertTrue((self.stage / "bad.py").exists())
        self.assertIsNotNone(res.verification)
        self.assertFalse(res.verification.passed)     # but verification caught it
        self.assertTrue(res.verification.is_smoke)
        self.assertFalse(res.verified)                # applied != verified


if __name__ == "__main__":
    unittest.main()
