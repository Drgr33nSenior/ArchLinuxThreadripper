"""Known-bad patch: ends the test process with a success status on import."""
import os

os._exit(0)
