"""Integration tests for the git worktree lifecycle (Wave 2A).

These run real git (verify-by-running) and skip if git is unavailable.
"""

import shutil
import tempfile
import unittest
from pathlib import Path

from claudex import worktree


@unittest.skipUnless(worktree.git_available(), "git not available")
class TestWorktree(unittest.TestCase):

    def setUp(self):
        self.target = Path(tempfile.mkdtemp(prefix="claudex_t_"))

    def tearDown(self):
        shutil.rmtree(self.target, ignore_errors=True)

    def test_ensure_repo_greenfield_then_idempotent(self):
        self.assertTrue(worktree.ensure_repo(self.target))    # initialized git
        self.assertTrue(worktree.is_git_repo(self.target))
        self.assertTrue(worktree._has_commit(self.target))
        self.assertFalse(worktree.ensure_repo(self.target))   # already a repo w/ commit

    def test_ensure_repo_disabled_raises_on_non_repo(self):
        with self.assertRaises(worktree.GitError):
            worktree.ensure_repo(self.target, auto_git_init=False)

    def test_create_worktree_and_diff(self):
        worktree.ensure_repo(self.target)
        stage = worktree.create_worktree(self.target, "sess1")
        self.assertTrue(stage.exists())
        (stage / "hello.py").write_text("print('hi')\n", encoding="utf-8")
        diff = worktree.stage_diff(stage)
        self.assertIn("hello.py", diff)
        self.assertIn("hello.py", worktree.stage_name_status(stage))
        worktree.remove_worktree(self.target, stage)
        self.assertFalse(stage.exists())

    def test_apply_patch_lands_in_target_working_tree_only(self):
        worktree.ensure_repo(self.target)
        stage = worktree.create_worktree(self.target, "sess2")
        (stage / "added.txt").write_text("content here\n", encoding="utf-8")
        patch = worktree.stage_diff(stage)
        worktree.remove_worktree(self.target, stage)
        # Target is untouched until we apply the tested patch.
        self.assertFalse((self.target / "added.txt").exists())
        self.assertTrue(worktree.apply_patch(self.target, patch))
        self.assertEqual((self.target / "added.txt").read_text(encoding="utf-8"), "content here\n")
        # Applied to the WORKING TREE only — left untracked, not staged.
        status = worktree._git(["status", "--porcelain"], cwd=self.target).stdout
        self.assertIn("?? added.txt", status)

    def test_apply_patch_refuses_dirty_target(self):
        worktree.ensure_repo(self.target)
        (self.target / "unrelated.txt").write_text("user's own change\n", encoding="utf-8")
        with self.assertRaises(worktree.GitError):
            worktree.apply_patch(self.target, "any non-empty patch", require_clean=True)

    def test_commit_stage_makes_real_backup_branch(self):
        worktree.ensure_repo(self.target)
        stage = worktree.create_worktree(self.target, "sess3")
        (stage / "proposed.py").write_text("x = 1\n", encoding="utf-8")
        self.assertTrue(worktree.commit_stage(stage))
        worktree.remove_worktree(self.target, stage)
        branch = "claudex/" + worktree._sanitize_ref("sess3")
        tree = worktree._git(["ls-tree", "-r", "--name-only", branch], cwd=self.target).stdout
        self.assertIn("proposed.py", tree)   # the branch actually preserves the proposal

    def test_binary_round_trip(self):
        worktree.ensure_repo(self.target)
        stage = worktree.create_worktree(self.target, "sess4")
        blob = bytes(range(256)) + b"\x00\xff\x00\xff"
        (stage / "data.bin").write_bytes(blob)
        patch = worktree.stage_diff(stage)
        worktree.remove_worktree(self.target, stage)
        self.assertTrue(worktree.apply_patch(self.target, patch))
        self.assertEqual((self.target / "data.bin").read_bytes(), blob)

    def test_unusual_session_id_sanitized(self):
        worktree.ensure_repo(self.target)
        stage = worktree.create_worktree(self.target, "feat/x y:z")
        self.assertTrue(stage.exists())
        worktree.remove_worktree(self.target, stage)
        # Branch was created under a sanitized name (no error, ref exists).
        branch = "claudex/" + worktree._sanitize_ref("feat/x y:z")
        self.assertEqual(worktree._git(["rev-parse", "--verify", branch], cwd=self.target,
                                       check=False).returncode, 0)

    def test_stage_diff_excludes_build_artifacts(self):
        worktree.ensure_repo(self.target)
        stage = worktree.create_worktree(self.target, "sess_junk")
        (stage / "real.py").write_text("x = 1\n", encoding="utf-8")
        (stage / "__pycache__").mkdir()
        (stage / "__pycache__" / "real.cpython-313.pyc").write_bytes(b"\x00junk")
        (stage / ".serena").mkdir()
        (stage / ".serena" / "cache.txt").write_text("junk", encoding="utf-8")
        diff = worktree.stage_diff(stage)
        names = worktree.stage_name_status(stage)
        worktree.remove_worktree(self.target, stage)
        self.assertIn("real.py", diff)
        self.assertNotIn("__pycache__", diff)
        self.assertNotIn(".serena", diff)
        self.assertNotIn("__pycache__", names)

    def test_stage_diff_no_error_when_artifact_is_gitignored(self):
        # Regression (found by the Wave 2B benchmark): on an EXISTING repo whose
        # .gitignore lists .serena/, a .serena dir in the worktree must not make
        # stage_diff crash (the old explicit-pathspec excludes errored on it).
        worktree.ensure_repo(self.target)
        (self.target / ".gitignore").write_text(".serena/\n__pycache__/\n", encoding="utf-8")
        worktree._git(["add", ".gitignore"], cwd=self.target)
        worktree._git(worktree._IDENT + ["commit", "-m", "gitignore"], cwd=self.target)
        stage = worktree.create_worktree(self.target, "sess_ignored")
        (stage / "real.py").write_text("x = 1\n", encoding="utf-8")
        (stage / ".serena").mkdir()
        (stage / ".serena" / "x.txt").write_text("junk", encoding="utf-8")
        diff = worktree.stage_diff(stage)        # must NOT raise
        worktree.remove_worktree(self.target, stage)
        self.assertIn("real.py", diff)
        self.assertNotIn(".serena", diff)

    def test_apply_empty_patch_is_noop_success(self):
        worktree.ensure_repo(self.target)
        self.assertTrue(worktree.apply_patch(self.target, "   "))


if __name__ == "__main__":
    unittest.main()
