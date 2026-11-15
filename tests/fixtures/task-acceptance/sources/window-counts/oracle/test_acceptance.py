"""Protected acceptance tests. The agent workspace never contains this file."""
import unittest

from windows import window_counts


class Acceptance(unittest.TestCase):
    def test_single_window_is_reported(self):
        self.assertEqual([(0, 3)], window_counts([0, 1, 2], 5))

    def test_every_window_including_the_last(self):
        self.assertEqual([(0, 1), (10, 2), (30, 1)], window_counts([3, 10, 19, 30], 10))

    def test_boundary_timestamp_starts_the_next_window(self):
        self.assertEqual([(0, 1), (7, 1), (14, 2)], window_counts([6, 7, 14, 20], 7))

    def test_width_one(self):
        self.assertEqual([(7, 2), (8, 1)], window_counts([7, 7, 8], 1))

    def test_negative_width_is_rejected(self):
        with self.assertRaises(ValueError):
            window_counts([1], -2)


if __name__ == "__main__":
    unittest.main()
