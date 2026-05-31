"""Tests for complexity detection (A1 fix)."""

import unittest

from claudex.phases.analyze import _detect_complexity


class TestDetectComplexity(unittest.TestCase):

    def test_marker_wins(self):
        self.assertEqual(_detect_complexity("blah blah\nCOMPLEXITY: complex"), "complex")
        self.assertEqual(_detect_complexity("COMPLEXITY: simple"), "simple")
        self.assertEqual(_detect_complexity("notes\nCOMPLEXITY: moderate\nmore"), "moderate")

    def test_marker_last_wins_over_echo(self):
        text = "Use format COMPLEXITY: simple|moderate|complex.\n...\nCOMPLEXITY: moderate"
        self.assertEqual(_detect_complexity(text), "moderate")

    def test_prose_complexity_word_does_not_force_complex(self):
        # THE regression: the word "complexity" must not match "complex".
        text = "This task has low complexity and is straightforward to implement."
        self.assertEqual(_detect_complexity(text), "simple")

    def test_real_complex_word_detected(self):
        self.assertEqual(_detect_complexity("This is a complex distributed system."), "complex")

    def test_defaults_to_moderate(self):
        self.assertEqual(_detect_complexity("A task with no obvious difficulty signal."), "moderate")

    def test_marker_beats_misleading_prose(self):
        # Prose mentions "complex" but the explicit marker says simple → marker wins.
        text = "Avoid over-complex designs.\nCOMPLEXITY: simple"
        self.assertEqual(_detect_complexity(text), "simple")


if __name__ == "__main__":
    unittest.main()
