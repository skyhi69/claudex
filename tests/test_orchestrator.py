"""End-to-end integration test of the Wave 2A pipeline with fake providers.

Exercises the REAL orchestrator + worktree + build + runner + audit wiring over
real git and real verification (py_compile) — only the two LLM CLIs are faked,
so no quota is spent and the run is deterministic.
"""

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import claudex
import claudex.orchestrator as orch_mod
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
    def __init__(self, complexity="complex"):
        super().__init__()
        self._complexity = complexity   # "complex" -> debate path; "simple" -> fast plan

    @property
    def name(self):
        return "claude"

    def _cli_command(self):
        return "claude"

    def is_available(self):
        return True

    def _send(self, prompt, system_prompt="", **kw):
        if "Analyze this coding task" in prompt:
            return _r(f"Assessment of the greeter task.\nCOMPLEXITY: {self._complexity}", "claude")
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

    def test_simple_task_uses_fast_plan_no_debate(self):
        # B3: a simple task skips the debate entirely (no planning rounds) but
        # still builds, verifies, and audits to completion.
        orch = Orchestrator(ClaudexConfig(), ROLES, on_message=lambda *a: None)
        orch.claude = FakeClaude(complexity="simple")
        orch.codex = FakeCodex()
        state = orch.run("Create a greeter with greet(name)", self.target)
        self.assertEqual(state.current_node, NodeType.DONE)
        self.assertEqual(state.plan.rounds, [])              # fast path: no debate
        self.assertTrue(state.verification_passed)
        self.assertTrue(state.audit_results[-1].approved)

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

    def test_resolve_loop_fixes_a_failing_build(self):
        # First build fails the smoke gate -> audit rejects -> resolve re-prompts ->
        # the fix passes -> approved. Exercises the full fix loop end to end.
        class FlakeyCodex(FakeCodex):
            def __init__(self):
                super().__init__()
                self.build_calls = 0

            def _send(self, prompt, system_prompt="", **kw):
                if "emit ONLY these blocks" in prompt:
                    self.build_calls += 1
                    if self.build_calls == 1:
                        return _r("=== FILE: mod.py ===\ndef f(:\n  pass\n=== END FILE ===", "codex")
                    return _r("=== FILE: mod.py ===\ndef f():\n    return 1\n=== END FILE ===", "codex")
                return _r("Agreed.\n" + _CONSENSUS, "codex")

        orch = Orchestrator(ClaudexConfig(resolve_max_iterations=2), ROLES, on_message=lambda *a: None)
        orch.claude = FakeClaude()
        orch.codex = FlakeyCodex()
        state = orch.run("create mod", self.target)
        self.assertEqual(state.current_node, NodeType.DONE)
        self.assertTrue(state.verification_passed)            # fixed during resolve
        self.assertTrue(state.audit_results[-1].approved)
        self.assertGreaterEqual(state.resolve_iteration, 1)   # resolve actually ran
        orch.cleanup(state)

    def test_crash_mid_run_cleans_up_worktree(self):
        # If the pipeline throws after the worktree exists, it must NOT leak.
        orch = Orchestrator(ClaudexConfig(), ROLES, on_message=lambda *a: None)
        orch.claude = FakeClaude()
        orch.codex = FakeCodex()
        with mock.patch.object(orch_mod, "run_build", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                orch.run("crash please", self.target)
        # No leaked claudex worktree registered in the repo.
        listing = worktree._git(["worktree", "list"], cwd=self.target).stdout
        self.assertNotIn("claudex_wt_", listing)

    def test_existing_repo_edit_flow(self):
        # Existing repo (not greenfield) + an EDIT to an existing file — the path
        # the Wave 2B benchmark exercised. Greenfield e2e tests miss this.
        worktree.ensure_repo(self.target)
        (self.target / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        worktree._git(["add", "-A"], cwd=self.target)
        worktree._git(worktree._IDENT + ["commit", "-m", "seed calc"], cwd=self.target)

        class EditCodex(FakeCodex):
            def _send(self, prompt, system_prompt="", **kw):
                if "emit ONLY these blocks" in prompt:
                    return _r(
                        "Add a subtract function.\n"
                        "=== EDIT: calc.py ===\n"
                        "<<<<<<< SEARCH\n"
                        "def add(a, b):\n    return a + b\n"
                        "=======\n"
                        "def add(a, b):\n    return a + b\n\n\n"
                        "def subtract(a, b):\n    return a - b\n"
                        ">>>>>>> REPLACE\n"
                        "=== END EDIT ===", "codex")
                return _r("Agreed.\n" + _CONSENSUS, "codex")

        orch = Orchestrator(ClaudexConfig(), ROLES, on_message=lambda *a: None)
        orch.claude = FakeClaude()
        orch.codex = EditCodex()

        state = orch.run("Add subtract to calc", self.target)
        self.assertEqual(state.current_node, NodeType.DONE)
        self.assertIn("calc.py", state.name_status)
        self.assertTrue(state.verification_passed)
        self.assertTrue(state.audit_results[-1].approved)
        # Apply the tested edit into the (clean) existing repo.
        orch.apply_on_approval(state)
        self.assertIn("subtract", (self.target / "calc.py").read_text(encoding="utf-8"))
        orch.cleanup(state)


if __name__ == "__main__":
    unittest.main()
