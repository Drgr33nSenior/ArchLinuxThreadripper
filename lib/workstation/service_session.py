"""Private host-to-gateway admission control; caller holds the session lock."""
import argparse
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import sys
import time
import urllib.error
import urllib.request

PHASES = ("ready", "starting", "draining", "maintenance", "unavailable")
DRAIN_SECONDS = 180
RECOVERY = ("To recover, fix the cause and rerun the switch (sudo workstationctl mode <name> --execute), "
            "or restore the previous session (sudo workstationctl session restore <hardware.json> "
            "<state-directory> --execute).")


class AdmissionError(ValueError):
    """A failed admission step. Its text names only facts that are safe to print: never the
    control credential, and never a response body, which could echo it."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())


def private(host):
    try:
        return ipaddress.ip_address(host).is_private
    except ValueError:
        return False


def cause(error):
    """Printable cause of a failed call: the HTTP status and refusal code, or the transport error."""
    if isinstance(error, urllib.error.HTTPError):
        try:
            code = json.loads(error.read(4096))["error"]["code"]
        except (OSError, ValueError, TypeError, KeyError):
            code = None
        # Gateway refusal codes are fixed identifiers; anything else in the body is not echoed.
        suffix = f" {code}" if isinstance(code, str) and re.fullmatch(r"[a-z_]{1,64}", code) else ""
        return f"refused: HTTP {error.code}{suffix}"
    if isinstance(error, urllib.error.URLError) and isinstance(error.reason, OSError):
        error = error.reason
    if isinstance(error, TimeoutError):
        return "no answer within 5 s"
    if isinstance(error, OSError):
        return f"unreachable: {error.strerror or type(error).__name__}"
    return f"answer malformed ({type(error).__name__})"


def read_credential(credential):
    path = Path(credential)
    try:
        meta = path.stat()
        if path.is_symlink() or meta.st_uid != 0 or stat.S_IMODE(meta.st_mode) != 0o600:
            raise AdmissionError(f"control credential {credential} must be a root-owned mode 0600 file")
        key = path.read_text().strip()
    except (OSError, UnicodeDecodeError) as error:
        reason = getattr(error, "strerror", None) or type(error).__name__
        raise AdmissionError(f"control credential {credential} unreadable ({reason})") from None
    if len(key) < 32:
        raise AdmissionError(f"control credential {credential} is too short")
    return key


def control(address, credential, phase, model=None, images=False, cancel=False):
    if not private(address):
        raise AdmissionError(f"gateway address {address} is not a private ClusterIP")
    key = read_credential(credential)
    request = urllib.request.Request(f"http://{address}:4000/_service/control",
        data=json.dumps({"phase": phase, "model": model, "images": images, "cancel": cancel}).encode(),
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    call = "cancel call" if cancel else "control call"
    try:
        with opener().open(request, timeout=5) as response:
            answer = json.loads(response.read(8192))
    except (OSError, ValueError) as error:
        raise AdmissionError(f"{call} to {address}:4000 {cause(error)}") from None
    if not isinstance(answer, dict) or type(answer.get("active")) is not int:
        raise AdmissionError(f"{call} to {address}:4000 answered without an active request count")
    return answer


def gateway_phase(address):
    """The gateway's phase from its credential-free status, or None when it does not say."""
    if not private(address):
        return None
    try:
        with opener().open(f"http://{address}:4000/_service/status", timeout=5) as response:
            phase = json.loads(response.read(4096)).get("phase")
    except (OSError, ValueError, AttributeError):
        return None
    return phase if phase in PHASES else None


def media_active(address):
    """Pictures and videos service-tools is still producing (its /health; no credential)."""
    host, _, port = address.rpartition(":")
    if not private(host) or not port.isdigit():
        raise AdmissionError(f"service-tools address {address} is not a private ClusterIP:port")
    try:
        with opener().open(f"http://{host}:{port}/health", timeout=5) as response:
            health = json.loads(response.read(65536))
        return int(health.get("image_active", 0)) + int(bool(health.get("video_active")))
    except (OSError, ValueError, TypeError, AttributeError) as error:
        raise AdmissionError(f"service-tools health at {address} {cause(error)}") from None


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
        raise AdmissionError(f"{active} picture/video job(s) still running after {seconds} s")


def report_failure(address, phase, reason):
    """Name the failed step, the phase the gateway is left in, and the way back."""
    now = gateway_phase(address)
    print(f"Gateway admission update failed: {phase}: {reason}.", file=sys.stderr)
    print(f"Gateway phase now: {now or 'unknown (its status did not answer)'}. {RECOVERY}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("address")
    parser.add_argument("credential")
    parser.add_argument("phase", choices=PHASES)
    parser.add_argument("model", choices=("Qwen3.8-27B-FP8", "Qwen3.5-9B", "Qwen3.8-27B-MXFP4", "none"))
    parser.add_argument("--images", action="store_true")
    parser.add_argument("--media-address", help="service-tools ClusterIP:port; draining waits for its jobs")
    args = parser.parse_args()
    try:
        if os.geteuid() != 0:
            raise AdmissionError("not running as root")
        result = control(args.address, args.credential, args.phase,
                         None if args.model == "none" else args.model, args.images)
        if args.phase == "draining":
            deadline = time.monotonic() + DRAIN_SECONDS
            while result["active"] and time.monotonic() < deadline:
                time.sleep(1)
                result = control(args.address, args.credential, "draining")
            if result["active"]:
                control(args.address, args.credential, "draining", cancel=True)
                time.sleep(2)
                active = control(args.address, args.credential, "draining")["active"]
                if active:
                    raise AdmissionError(f"{active} gateway request(s) did not cancel within {DRAIN_SECONDS} s")
            if args.media_address:
                wait_for_media(args.media_address, int(os.environ.get("SESSION_MEDIA_DRAIN_SECONDS", "1260")))
    except AdmissionError as error:
        report_failure(args.address, args.phase, str(error))
        return 1
    except (ValueError, OSError, KeyError, TypeError) as error:
        report_failure(args.address, args.phase, f"unexpected {type(error).__name__}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
