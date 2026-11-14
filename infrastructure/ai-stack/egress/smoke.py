"""Offline smoke test for the workspace egress proxy image.

`requests` runs in a client container that shares the proxy container's network
namespace (loopback only, no network): it sends CONNECT and plain requests through the
proxy started with smoke-acl.yaml and the "family" group, with the .test names pinned
by --add-host. `logs` then reads the proxy's output on stdin and requires that every
line carries only the group, the domain and the decision.
"""
import json
import socket
import sys
import time
import urllib.request

PROXY = ("127.0.0.1", 4750)
SECRET = "/private/path?token=SMOKE-SECRET"


def status(request):
    with socket.create_connection(PROXY, timeout=20) as connection:
        connection.sendall(request.encode())
        data = b""
        while b"\r\n" not in data:
            chunk = connection.recv(4096)
            if not chunk:
                break
            data += chunk
    return int(data.split()[1])


def connect(host, port=443):
    return status(f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n")


def get(host):
    return status(f"GET http://{host}{SECRET} HTTP/1.1\r\nHost: {host}\r\nCookie: SMOKE-SECRET\r\n\r\n")


def requests():
    for _ in range(50):
        try:
            socket.create_connection(PROXY, timeout=1).close()
            break
        except OSError:
            time.sleep(0.2)
    denied = 407  # Smokescreen's "request rejected by proxy"
    checks = {
        # Not on this group's list, including another group's entry and a look-alike.
        "unlisted": connect("unlisted.sandbox.test") == denied,
        "other group's list": connect("parents-only.sandbox.test") == denied,
        "suffix look-alike": connect("public.sandbox.test.evil.test") == denied,
        "plain unlisted": get("unlisted.sandbox.test") == denied,
        # Listed names that resolve to the LAN, the cluster or loopback: refused after resolution.
        "listed name on the LAN": connect("lan.sandbox.test") == denied,
        "listed name in the cluster": connect("cluster.sandbox.test") == denied,
        "listed loopback": connect("localhost", 9810) == denied,
        "plain listed name on the LAN": get("lan.sandbox.test") == denied,
        # Address literals never match a domain list.
        "LAN literal": connect("192.168.1.1") == denied,
        "metadata literal": connect("169.254.169.254", 80) == denied,
        # Listed and public: allowed by policy, then the dial fails (this container has no network).
        "listed public": connect("public.sandbox.test") in (502, 504),
        "listed wildcard": connect("a.wild.sandbox.test") in (502, 504),
        "plain listed public": get("public.sandbox.test") in (502, 504),
    }
    failed = [name for name, passed in checks.items() if not passed]
    assert not failed, f"egress smoke failed: {failed}"
    metrics = urllib.request.urlopen("http://127.0.0.1:9810/metrics", timeout=5).read().decode()
    assert 'workstation_sandbox_egress_requests_total{group="family",decision="allow"} 3' in metrics, metrics
    assert 'workstation_sandbox_egress_requests_total{group="family",decision="deny"} 10' in metrics, metrics
    print("egress proxy image smoke: requests ok")


def logs():
    lines = [json.loads(line) for line in sys.stdin.read().splitlines() if line.strip()]
    assert len(lines) == 13, f"expected one line per request, got {len(lines)}"
    for line in lines:
        assert set(line) == {"group", "domain", "decision"}, line
        assert line["group"] == "family" and line["decision"] in {"allow", "deny"}, line
        assert "/" not in line["domain"] and "SECRET" not in json.dumps(line), line
    assert {"domain": "lan.sandbox.test", "decision": "deny", "group": "family"} in lines
    assert {"domain": "public.sandbox.test", "decision": "allow", "group": "family"} in lines
    print("egress proxy image smoke: log carries only group, domain and decision")


if __name__ == "__main__":
    {"requests": requests, "logs": logs}[sys.argv[1]]()
