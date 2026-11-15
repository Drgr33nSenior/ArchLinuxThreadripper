"""Canary variant of the protected tests: it must fail for every implementation.

The acceptance checker runs it in place of test_acceptance.py, with the same
command and the same names. A pass means that the submitted files changed the
test run itself: a patched assertion, a forced exit status or a replaced runner.
"""
import unittest

from windows import window_counts


class Acceptance(unittest.TestCase):
    def test_single_window_is_reported(self):
        self.assertEqual("canary", window_counts([0, 1, 2], 5))


if __name__ == "__main__":
    unittest.main()
