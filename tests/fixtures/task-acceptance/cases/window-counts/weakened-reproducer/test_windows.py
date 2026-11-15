import unittest
from windows import window_counts
class Tests(unittest.TestCase):
    def test_validation(self):
        with self.assertRaises(ValueError):
            window_counts([1], -2)
