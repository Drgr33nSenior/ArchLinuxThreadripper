import unittest

from windows import window_counts


class WindowCounts(unittest.TestCase):
    def test_last_window_is_kept(self):
        self.assertEqual([(0, 2), (10, 1)], window_counts([1, 9, 10], 10))

    def test_boundaries_are_half_open(self):
        self.assertEqual([(0, 1), (5, 2)], window_counts([4, 5, 9], 5))

    def test_empty_and_invalid(self):
        self.assertEqual([], window_counts([], 3))
        with self.assertRaises(ValueError):
            window_counts([1], 0)


if __name__ == "__main__":
    unittest.main()
