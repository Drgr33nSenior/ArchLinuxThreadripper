"""Authenticated native readiness probe; never expose the key in argv/output."""
from pathlib import Path
import sys
import urllib.request


if __name__ == "__main__":
    try:
        key = Path("/run/gateway/master-key").read_text().strip()
        request = urllib.request.Request("http://127.0.0.1:4000/health/readiness",
                                         headers={"Authorization": "Bearer " + key})
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=2) as response:
            sys.exit(0 if response.status == 200 else 1)
    except Exception:
        sys.exit(1)
