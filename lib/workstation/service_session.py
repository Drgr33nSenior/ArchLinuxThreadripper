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


def media_active(address):
    """Pictures and videos service-tools is still producing (its /health; no credential)."""
    host, _, port = address.rpartition(":")
    if not ipaddress.ip_address(host).is_private or not port.isdigit():
        raise ValueError("private ClusterIP:port required for service-tools")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f"http://{host}:{port}/health", timeout=5) as response:
        health = json.loads(response.read(65536))
    return int(health.get("image_active", 0)) + int(bool(health.get("video_active")))


def wait_for_media(address, seconds):
    """Wait for running pictures/videos once draining refuses new ones.

    Pictures and videos do not pass the gateway's request count: service-tools runs them
    against the image backends directly, and refuses new ones while the gateway is not
    ready. Restarting a backend under a running job leaves its in-flight marker and blocks
    every later picture, so the switch waits (a video may take up to 20 minutes)."""
    deadline = time.monotonic() + seconds
    active = media_active(address)
    if active:
        print(f"Waiting for {active} picture/video job(s) to finish before the switch.", file=sys.stderr)
    while active and time.monotonic() < deadline:
        time.sleep(2)
        active = media_active(address)
    if active:
        raise ValueError("pictures or videos still running")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("address")
    parser.add_argument("credential")
    parser.add_argument("phase", choices=("ready", "starting", "draining", "maintenance", "unavailable"))
    parser.add_argument("model", choices=("Qwen3.8-27B-FP8", "Qwen3.5-9B", "Qwen3.8-27B-MXFP4", "none"))
    parser.add_argument("--images", action="store_true")
    parser.add_argument("--media-address", help="service-tools ClusterIP:port; draining waits for its jobs")
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
            if args.media_address:
                wait_for_media(args.media_address, int(os.environ.get("SESSION_MEDIA_DRAIN_SECONDS", "1260")))
    except (ValueError, OSError, KeyError, TypeError):
        print("Gateway admission update failed; session must remain closed.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
