"""Tests for structured, fail-closed audit verdict parsing (Wave 1.2)."""

import unittest

from claudex.phases.audit import _parse_verdict


class TestParseVerdict(unittest.TestCase):

    def test_approved_json(self):
        text = '''Looks good.
```json
{"approved": true, "issues": [], "assessment": "Clean and complete."}
```'''
        approved, issues, assessment, parsed_ok = _parse_verdict(text)
        self.assertTrue(approved)
        self.assertTrue(parsed_ok)
        self.assertEqual(issues, [])
        self.assertEqual(assessment, "Clean and complete.")

    def test_rejected_json_with_issues(self):
        text = '''Problems found.
```json
{"approved": false, "issues": [
  {"severity": "high", "file": "a.py", "issue": "SQL injection", "fix": "parameterize"},
  {"severity": "low", "file": "b.py", "issue": "naming", "fix": "rename"}
], "assessment": "Has a serious bug."}
```'''
        approved, issues, assessment, parsed_ok = _parse_verdict(text)
        self.assertFalse(approved)
        self.assertTrue(parsed_ok)
        self.assertEqual(len(issues), 2)
        self.assertEqual(issues[0].severity, "high")
        self.assertEqual(issues[0].suggested_fix, "parameterize")

    def test_critical_issue_forces_rejection_even_if_approved_true(self):
        # Anti-sycophancy guard: model says approved=true but lists a critical issue.
        text = '''```json
{"approved": true, "issues": [{"severity": "critical", "file": "x.py", "issue": "RCE"}],
 "assessment": "ship it"}
```'''
        approved, issues, _, parsed_ok = _parse_verdict(text)
        self.assertFalse(approved)
        self.assertTrue(parsed_ok)
        self.assertEqual(issues[0].severity, "critical")

    def test_malformed_json_rejects_in_normal_mode(self):
        # Even with a legacy VERDICT: APPROVED line, malformed JSON must REJECT
        # in active audits — no backdoor around the structured contract.
        text = '''Review text.
```json
{"approved": true, "issues": [ broken json here
```
VERDICT: APPROVED'''
        approved, issues, _, parsed_ok = _parse_verdict(text)
        self.assertFalse(approved)
        self.assertFalse(parsed_ok)
        self.assertEqual(issues, [])

    def test_legacy_verdict_only_behind_compat_flag(self):
        # The VERDICT: line is honored only when explicitly re-parsing old transcripts.
        text = '''```json
{ broken
```
VERDICT: APPROVED'''
        self.assertFalse(_parse_verdict(text)[0])                              # normal: reject
        self.assertTrue(_parse_verdict(text, allow_legacy_verdict=True)[0])    # compat: approve

    def test_no_json_no_verdict_fails_closed(self):
        text = "The code looks fine to me overall, nice work."
        approved, issues, _, parsed_ok = _parse_verdict(text)
        self.assertFalse(approved)    # fail closed
        self.assertFalse(parsed_ok)

    def test_prose_approved_word_does_not_approve(self):
        # No keyword guessing: 'approved' in prose must NOT approve.
        text = "I would have approved this in the past, but say nothing structured now."
        approved, _, _, parsed_ok = _parse_verdict(text)
        self.assertFalse(approved)
        self.assertFalse(parsed_ok)

    def test_invalid_severity_normalized(self):
        text = '''```json
{"approved": false, "issues": [{"severity": "spicy", "issue": "weird"}], "assessment": "x"}
```'''
        _, issues, _, _ = _parse_verdict(text)
        self.assertEqual(issues[0].severity, "medium")

    def test_last_json_block_wins(self):
        text = '''```json
{"approved": false, "issues": [], "assessment": "first"}
```
On reflection:
```json
{"approved": true, "issues": [], "assessment": "second"}
```'''
        approved, _, assessment, _ = _parse_verdict(text)
        self.assertTrue(approved)
        self.assertEqual(assessment, "second")

    def test_empty_issue_text_skipped(self):
        text = '''```json
{"approved": false, "issues": [{"severity": "high", "issue": ""}, {"severity":"high","issue":"real"}],
 "assessment": "x"}
```'''
        _, issues, _, _ = _parse_verdict(text)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].issue, "real")


if __name__ == "__main__":
    unittest.main()
