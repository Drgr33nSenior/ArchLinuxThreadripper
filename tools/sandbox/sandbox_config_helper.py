"""Helper for make-sandbox-config.sh. Standard library only (the `extract` step runs inside the
Open WebUI pod). It never prints an email, token or account id: account ids stay in the
dump file and the private outputs, and the terminal shows names, roles and groups only.

  extract DB                     read the Open WebUI SQLite database read-only; JSON to stdout
  propose DUMP REVIEW            print the accounts and write the review file (no ids in it)
  pick-ips CIDRS SVC IPADDR      choose the two egress proxy addresses (JSON to stdout)
  api-endpoint SLICES            the node API endpoint of the `kubernetes` Service
  build DUMP REVIEW USERS SANDBOX --src SRC --homes PATH --ips JSON --api IP:PORT --image REF --egress-image REF
                                 write and validate the private files (temporary paths)
"""
import argparse
import ipaddress
import json
import os
import re
import sqlite3
import sys
import unicodedata
from pathlib import Path

GROUP_NAMES = ("Parents", "Family")
DEFAULT_QUOTA = "20G"
FAMILY_DNS = ["1.1.1.3", "1.0.0.3"]


def extract(db):
    # mode=ro: no write, no schema change; WAL readers do not block the running service.
    connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
    users = [{"id": row[0], "name": row[1] or "", "role": row[2] or ""}
             for row in connection.execute('select id, name, role from "user" order by created_at, id')]
    groups = [{"id": row[0], "name": row[1] or ""} for row in connection.execute('select id, name from "group" order by name, id')]
    members = {}
    for group_id, user_id in connection.execute("select group_id, user_id from group_member"):
        members.setdefault(user_id, []).append(group_id)
    for user in users:
        user["group_ids"] = sorted(members.get(user["id"], []))
    json.dump({"users": users, "groups": groups}, sys.stdout)


def load_dump(path):
    dump = json.loads(Path(path).read_text())
    groups = {}
    for name in GROUP_NAMES:
        found = [g["id"] for g in dump["groups"] if g["name"] == name]
        if len(found) != 1:
            raise SystemExit(f"STOP: expected exactly one Open WebUI group named {name}, found {len(found)}")
        groups[name] = found[0]
    names = {g["id"]: g["name"] for g in dump["groups"]}
    for user in dump["users"]:
        user["groups"] = sorted(names.get(g, "?") for g in user["group_ids"])
    return dump, groups


def resolved(user):
    """The group the gate gives this account under most-permissive: Admin, Parents, Family."""
    if user["role"] == "admin":
        return "Admin"
    for name in GROUP_NAMES:
        if name in user["groups"]:
            return name
    return None


def label(name, taken):
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    base = re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-")
    if not base or not base[0].isalpha():
        base = ("user-" + base).strip("-")
    base = base[:32].strip("-")
    candidate, number = base, 2
    while candidate in taken:
        suffix = f"-{number}"
        candidate = base[:32 - len(suffix)].strip("-") + suffix
        number += 1
    taken.add(candidate)
    return candidate


def describe(user):
    return f'{user["name"] or "(no name)"} ({user["role"]}; groups: {", ".join(user["groups"]) or "none"})'


def propose(dump_path, review_path):
    dump, _ = load_dump(dump_path)
    users = dump["users"]
    roles = {}
    for user in users:
        roles[user["role"]] = roles.get(user["role"], 0) + 1
    print(f'Open WebUI: {len(users)} accounts ({", ".join(f"{n} {r}" for r, n in sorted(roles.items()))}); '
          f'groups: {", ".join(sorted(g["name"] for g in dump["groups"]))}')
    taken, lines = set(), []
    for row, user in enumerate(users, 1):
        group = resolved(user)
        include = "yes" if group else "no"
        home = label(user["name"] or f"user-{row}", taken)
        lines.append(f'{row}\t{include}\t{home}\t{group or "-"}\t{DEFAULT_QUOTA}\t# {describe(user)}'
                     + ("" if group else "  NO SANDBOX ACCESS (no group, not admin)"))
    header = [
        "# Sandbox homes: one line per Open WebUI account. Edit columns 2-5; keep the row number.",
        "# include  yes or no. An account left out still gets a workspace if its groups allow,",
        "#          with a disposable home (deleted at teardown).",
        "# home     lowercase DNS label, at most 32 characters, unique. It names the NAS dataset,",
        "#          export and claim: do not change it once the home holds files.",
        "# group    Admin (admin role only), Parents or Family, and only a group the account holds.",
        "#          The proposal follows most-permissive: Admin, then Parents, then Family.",
        "# quota    whole gibibytes, 1G to 500G.",
        "# row\tinclude\thome\tgroup\tquota\t# account",
    ]
    Path(review_path).write_text("\n".join(header + lines) + "\n")
    print("Proposed homes:")
    for line in lines:
        print("  " + line.replace("\t", "  "))


def read_review(dump, review_path):
    users = dump["users"]
    entries, seen = [], set()
    for number, raw in enumerate(Path(review_path).read_text().splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        fields = line.split()
        if len(fields) != 5:
            raise ValueError(f"review line {number}: expected 5 columns (row include home group quota)")
        row, include, home, group, quota = fields
        if not row.isdigit() or not 1 <= int(row) <= len(users) or row in seen:
            raise ValueError(f"review line {number}: unknown or repeated row {row}")
        seen.add(row)
        if include not in {"yes", "no"}:
            raise ValueError(f"review line {number}: include must be yes or no")
        if include == "no":
            continue
        user = users[int(row) - 1]
        if group == "Admin" and user["role"] != "admin":
            raise ValueError(f"review line {number}: Admin is only for the admin-role account")
        if group in GROUP_NAMES and group not in user["groups"]:
            raise ValueError(f"review line {number}: the account is not in {group}; the gate would deny it")
        if group not in {"Admin", *GROUP_NAMES}:
            raise ValueError(f"review line {number}: group must be Admin, Parents or Family")
        entries.append({"webui_user_id": user["id"], "name": home, "group": group, "quota": quota})
    if len(seen) != len(users):
        raise ValueError(f"the review lists {len(seen)} of {len(users)} accounts; keep every row (include no to leave one out)")
    return entries


def pick_ips(cidrs_path, services_path, addresses_path):
    cidrs = [c for item in json.loads(Path(cidrs_path).read_text())["items"] for c in item["spec"]["cidrs"]]
    v4 = [ipaddress.ip_network(c) for c in cidrs if ipaddress.ip_network(c).version == 4]
    if len(v4) != 1:
        raise SystemExit(f"STOP: expected one IPv4 service CIDR, found {len(v4)}")
    network = v4[0]
    used = {s["spec"].get("clusterIP") for s in json.loads(Path(services_path).read_text())["items"]}
    used |= {ip for s in json.loads(Path(services_path).read_text())["items"] for ip in s["spec"].get("clusterIPs", [])}
    used |= {a["metadata"]["name"] for a in json.loads(Path(addresses_path).read_text())["items"]}
    # The low band Kubernetes keeps for fixed assignments: min(max(16, size/16), 256) addresses.
    band = min(max(16, network.num_addresses // 16), 256)
    hosts = [network.network_address + offset for offset in range(1, band)]
    preferred = [network.network_address + 201, network.network_address + 202]
    free = [str(ip) for ip in preferred + [h for h in reversed(hosts) if h not in preferred]
            if ip in network and str(ip) not in used and int(ip) - int(network.network_address) < band
            and ip not in (network.network_address + 1, network.network_address + 10)]
    if len(free) < 2:
        raise SystemExit("STOP: fewer than two free addresses in the service range's static band")
    json.dump({"cidr": str(network), "family": free[0], "parents": free[1]}, sys.stdout)


def api_endpoint(slices_path):
    items = json.loads(Path(slices_path).read_text())["items"]
    addresses = {a for s in items if s.get("addressType") == "IPv4" for e in s.get("endpoints", []) for a in e["addresses"]}
    ports = {p["port"] for s in items for p in s.get("ports") or [] if p.get("name") == "https"}
    if len(addresses) != 1 or len(ports) != 1:
        raise SystemExit(f"STOP: expected one API server address and port, found {len(addresses)} and {len(ports)}")
    address = addresses.pop()
    if not ipaddress.ip_address(address).is_private:
        raise SystemExit("STOP: the API server address is not private")
    print(f"{address}:{ports.pop()}")


def build(args):
    dump, groups = load_dump(args.dump)
    users = read_review(dump, args.review)
    Path(args.users).write_text(json.dumps({"users": users}, indent=2) + "\n")
    ips = json.loads(args.ips)
    address, port = args.api.rsplit(":", 1)
    config = {"image": args.image, "egress_image": args.egress_image,
              "parents_group_id": groups["Parents"], "family_group_id": groups["Family"],
              "family_enabled": True, "group_resolution": "most-permissive",
              "egress_service_ips": {"family": ips["family"], "parents": ips["parents"]},
              "api_server_cidr": f"{address}/32", "api_server_port": int(port),
              "homes": args.homes, "family_dns": FAMILY_DNS}
    Path(args.sandbox).write_text(json.dumps(config, indent=2) + "\n")
    # Validate with the repository's own loaders, reading the temporary users file.
    sys.path.insert(0, str(Path(args.src) / "lib/workstation"))
    import sandbox
    probe = Path(args.sandbox).with_name(Path(args.sandbox).name + ".check")
    probe.write_text(json.dumps({**config, "homes": str(Path(args.users).resolve())}))
    try:
        loaded = sandbox.read_config(probe)
    finally:
        probe.unlink()
    if len(loaded["users"]) != len(users):
        raise SystemExit("STOP: the repository loader read a different number of homes")
    sandbox.egress_policies()
    by_group = {}
    for user in loaded["users"]:
        by_group.setdefault(user["group"], []).append(f'{user["name"]} ({user["quota"]})')
    print("Validated with the repository loaders (sandbox.read_config, sandbox_homes.read_users):")
    print(f"  homes: {len(users)}; " + "; ".join(f"{g}: {', '.join(n)}" for g, n in sorted(by_group.items())))
    print(f'  egress proxies: family {ips["family"]}, parents {ips["parents"]}; API {address}/32:{port}')
    print(f'  family_enabled true, group_resolution most-permissive, family_dns {", ".join(FAMILY_DNS)}')


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("extract").add_argument("db")
    p = sub.add_parser("propose")
    p.add_argument("dump")
    p.add_argument("review")
    p = sub.add_parser("pick-ips")
    for name in ("cidrs", "services", "addresses"):
        p.add_argument(name)
    sub.add_parser("api-endpoint").add_argument("slices")
    p = sub.add_parser("build")
    for name in ("dump", "review", "users", "sandbox"):
        p.add_argument(name)
    for name in ("src", "homes", "ips", "api", "image", "egress-image"):
        p.add_argument("--" + name, required=True)
    args = parser.parse_args()
    if args.action == "extract":
        extract(args.db)
    elif args.action == "propose":
        propose(args.dump, args.review)
    elif args.action == "pick-ips":
        pick_ips(args.cidrs, args.services, args.addresses)
    elif args.action == "api-endpoint":
        api_endpoint(args.slices)
    else:
        try:
            build(args)
        except ValueError as error:
            print(f"STOP: {error}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    os.umask(0o077)
    raise SystemExit(main())
