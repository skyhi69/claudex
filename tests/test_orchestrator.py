"""End-to-end integration test of the Wave 2A pipeline with fake providers.

Exercises the REAL orchestrator + worktree + build + runner + audit wiring over
real git and real verification (py_compile) — only the two LLM CLIs are faked,
so no quota is spent and the run is deterministic.
"""

import shutil
import tempfile
import unittest
from pathlib import Path

import claudex
from claudex import worktree
from claudex.config import ClaudexConfig
from claudex.orchestrator import Orchestrator
from claudex.models import NodeType
from claudex.providers.base import LLMProvider, LLMResponse

ROLES = Path(claudex.__file__).resolve().parent.parent / "roles"

_CONSENSUS = ('```json\n{"consensus_block": true, "agreed": true, "concerns": [], '
             '"position": "agree", "final_plan": "Create hello.py with greet(name) '
             'returning a greeting string."}\n```')


def _r(text, provider):
    return LLMResponse(content=text, provider=provider, success=True)


class FakeClaude(LLMProvider):
    @property
    def name(self):
        return "claude"

    def _cli_command(self):
        return "claude"

    def is_available(self):
        return True

    def _send(self, prompt, system_prompt="", **kw):
        if "Analyze this coding task" in prompt:
            return _r("This is a simple task: build a greeter module.", "claude")
        if "BLIND DIFF REVIEW" in prompt:
            return _r('Looks correct and complete.\n```json\n'
                      '{"approved": true, "issues": [], "assessment": "Correct."}\n```', "claude")
        return _r("Proposal: create hello.py.\n" + _CONSENSUS, "claude")


class FakeCodex(LLMProvider):
    @property
    def name(self):
        return "codex"

    def _cli_command(self):
        return "codex"

    def is_available(self):
        return True

    def _send(self, prompt, system_prompt="", **kw):
        if "emit ONLY these blocks" in prompt:
            return _r('Creating the greeter.\n'
                      '=== FILE: hello.py ===\n'
                      'def greet(name):\n    return f"Hello, {name}!"\n'
                      '=== END FILE ===', "codex")
        return _r("Agreed, that works.\n" + _CONSENSUS, "codex")


@unittest.skipUnless(worktree.git_available() and ROLES.is_dir(), "git or roles/ missing")
class TestPipelineEndToEnd(unittest.TestCase):

    def setUp(self):
        self.target = Path(tempfile.mkdtemp(prefix="claudex_e2e_"))

    def tearDown(self):
        shutil.rmtree(self.target, ignore_errors=True)

    def test_full_pipeline_builds_verifies_audits_and_applies(self):
        orch = Orchestrator(ClaudexConfig(), ROLES, on_message=lambda *a: None)
        orch.claude = FakeClaude()
        orch.codex = FakeCodex()

        state = orch.run("Create a greeter with greet(name)", self.target)

        # Pipeline reached DONE via a real build + verification + approving audit.
        self.assertEqual(state.current_node, NodeType.DONE)
        self.assertIn("hello.py", state.diff)
        self.assertIn("hello.py", state.name_status)
        self.assertTrue(state.verification_passed)           # claudex ran py_compile
        self.assertTrue(state.audit_results[-1].approved)
        # The change is NOT in the real project until applied.
        self.assertFalse((self.target / "hello.py").exists())

        # Apply-on-approval lands the tested diff in the real working tree.
        orch.apply_on_approval(state)
        self.assertTrue((self.target / "hello.py").exists())
        self.assertIn("def greet", (self.target / "hello.py").read_text(encoding="utf-8"))

        orch.cleanup(state)
        self.assertIsNone(state.stage_dir)

    def test_brief_uses_changed_files_when_no_explanation(self):
        from claudex.models import SessionState
        orch = Orchestrator(ClaudexConfig(), ROLES, on_message=lambda *a: None)
        state = SessionState(task="t", target_dir=self.target)
        state.name_status = "A\thello.py\nM\tutil.py"
        state.build_explanation = ""
        brief = orch._build_brief(state)
        self.assertIn("hello.py", brief.what_was_built)
        self.assertIn("util.py", brief.what_was_built)
        # Must NOT be the analysis essay (there is none here, but assert it's the file list).
        self.assertTrue(brief.what_was_built.startswith("Changed:"))

    def test_brief_prefers_codex_explanation(self):
        from claudex.models import SessionState
        orch = Orchestrator(ClaudexConfig(), ROLES, on_message=lambda *a: None)
        state = SessionState(task="t", target_dir=self.target)
        state.name_status = "A\thello.py"
        state.build_explanation = "Added a greeter function."
        brief = orch._build_brief(state)
        self.assertEqual(brief.what_was_built, "Added a greeter function.")

    def test_failed_verification_blocks_approval(self):
        # Codex emits code with a syntax error → smoke fails → audit must REJECT
        # even though Claude's verdict says approved.
        class BadCodex(FakeCodex):
            def _send(self, prompt, system_prompt="", **kw):
                if "emit ONLY these blocks" in prompt:
                    return _r("=== FILE: hello.py ===\ndef greet(:\n  pass\n=== END FILE ===", "codex")
                return _r("Agreed.\n" + _CONSENSUS, "codex")

        orch = Orchestrator(ClaudexConfig(resolve_max_iterations=1), ROLES, on_message=lambda *a: None)
        orch.claude = FakeClaude()
        orch.codex = BadCodex()

        state = orch.run("Create a greeter", self.target)
        self.assertEqual(state.current_node, NodeType.DONE)   # ends, but...
        self.assertFalse(state.verification_passed)           # smoke failed
        self.assertFalse(state.audit_results[-1].approved)    # gate blocked approval
        orch.cleanup(state)


if __name__ == "__main__":
    unittest.main()
