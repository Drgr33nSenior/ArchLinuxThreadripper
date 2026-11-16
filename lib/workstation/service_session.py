"""Private host-to-gateway admission control; caller holds the session lock."""
import argparse
import http.client
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
GATEWAY_PORT = 4000
# `session switch` and `session restore` load no configuration by themselves and sudo
# resets the environment, so the recovery commands name the host configuration.
WORKSTATIONCTL = "sudo workstationctl --config /etc/workstation/workstation.conf"
STATE_DIRECTORY = "/var/lib/workstation-session"


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
        except (OSError, ValueError, TypeError, KeyError, http.client.HTTPException):
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
    # A malformed reply (bad status line, bad JSON) is named by its type only, never echoed.
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
    # Checked here so a line break or non-ASCII byte is named as a credential problem,
    # not as a failed request.
    if not re.fullmatch(r"[\x21-\x7e]+", key):
        raise AdmissionError(f"control credential {credential} has characters a request header cannot carry")
    return key


def control(address, credential, phase, model=None, images=False, cancel=False):
    if not private(address):
        raise AdmissionError(f"gateway address {address} is not a private ClusterIP")
    key = read_credential(credential)
    request = urllib.request.Request(f"http://{address}:{GATEWAY_PORT}/_service/control",
        data=json.dumps({"phase": phase, "model": model, "images": images, "cancel": cancel}).encode(),
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    call = f"{'cancel' if cancel else 'control'} call to {address}:{GATEWAY_PORT}"
    try:
        with opener().open(request, timeout=5) as response:
            answer = json.loads(response.read(8192))
    except (OSError, ValueError, http.client.HTTPException) as error:
        raise AdmissionError(f"{call} {cause(error)}") from None
    if not isinstance(answer, dict) or type(answer.get("active")) is not int:
        raise AdmissionError(f"{call} answered without an active request count")
    return answer


def gateway_phase(address):
    """The gateway's phase from its credential-free status, or None when it does not say."""
    if not private(address):
        return None
    try:
        with opener().open(f"http://{address}:{GATEWAY_PORT}/_service/status", timeout=5) as response:
            phase = json.loads(response.read(4096)).get("phase")
    except Exception:  # Best-effort label after a failure; the reply is never echoed.
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
    except (OSError, ValueError, TypeError, AttributeError, http.client.HTTPException) as error:
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


def default_hardware():
    """This boot's evidence path, as `workstationctl mode` finds it (mode.sh)."""
    try:
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip().replace("-", "")
    except OSError:
        boot = ""
    return f"/var/lib/workstation-evidence/boot-{boot if re.fullmatch(r'[0-9a-f]{32}', boot) else '<id>'}/hardware.json"


def word(value, placeholder):
    """A mode name as given, when it is one plain word; otherwise the placeholder. The recovery
    options are never validated against a fixed list: a mode added to the shell side later must
    not make every admission call (draining included) fail its argument parsing."""
    return value if re.fullmatch(r"[a-z]{1,32}", value or "") else placeholder


def recovery(args):
    """The commands that return to a known session, for the command that started this switch."""
    hardware = args.hardware or default_hardware()
    state = args.state_directory or STATE_DIRECTORY
    restore = f"{WORKSTATIONCTL} session restore {hardware} {state} --execute"
    if args.mode_name:
        rerun = f"{WORKSTATIONCTL} mode {word(args.mode_name, '<name>')} --execute"
    elif args.switch == "restore":
        rerun = restore
    elif args.switch:
        rerun = f"{WORKSTATIONCTL} session switch {word(args.switch, '<mode>')} {hardware} {state} --execute"
        if args.apply:
            rerun += f" --apply {args.apply}"
    else:
        rerun = f"{WORKSTATIONCTL} mode <name> --execute"
    return f"To recover, fix the cause and rerun the switch ({rerun}), or restore the previous session ({restore})."


def report_failure(args, reason):
    """Name the failed step first, so it is printed whatever the status query does, then the
    phase the gateway is left in and the way back."""
    print(f"Gateway admission update failed: {args.phase}: {reason}.", file=sys.stderr, flush=True)
    now = gateway_phase(args.address)
    print(f"Gateway phase now: {now or 'unknown (its status did not answer)'}. {recovery(args)}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("address")
    parser.add_argument("credential")
    parser.add_argument("phase", choices=PHASES)
    parser.add_argument("model", choices=("Qwen3.8-27B-FP8", "Qwen3.5-9B", "Qwen3.8-27B-MXFP4", "none"))
    parser.add_argument("--images", action="store_true")
    parser.add_argument("--media-address", help="service-tools ClusterIP:port; draining waits for its jobs")
    # Used only for the recovery commands printed on failure.
    parser.add_argument("--hardware", help="hardware evidence of the switch")
    parser.add_argument("--state-directory", help="session state directory of the switch")
    parser.add_argument("--mode-name", help="set when `workstationctl mode` started the switch")
    parser.add_argument("--switch", help="the `session switch` mode otherwise")
    parser.add_argument("--apply", help="the render the switch applies")
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
        report_failure(args, str(error))
        return 1
    except (ValueError, OSError, KeyError, TypeError, http.client.HTTPException) as error:
        report_failure(args, f"unexpected {type(error).__name__}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
