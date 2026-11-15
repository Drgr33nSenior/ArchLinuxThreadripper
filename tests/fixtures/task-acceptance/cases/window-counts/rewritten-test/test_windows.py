import unittest

from windows import window_counts


class WindowCounts(unittest.TestCase):
    def test_last_window_is_kept(self):
        self.assertIsInstance(window_counts([1, 9, 10], 10), list)

    def test_boundaries_are_half_open(self):
        self.assertIsInstance(window_counts([4, 5, 9], 5), list)

    def test_empty_and_invalid(self):
        self.assertEqual([], window_counts([], 3))


if __name__ == "__main__":
    unittest.main()
