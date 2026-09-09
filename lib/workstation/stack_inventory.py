"""Read effective Arch package and mirror metadata; never synchronize or upgrade."""
import json
import re
import subprocess
import urllib.parse


def classify(servers):
    result = []
    for server in servers:
        url = urllib.parse.urlsplit(server)
        archive = url.hostname == "archive.archlinux.org" and re.match(r"/repos/[0-9]{4}/[0-9]{2}/[0-9]{2}/", url.path)
        result.append({"host": url.hostname, "scheme": url.scheme,
                       "classification": "dated-archive" if archive else "rolling-candidate-unverified",
                       "archive_date": url.path.split("/")[2:5] if archive else None})
    return result


def main():
    repos = subprocess.check_output(["pacman-conf", "--repo-list"], text=True).splitlines()
    result = {}
    for repo in repos:
        if not re.fullmatch(r"[a-zA-Z0-9_-]+", repo):
            raise ValueError("unsupported repository name")
        servers = subprocess.check_output(["pacman-conf", "--repo", repo, "Server"], text=True).splitlines()
        result[repo] = classify(servers)
    packages = subprocess.check_output(["pacman", "-Q"], text=True).splitlines()
    print(json.dumps({"schema": 1, "host_packages": packages, "mirrors": result,
                      "container_runtime": "collect separately with rocm serving-evidence; do not mix library prefixes",
                      "remote_freshness_verified": False,
                      "upgrade": "owner-controlled full pacman -Syu after reviewed mirror migration; retain stable/LTS recovery"}, indent=2))


if __name__ == "__main__":
    main()
