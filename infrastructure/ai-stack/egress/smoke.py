"""Offline smoke test for the workspace egress proxy image.

`family` and `parents` run in a client container that shares the proxy container's
network namespace (loopback only, no network) and send CONNECT and plain requests
through the proxy. For `family` this script also answers the proxy's DNS questions on
127.0.0.1:5353, standing in for a filtering resolver: it answers 0.0.0.0 for a name it
"blocks". For `parents` the .test names are pinned with --add-host. `logs N` then reads
the proxy's whole output on stdin and requires N lines that carry only the group, the
domain and the decision.
"""
import json
import socket
import struct
import sys
import threading
import time
import urllib.request

PROXY = ("127.0.0.1", 4750)
SECRET = "/private/path?token=SMOKE-SECRET"
ANSWERS = {"public.sandbox.test": "203.0.113.9", "lan.sandbox.test": "192.168.1.10",
           "cluster.sandbox.test": "10.43.0.1", "filtered.sandbox.test": "0.0.0.0"}
FAMILY, BLOCKLIST, PRIVATE = "blocked by the family filter", "blocked by the sandbox blocklist", "private address"
LITERAL = "address literals are not allowed for this group"
REACHED = "reached"  # allowed by policy; the dial then fails because this container has no network


def resolver():
    server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server.bind(("127.0.0.1", 5353))
    while True:
        query, client = server.recvfrom(512)
        labels, at = [], 12
        while query[at]:
            labels.append(query[at + 1:at + 1 + query[at]].decode())
            at += query[at] + 1
        question = query[12:at + 5]
        address = ANSWERS.get(".".join(labels).lower())
        qtype = struct.unpack("!H", query[at + 1:at + 3])[0]
        if address is None:
            reply = query[:2] + b"\x81\x83\x00\x01\x00\x00\x00\x00\x00\x00" + question  # NXDOMAIN
        elif qtype != 1:
            reply = query[:2] + b"\x81\x80\x00\x01\x00\x00\x00\x00\x00\x00" + question  # no such record
        else:
            reply = (query[:2] + b"\x81\x80\x00\x01\x00\x01\x00\x00\x00\x00" + question +
                     b"\xc0\x0c\x00\x01\x00\x01\x00\x00\x00\x3c\x00\x04" + socket.inet_aton(address))
        server.sendto(reply, client)


def ask(request):
    """(outcome, body): a refusal's reason header, REACHED for a 502/504, else the status."""
    with socket.create_connection(PROXY, timeout=20) as connection:
        connection.sendall(request.encode())
        data = b""
        while True:
            chunk = connection.recv(4096)
            if not chunk:
                break
            data += chunk
            head, _, body = data.partition(b"\r\n\r\n")
            if _ and (b"content-length: 0" in head.lower() or body):
                break
    head, _, body = data.partition(b"\r\n\r\n")
    code = int(head.split()[1])
    headers = {k.strip().lower(): v.strip() for k, _, v in
               (line.partition(":") for line in head.decode("latin1").split("\r\n")[1:])}
    if code == 403:
        assert "smokescreen" not in head.decode("latin1").lower(), head
        return headers.get("x-sandbox-blocked"), body.decode()
    return (REACHED if code in (502, 504) else code), body.decode()


def connect(host, port=443):
    return ask(f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n")[0]


def get(host):
    return ask(f"GET http://{host}{SECRET} HTTP/1.1\r\nHost: {host}\r\nCookie: SMOKE-SECRET\r\nConnection: close\r\n\r\n")


def run(name, checks, allowed, denied):
    failed = {label: got for label, (got, want) in checks.items() if got != want}
    assert not failed, f"egress smoke ({name}) failed: {failed}"
    metrics = urllib.request.urlopen("http://127.0.0.1:9810/metrics", timeout=5).read().decode()
    assert f'requests_total{{group="{name}",decision="allow"}} {allowed}' in metrics, metrics
    assert f'requests_total{{group="{name}",decision="deny"}} {denied}' in metrics, metrics
    return metrics


def wait():
    for _ in range(50):
        try:
            socket.create_connection(PROXY, timeout=1).close()
            return
        except OSError:
            time.sleep(0.2)


def family():
    threading.Thread(target=resolver, daemon=True).start()
    wait()
    plain, body = get("filtered.sandbox.test")
    metrics = run("family", {
        # On a maintained list baked into the image, by name and by parent domain.
        "listed adult site": (connect("pornhub.com"), FAMILY),
        "subdomain of a listed site": (connect("www.pornhub.com"), FAMILY),
        "listed bypass service": (connect("nordvpn.com"), FAMILY),
        "owner's blocklist": (connect("owner-blocked.sandbox.test"), BLOCKLIST),
        # Not on any list, but the filtering resolver answers 0.0.0.0.
        "name the resolver filters": (connect("filtered.sandbox.test"), FAMILY),
        "plain request for a filtered name": (plain, FAMILY),
        "refusal body is the short reason": (body, "Blocked by the sandbox proxy: " + FAMILY + ".\n"),
        # Names that resolve to the LAN, the cluster or loopback.
        "name on the LAN": (connect("lan.sandbox.test"), PRIVATE),
        "name in the cluster": (connect("cluster.sandbox.test"), PRIVATE),
        "loopback": (connect("localhost", 9810), PRIVATE),
        # No name filter can judge an address.
        "public literal": (connect("203.0.113.9"), LITERAL),
        "LAN literal": (connect("192.168.1.1"), LITERAL),
        # Everything else is open.
        "unlisted public name": (connect("public.sandbox.test"), REACHED),
        "plain unlisted public name": (get("public.sandbox.test")[0], REACHED),
        "unknown name": (connect("nowhere.sandbox.test"), REACHED),
    }, allowed=3, denied=11)
    for name in ("nsfw", "bypass", "gambling"):
        assert f'blocklist_entries{{group="family",list="{name}"}}' in metrics, metrics
    print("egress proxy image smoke: family ok")


def parents():
    wait()
    run("parents", {
        "owner's blocklist": (connect("owner-blocked.sandbox.test"), BLOCKLIST),
        "subdomain of the owner's entry": (connect("a.owner-blocked.sandbox.test"), BLOCKLIST),
        "name on the LAN": (connect("lan.sandbox.test"), PRIVATE),
        "name in the cluster": (connect("cluster.sandbox.test"), PRIVATE),
        "loopback": (connect("localhost", 9810), PRIVATE),
        "LAN literal": (connect("192.168.1.1"), PRIVATE),
        "metadata literal": (connect("169.254.169.254", 80), PRIVATE),
        "plain name on the LAN": (get("lan.sandbox.test")[0], PRIVATE),
        # Open: any public name or address, including ones the family lists block.
        "public name": (connect("public.sandbox.test"), REACHED),
        "public literal": (connect("203.0.113.9"), REACHED),
        "plain public name": (get("public.sandbox.test")[0], REACHED),
        "family-listed name": (connect("pornhub.com"), REACHED),
    }, allowed=4, denied=8)
    print("egress proxy image smoke: parents ok")


def logs(expected):
    lines = [json.loads(line) for line in sys.stdin.read().splitlines() if line.strip()]
    assert len(lines) == expected, f"expected {expected} lines, one per request, got {len(lines)}"
    for line in lines:
        assert set(line) == {"group", "domain", "decision"}, line
        assert line["decision"] in {"allow", "deny"}, line
        assert "/" not in line["domain"] and "SECRET" not in json.dumps(line), line
    print("egress proxy image smoke: log carries only group, domain and decision")


if __name__ == "__main__":
    if sys.argv[1] == "logs":
        logs(int(sys.argv[2]))
    else:
        {"family": family, "parents": parents}[sys.argv[1]]()
