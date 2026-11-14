"""Per-user sandbox homes on the NAS: mapping loader and static volume render. No cluster or NAS calls."""
import argparse
import json
from pathlib import Path
import re
import sys

from sandbox import NAMESPACE

GROUPS = ("Parents", "Family", "Admin")
DEFAULT_QUOTA = "20G"
OWNER = 1000
# The same NAS and mount options as the nas-nfs StorageClass; tests keep them in step.
NAS_SERVER = "192.168.30.10"
MOUNT_OPTIONS = ["nfsvers=4.2", "hard", "proto=tcp"]
EXPORT_ROOT = "/srv/microserver/sandbox"
# A statically bound claim cannot be resized, so every claim requests this fixed
# minimum. The volume's capacity carries the quota; the NAS dataset enforces it.
CLAIM_REQUEST = "1Gi"


def read_users(path):
    """Return the validated users, each with its quota filled in."""
    value = json.loads(Path(path).read_text())
    if not isinstance(value, dict) or set(value) != {"users"} or not isinstance(value["users"], list):
        raise ValueError('sandbox user mapping must be {"users": [...]}')
    if len(value["users"]) > 32:
        raise ValueError("at most 32 sandbox users")
    users = []
    for entry in value["users"]:
        if (not isinstance(entry, dict) or not {"webui_user_id", "name", "group"} <= entry.keys()
                or entry.keys() - {"webui_user_id", "name", "group", "quota"}):
            raise ValueError("each sandbox user needs webui_user_id, name and group, and optionally quota")
        user = {**entry, "quota": entry.get("quota", DEFAULT_QUOTA)}
        if not all(isinstance(item, str) for item in user.values()):
            raise ValueError("sandbox user fields must be strings")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", user["webui_user_id"]):
            raise ValueError("webui_user_id must be the exact Open WebUI user id")
        if not re.fullmatch(r"[a-z](?:[a-z0-9-]{0,30}[a-z0-9])?", user["name"]):
            raise ValueError("sandbox user name must be a lowercase DNS label of at most 32 characters")
        if not re.fullmatch(r"(?:[1-9][0-9]?|[1-4][0-9]{2}|500)G", user["quota"]):
            raise ValueError("sandbox quota must be whole gibibytes from 1G to 500G")
        if user["group"] not in GROUPS:
            raise ValueError("sandbox user group must be Parents, Family or Admin")
        users.append(user)
    for key in ("webui_user_id", "name"):
        if len({user[key] for user in users}) != len(users):
            raise ValueError(f"sandbox users need unique {key} values")
    return users


def claim_name(name):
    return f"sandbox-home-{name}"


def render(users):
    """One retained NFS volume and its pre-bound claim per user."""
    objects = []
    for user in users:
        name, share = claim_name(user["name"]), f'{EXPORT_ROOT}/{user["name"]}'
        objects += [
            {"apiVersion": "v1", "kind": "PersistentVolume", "metadata": {"name": name},
             "spec": {"capacity": {"storage": user["quota"] + "i"}, "accessModes": ["ReadWriteOnce"],
                      "persistentVolumeReclaimPolicy": "Retain", "storageClassName": "",
                      "volumeMode": "Filesystem", "mountOptions": MOUNT_OPTIONS,
                      "claimRef": {"namespace": NAMESPACE, "name": name},
                      "csi": {"driver": "nfs.csi.k8s.io", "volumeHandle": f"{NAS_SERVER}{share}##",
                              "volumeAttributes": {"server": NAS_SERVER, "share": share}}}},
            {"apiVersion": "v1", "kind": "PersistentVolumeClaim",
             "metadata": {"name": name, "namespace": NAMESPACE},
             "spec": {"accessModes": ["ReadWriteOnce"], "storageClassName": "", "volumeName": name,
                      "resources": {"requests": {"storage": CLAIM_REQUEST}}}}]
    return objects


def nas_vars(users):
    """Extra-vars for the NAS owner play; carries names and quotas only."""
    return {"microserver_sandbox_homes_enabled": True,
            "microserver_sandbox_homes": [{"name": user["name"], "quota": user["quota"]} for user in users]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["render", "nas-vars"])
    parser.add_argument("mapping", help="private sandbox user mapping (sandbox-users.json)")
    args = parser.parse_args()
    try:
        users = read_users(args.mapping)
    except (OSError, ValueError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    if args.action == "render":
        from service import kube_yaml_all
        sys.stdout.write(kube_yaml_all(render(users)))
    else:
        print(json.dumps(nas_vars(users), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
