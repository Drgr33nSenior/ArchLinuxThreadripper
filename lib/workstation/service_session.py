"""Private host-to-gateway admission control; caller holds the session lock."""
import argparse
import ipaddress
import json
import os
from pathlib import Path
import stat
import sys
import time
import urllib.request


def control(address, credential, phase, model=None, images=False, cancel=False):
    if not ipaddress.ip_address(address).is_private:
        raise ValueError("private ClusterIP required")
    path = Path(credential)
    meta = path.stat()
    if path.is_symlink() or meta.st_uid != 0 or stat.S_IMODE(meta.st_mode) != 0o600:
        raise ValueError("root-owned mode 0600 control credential required")
    key = path.read_text().strip()
    if len(key) < 32:
        raise ValueError("control credential unavailable")
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None
    request = urllib.request.Request(f"http://{address}:4000/_service/control",
        data=json.dumps({"phase": phase, "model": model, "images": images, "cancel": cancel}).encode(),
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    with urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect()).open(request, timeout=5) as response:
        return json.loads(response.read(8192))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("address")
    parser.add_argument("credential")
    parser.add_argument("phase", choices=("ready", "starting", "draining", "maintenance", "unavailable"))
    parser.add_argument("model", choices=("Qwen3.8-27B-FP8", "Qwen3.5-9B", "none"))
    parser.add_argument("--images", action="store_true")
    args = parser.parse_args()
    try:
        if os.geteuid() != 0:
            raise ValueError("root session owner required")
        result = control(args.address, args.credential, args.phase,
                         None if args.model == "none" else args.model, args.images)
        if args.phase == "draining":
            deadline = time.monotonic() + 180
            while result["active"] and time.monotonic() < deadline:
                time.sleep(1)
                result = control(args.address, args.credential, "draining")
            if result["active"]:
                control(args.address, args.credential, "draining", cancel=True)
                time.sleep(2)
                if control(args.address, args.credential, "draining")["active"]:
                    raise ValueError("gateway requests did not cancel")
    except (ValueError, OSError, KeyError):
        print("Gateway admission update failed; session must remain closed.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
