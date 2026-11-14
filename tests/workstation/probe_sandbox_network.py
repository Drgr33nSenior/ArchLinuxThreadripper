"""Network-denial probe, run inside a coding workspace pod (docs/ASSISTANT.md).

Checks what the workspace can reach directly (it must be nothing but its own proxy)
and what its proxy lets through. Standard library only; it sends TCP connects, one DNS
question and CONNECT requests, never credentials or content. Exits 1 on any surprise.

    python probe_sandbox_network.py '<json>'

    {"closed": {"node API": ["10.0.2.15", 6443], ...},      direct connects that must fail
     "open":   {"own proxy": ["10.43.0.201", 4750]},        direct connects that must succeed
     "dns": "10.43.0.10",                                   a resolver that must not answer
     "allowed": ["pypi.org"], "denied": ["github.com", ...]}   CONNECT host[:port] through the proxy
"""
import json
import os
import socket
import sys
import urllib.parse


def connects(host, port, seconds=4):
    try:
        socket.create_connection((host, port), timeout=seconds).close()
        return True
    except OSError:
        return False


def dns_answers(server):
    # One A question for example.com, over UDP and then TCP.
    question = b"\x12\x34\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00\x07example\x03com\x00\x00\x01\x00\x01"
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
        udp.settimeout(3)
        try:
            udp.sendto(question, (server, 53))
            if udp.recv(512):
                return True
        except OSError:
            pass
    return connects(server, 53, 3)


def proxy_status(target):
    """Status line of a CONNECT through the workspace's configured proxy (0: no reply)."""
    proxy = urllib.parse.urlsplit(os.environ["HTTPS_PROXY"])
    host, _, port = target.partition(":")
    target = f"{host}:{port or 443}"
    try:
        with socket.create_connection((proxy.hostname, proxy.port), timeout=5) as connection:
            connection.settimeout(25)
            connection.sendall(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())
            reply = connection.recv(256)
        return int(reply.split()[1])
    except (OSError, ValueError, IndexError):
        return 0


def main():
    spec = json.loads(sys.argv[1])
    rows = []
    for name, (host, port) in spec.get("closed", {}).items():
        rows.append((f"direct {name} {host}:{port}", "closed", "open" if connects(host, port) else "closed"))
    for name, (host, port) in spec.get("open", {}).items():
        rows.append((f"direct {name} {host}:{port}", "open", "open" if connects(host, port) else "closed"))
    if spec.get("dns"):
        rows.append((f'direct DNS {spec["dns"]}', "no answer", "answer" if dns_answers(spec["dns"]) else "no answer"))
    try:
        socket.getaddrinfo("pypi.org", 443)
        rows.append(("name resolution in the workspace", "fails", "works"))
    except OSError:
        rows.append(("name resolution in the workspace", "fails", "fails"))
    for target in spec.get("allowed", []):
        rows.append((f"proxy {target}", "200", str(proxy_status(target))))
    for target in spec.get("denied", []):
        status = proxy_status(target)
        # 407 is the proxy's refusal; anything but a 2xx tunnel is a denial.
        rows.append((f"proxy {target}", "refused", "refused" if not 200 <= status < 300 else f"TUNNEL {status}"))
    failed = 0
    for name, want, got in rows:
        ok = want == got
        failed += not ok
        print(f'{"ok  " if ok else "FAIL"} {name}: {got}' + ("" if ok else f" (expected {want})"))
    print(f"{len(rows) - failed}/{len(rows)} as expected")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
