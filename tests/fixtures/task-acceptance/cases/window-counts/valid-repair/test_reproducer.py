"""Authored stand-in for a reproducer test that an agent adds with its repair."""
import unittest

from windows import window_counts


class Reproducer(unittest.TestCase):
    def test_final_window_is_reported(self):
        self.assertEqual([(0, 1), (20, 2)], window_counts([5, 20, 21], 10))


if __name__ == "__main__":
    unittest.main()
