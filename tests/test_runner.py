"""Tests for verification detection + capture (Wave 2A)."""

import shutil
import tempfile
import unittest
from pathlib import Path

from claudex import runner


class TestDetect(unittest.TestCase):

    def setUp(self):
        self.stage = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.stage, ignore_errors=True)

    def _w(self, rel, content="x = 1\n"):
        p = self.stage / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return p

    def test_detect_pytest_by_tests_dir(self):
        (self.stage / "tests").mkdir()
        cmds, kind, smoke = runner.detect_verification(self.stage)
        self.assertEqual(kind, "pytest")
        self.assertFalse(smoke)

    def test_detect_pytest_by_test_file(self):
        self._w("test_app.py")
        self.assertEqual(runner.detect_verification(self.stage)[1], "pytest")

    def test_detect_npm(self):
        self._w("package.json", '{"scripts": {"test": "echo hi"}}')
        self.assertEqual(runner.detect_verification(self.stage)[1], "npm")

    def test_detect_smoke_for_plain_python(self):
        self._w("app.py")
        cmds, kind, smoke = runner.detect_verification(self.stage)
        self.assertEqual(kind, "smoke")
        self.assertTrue(smoke)
        self.assertEqual(cmds[0][:3], ["python", "-m", "py_compile"])

    def test_detect_smoke_for_js(self):
        self._w("app.js", "console.log(1)")
        cmds, kind, smoke = runner.detect_verification(self.stage)
        self.assertEqual(kind, "smoke")
        self.assertEqual(cmds[0][:2], ["node", "--check"])

    def test_configured_overrides_everything(self):
        (self.stage / "tests").mkdir()  # would otherwise be pytest
        cmds, kind, smoke = runner.detect_verification(self.stage, configured="make test")
        self.assertEqual(kind, "configured")
        self.assertFalse(smoke)
        self.assertEqual(cmds, [["make", "test"]])

    def test_pytest_config_alone_does_not_trigger_pytest(self):
        # pyproject mentions pytest but there are NO tests → must fall back to smoke,
        # not run pytest (which would exit 5 and falsely fail).
        self._w("pyproject.toml", "[tool.pytest.ini_options]\naddopts = '-q'\n")
        self._w("app.py")
        self.assertEqual(runner.detect_verification(self.stage)[1], "smoke")


class TestRun(unittest.TestCase):

    def setUp(self):
        self.stage = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.stage, ignore_errors=True)

    def _w(self, rel, content):
        p = self.stage / rel
        p.write_text(content, encoding="utf-8")
        return p

    def test_smoke_passes_on_valid_python(self):
        self._w("good.py", "x = 1\ndef f():\n    return x\n")
        res = runner.run_verification(self.stage)
        self.assertTrue(res.passed)
        self.assertTrue(res.is_smoke)
        self.assertEqual(res.label(), "smoke check passed")

    def test_smoke_fails_on_syntax_error(self):
        self._w("bad.py", "def f(:\n  pass\n")
        res = runner.run_verification(self.stage)
        self.assertFalse(res.passed)
        self.assertTrue(res.is_smoke)
        self.assertEqual(res.label(), "smoke check FAILED")

    def test_configured_command_exit_zero_passes(self):
        res = runner.run_verification(self.stage, configured='python -c "import sys; sys.exit(0)"')
        self.assertTrue(res.passed)
        self.assertFalse(res.is_smoke)

    def test_configured_command_exit_nonzero_fails(self):
        res = runner.run_verification(self.stage, configured='python -c "import sys; sys.exit(3)"')
        self.assertFalse(res.passed)
        self.assertEqual(res.exit_code, 3)

    def test_no_verifiable_files_is_none(self):
        res = runner.run_verification(self.stage)
        self.assertEqual(res.kind, "none")
        self.assertTrue(res.passed)
        self.assertEqual(res.label(), "no verification applicable")

    def test_missing_command_fails_gracefully(self):
        res = runner.run_verification(self.stage, configured="definitely_not_a_real_cmd_xyz --go")
        self.assertFalse(res.passed)
        self.assertIn("not found", res.output)

    def test_pytest_empty_suite_is_not_a_failure(self):
        # tests/ dir present but no tests → pytest exits 5 → treated as pass.
        (self.stage / "tests").mkdir()
        res = runner.run_verification(self.stage)
        self.assertEqual(res.kind, "pytest")
        self.assertTrue(res.passed)


if __name__ == "__main__":
    unittest.main()
