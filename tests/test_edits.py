"""Tests for the search/replace + full-file edit engine (Wave 2A)."""

import shutil
import tempfile
import unittest
from pathlib import Path

from claudex.edits import parse_edits, apply_edits, EditOp


def _edit_block(path, search, replace):
    return (
        f"=== EDIT: {path} ===\n"
        f"<<<<<<< SEARCH\n{search}\n=======\n{replace}\n>>>>>>> REPLACE\n"
        f"=== END EDIT ==="
    )


def _file_block(path, content):
    return f"=== FILE: {path} ===\n{content}\n=== END FILE ==="


class TestParse(unittest.TestCase):

    def test_parse_edit(self):
        ops = parse_edits(_edit_block("a.py", "old line", "new line"))
        self.assertEqual(len(ops), 1)
        self.assertEqual(ops[0].kind, "edit")
        self.assertEqual(ops[0].path, "a.py")
        self.assertEqual(ops[0].search, "old line")
        self.assertEqual(ops[0].replace, "new line")

    def test_parse_file(self):
        ops = parse_edits(_file_block("new.py", "print('hi')"))
        self.assertEqual(len(ops), 1)
        self.assertEqual(ops[0].kind, "file")
        self.assertEqual(ops[0].path, "new.py")
        self.assertEqual(ops[0].content, "print('hi')")

    def test_parse_multiple_in_order(self):
        text = "intro\n" + _file_block("a.py", "x = 1") + "\nmid\n" + _edit_block("b.py", "foo", "bar")
        ops = parse_edits(text)
        self.assertEqual([o.kind for o in ops], ["file", "edit"])
        self.assertEqual([o.path for o in ops], ["a.py", "b.py"])

    def test_parse_multiline_search(self):
        ops = parse_edits(_edit_block("a.py", "line1\nline2", "lineA\nlineB"))
        self.assertEqual(ops[0].search, "line1\nline2")
        self.assertEqual(ops[0].replace, "lineA\nlineB")

    def test_edit_without_end_marker_not_parsed(self):
        # Truncated block (no === END EDIT ===) must NOT parse.
        truncated = (
            "=== EDIT: a.py ===\n"
            "<<<<<<< SEARCH\nold\n=======\nnew\n>>>>>>> REPLACE\n"
            "(stream cut off here)"
        )
        self.assertEqual(parse_edits(truncated), [])

    def test_file_without_end_marker_not_parsed(self):
        truncated = "=== FILE: a.py ===\nsome content\n(cut off)"
        self.assertEqual(parse_edits(truncated), [])

    def test_markdown_fenced_block_still_parses(self):
        # Codex often wraps output in ``` — the markers must still be found.
        inner = _edit_block("a.py", "old", "new")
        self.assertEqual(len(parse_edits("```\n" + inner + "\n```")), 1)
        self.assertEqual(len(parse_edits("```python\n" + inner + "\n```")), 1)

    def test_extra_whitespace_in_markers_tolerated(self):
        text = "===  EDIT:  a.py  ===\n<<<<<<<  SEARCH\nold\n=======\nnew\n>>>>>>>  REPLACE\n===  END EDIT  ==="
        ops = parse_edits(text)
        self.assertEqual(len(ops), 1)
        self.assertEqual(ops[0].path, "a.py")


class TestApply(unittest.TestCase):

    def setUp(self):
        self.base = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)

    def _write(self, rel, content, newline="\n"):
        p = self.base / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content.replace("\n", newline).encode("utf-8"))
        return p

    def test_edit_unique_match_applies(self):
        self._write("a.py", "x = 1\ny = 2\n")
        res = apply_edits(parse_edits(_edit_block("a.py", "x = 1", "x = 42")), self.base)
        self.assertTrue(res.ok)
        self.assertEqual((self.base / "a.py").read_text(encoding="utf-8"), "x = 42\ny = 2\n")

    def test_edit_zero_match_fails_and_writes_nothing(self):
        self._write("a.py", "x = 1\n")
        res = apply_edits(parse_edits(_edit_block("a.py", "NOPE", "x = 42")), self.base)
        self.assertFalse(res.ok)
        self.assertEqual(res.failures[0].match_count, 0)
        self.assertEqual((self.base / "a.py").read_text(encoding="utf-8"), "x = 1\n")

    def test_edit_multi_match_fails(self):
        self._write("a.py", "dup\ndup\n")
        res = apply_edits(parse_edits(_edit_block("a.py", "dup", "x")), self.base)
        self.assertFalse(res.ok)
        self.assertEqual(res.failures[0].match_count, 2)

    def test_file_creates_new(self):
        res = apply_edits(parse_edits(_file_block("pkg/new.py", "print('hi')")), self.base)
        self.assertTrue(res.ok)
        self.assertEqual((self.base / "pkg" / "new.py").read_text(encoding="utf-8"), "print('hi')")

    def test_file_no_trailing_newline(self):
        # Content "abc" with no blank line before the marker → file has no final newline.
        text = "=== FILE: a.py ===\nabc\n=== END FILE ==="
        res = apply_edits(parse_edits(text), self.base)
        self.assertTrue(res.ok)
        self.assertEqual((self.base / "a.py").read_bytes(), b"abc")

    def test_file_trailing_newline_via_blank_line(self):
        # A blank line before the marker → file DOES end with a trailing newline.
        text = "=== FILE: a.py ===\nabc\n\n=== END FILE ==="
        res = apply_edits(parse_edits(text), self.base)
        self.assertTrue(res.ok)
        self.assertEqual((self.base / "a.py").read_bytes(), b"abc\n")

    def test_transactional_one_failure_writes_none(self):
        self._write("a.py", "x = 1\n")
        text = _edit_block("a.py", "x = 1", "x = 2") + "\n" + _edit_block("a.py", "NOPE", "y")
        res = apply_edits(parse_edits(text), self.base)
        self.assertFalse(res.ok)
        # First (valid) edit must NOT have been written.
        self.assertEqual((self.base / "a.py").read_text(encoding="utf-8"), "x = 1\n")

    def test_apply_across_two_files(self):
        self._write("a.py", "A1\n")
        self._write("b.py", "B1\n")
        text = _edit_block("a.py", "A1", "A2") + "\n" + _edit_block("b.py", "B1", "B2")
        res = apply_edits(parse_edits(text), self.base)
        self.assertTrue(res.ok)
        self.assertEqual((self.base / "a.py").read_text(encoding="utf-8"), "A2\n")
        self.assertEqual((self.base / "b.py").read_text(encoding="utf-8"), "B2\n")

    def test_sequential_edits_same_file(self):
        self._write("a.py", "a\nb\nc\n")
        text = _edit_block("a.py", "a", "A") + "\n" + _edit_block("a.py", "c", "C")
        res = apply_edits(parse_edits(text), self.base)
        self.assertTrue(res.ok)
        self.assertEqual((self.base / "a.py").read_text(encoding="utf-8"), "A\nb\nC\n")

    def test_crlf_preserved(self):
        self._write("a.py", "x = 1\ny = 2\n", newline="\r\n")
        res = apply_edits(parse_edits(_edit_block("a.py", "x = 1", "x = 9")), self.base)
        self.assertTrue(res.ok)
        raw = (self.base / "a.py").read_bytes()
        # CRLF preserved throughout, no bare LF introduced.
        self.assertEqual(raw, b"x = 9\r\ny = 2\r\n")

    def test_no_final_newline_preserved(self):
        self._write("a.py", "only_line")  # no trailing newline
        res = apply_edits(parse_edits(_edit_block("a.py", "only_line", "changed")), self.base)
        self.assertTrue(res.ok)
        self.assertEqual((self.base / "a.py").read_bytes(), b"changed")

    def test_edit_missing_file_fails(self):
        res = apply_edits(parse_edits(_edit_block("ghost.py", "x", "y")), self.base)
        self.assertFalse(res.ok)
        self.assertIn("not found", res.failures[0].reason)

    def test_unsafe_path_fails(self):
        res = apply_edits([EditOp(kind="file", path=r"..\..\escape.txt", content="x")], self.base)
        self.assertFalse(res.ok)
        self.assertFalse((self.base.parent / "escape.txt").exists())

    def test_empty_search_fails(self):
        res = apply_edits([EditOp(kind="edit", path="a.py", search="  ", replace="z")], self.base)
        self.assertFalse(res.ok)


if __name__ == "__main__":
    unittest.main()
