"""Run on a second device; observe HTTPS availability and optionally notify."""
import argparse
import json
from pathlib import Path
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


def exchange(url, data=None, ca_file=None):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.fragment:
        raise ValueError("credential-free HTTPS endpoint required")
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(),
        urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=ca_file)))
    with opener.open(urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}), timeout=10) as result:
        return result.read(8192)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config")
    parser.add_argument("--notify", action="store_true")
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text())
    remaining = config.get("maintenance_until", 0) - time.time()
    if remaining > 14400:
        raise ValueError("external maintenance period exceeds four hours")
    if remaining > 0:
        print("External heartbeat is in an owner-scheduled maintenance period")
        return 0
    healthy = False
    try:
        state = json.loads(exchange(config["status_url"], ca_file=config.get("ca_file")))
        # Maintenance is intentional. A second device reports loss of the
        # entire HTTPS path, independent of the active model or local alerts.
        healthy = state["phase"] in {"ready", "starting", "draining", "unavailable", "maintenance"}
    except (OSError, ValueError, KeyError, urllib.error.URLError):
        pass
    if not healthy and args.notify:
        try:
            webhook = Path(config["webhook_file"])
            if webhook.is_symlink() or webhook.stat().st_mode & 0o077:
                raise ValueError("protected webhook file required")
            exchange(webhook.read_text().strip(), json.dumps({"status": "firing", "alerts": [
                {"labels": {"alertname": "WorkstationExternalHeartbeatFailed", "severity": "critical"},
                 "annotations": {"summary": "Workstation HTTPS service is unreachable from the independent observer"}}]}).encode())
        except (OSError, ValueError, urllib.error.URLError):
            print("External heartbeat notification failed", file=sys.stderr)
            return 2
    print("External HTTPS heartbeat " + ("healthy" if healthy else "failed"))
    return 0 if healthy else 1


if __name__ == "__main__":
    sys.exit(main())
