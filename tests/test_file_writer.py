"""Tests for path confinement and transactional writes (Wave 1.1)."""

import shutil
import tempfile
import unittest
from pathlib import Path

from claudex.file_writer import safe_resolve, write_files, UnsafePathError
from claudex.models import FileOutput


class TestSafeResolve(unittest.TestCase):
    """safe_resolve must reject anything that escapes the target dir."""

    def setUp(self):
        self.base = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)

    def test_normal_relative_ok(self):
        resolved = safe_resolve(self.base, "src/app.py")
        self.assertTrue(resolved.is_relative_to(self.base.resolve()))

    def test_reject_absolute_posix(self):
        with self.assertRaises(UnsafePathError):
            safe_resolve(self.base, "/etc/passwd")

    def test_reject_absolute_windows_drive(self):
        with self.assertRaises(UnsafePathError):
            safe_resolve(self.base, r"C:\Windows\System32\evil.dll")

    def test_reject_unc(self):
        with self.assertRaises(UnsafePathError):
            safe_resolve(self.base, r"\\attacker\share\x")

    def test_reject_leading_slash(self):
        with self.assertRaises(UnsafePathError):
            safe_resolve(self.base, "/already/rooted")

    def test_reject_parent_traversal(self):
        with self.assertRaises(UnsafePathError):
            safe_resolve(self.base, r"..\..\outside.txt")

    def test_reject_traversal_even_if_resolves_inside(self):
        # Conservative: any '..' part is rejected, even if it would net stay inside.
        with self.assertRaises(UnsafePathError):
            safe_resolve(self.base, "sub/../still_inside.txt")

    def test_reject_empty(self):
        with self.assertRaises(UnsafePathError):
            safe_resolve(self.base, "   ")

    def test_reject_alternate_data_stream(self):
        with self.assertRaises(UnsafePathError):
            safe_resolve(self.base, "notes.txt:hidden")

    def test_reject_reserved_nul(self):
        with self.assertRaises(UnsafePathError):
            safe_resolve(self.base, "NUL")

    def test_reject_reserved_with_extension(self):
        # CON.txt is still the reserved device CON on Windows.
        with self.assertRaises(UnsafePathError):
            safe_resolve(self.base, "CON.txt")

    def test_reject_reserved_in_subdir(self):
        with self.assertRaises(UnsafePathError):
            safe_resolve(self.base, "logs/COM1.log")

    def test_does_not_over_reject_similar_names(self):
        # Names that merely contain a reserved prefix are fine (COMMON != COM1).
        for ok in ("common.py", "com10.txt", "console.js", "auxiliary.md"):
            resolved = safe_resolve(self.base, ok)
            self.assertTrue(resolved.is_relative_to(self.base.resolve()))


class TestWriteFiles(unittest.TestCase):
    """write_files must be transactional and confined."""

    def setUp(self):
        self.base = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)

    def test_writes_normal_file(self):
        files = [FileOutput(path="a/b.txt", content="hello", action="create")]
        summaries = write_files(files, self.base, backup=False)
        written = self.base / "a" / "b.txt"
        self.assertTrue(written.exists())
        self.assertEqual(written.read_text(encoding="utf-8"), "hello")
        self.assertEqual(len(summaries), 1)

    def test_backup_on_overwrite(self):
        target = self.base / "x.txt"
        target.write_text("old", encoding="utf-8")
        files = [FileOutput(path="x.txt", content="new", action="modify")]
        write_files(files, self.base, backup=True)
        self.assertEqual(target.read_text(encoding="utf-8"), "new")
        self.assertTrue((self.base / "x.txt.claudex.bak").exists())

    def test_transactional_nothing_written_if_any_unsafe(self):
        files = [
            FileOutput(path="good.txt", content="data", action="create"),
            FileOutput(path=r"..\..\escape.txt", content="bad", action="create"),
        ]
        with self.assertRaises(UnsafePathError):
            write_files(files, self.base, backup=False)
        # The good file must NOT have been written — operation is all-or-nothing.
        self.assertFalse((self.base / "good.txt").exists())

    def test_unsafe_delete_rejected(self):
        files = [FileOutput(path=r"C:\Windows\notepad.exe", content="", action="delete")]
        with self.assertRaises(UnsafePathError):
            write_files(files, self.base, backup=False)


if __name__ == "__main__":
    unittest.main()
