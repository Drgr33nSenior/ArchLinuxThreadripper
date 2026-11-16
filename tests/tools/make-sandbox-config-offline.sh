#!/usr/bin/env bash
# Offline test of tools/sandbox/make-sandbox-config.sh: fake Open WebUI database, fake read-only
# kubectl, scratch private directory. Usage: make-sandbox-config-offline.sh [SOURCE_DIR]
# (default: this checkout). Run by tests/test_tools.sh.
set -euo pipefail
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
src=$(cd -- "${1:-$here/../..}" && pwd -P)
tool=$src/tools/sandbox/make-sandbox-config.sh
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
fail() {
  printf 'FAIL: %s\n' "$*" >&2
  exit 1
}
mkdir -m 700 "$work/private"
python3 -I "$here/make-fake-db.py" "$work/webui.db"
export KUBECTL=$here/fake-kubectl.sh SANDBOX_PRIVATE_DIR=$work/private SANDBOX_WEBUI_DB=$work/webui.db

# 0. A source tree is required.
if out=$(bash "$tool" </dev/null 2>&1); then fail 'ran without a source tree'; fi
grep -q 'usage: make-sandbox-config.sh' <<<"$out" || fail "no usage line: $out"

# 1. The proposal: run without a terminal and no review file -> refuses after printing.
if out=$(bash "$tool" "$src" </dev/null 2>&1); then fail 'ran without a review'; fi
grep -q 'no terminal for the review' <<<"$out" || fail "unexpected refusal: $out"
grep -q '6 accounts (1 admin, 1 pending, 4 user)' <<<"$out" || fail "counts missing: $out"
! grep -q '@example' <<<"$out" || fail 'an email reached the terminal'
! grep -q 'aaaaaaaa-' <<<"$out" || fail 'an account id reached the terminal'
! grep -q 'gggggggg-' <<<"$out" || fail 'a group id reached the terminal'
printf '%s\n' "$out" | sed -n '/Proposed homes/,$p'

# 2. A review that names a group the account lacks is refused; nothing written.
cat >"$work/bad.tsv" <<'EOF'
1 yes admin-person Admin 20G
2 yes parent-one Parents 20G
3 yes kid-two Parents 20G
4 no guest-person - 20G
5 no kid-two-2 Family 20G
6 no user-9-lives-unicode - 20G
EOF
if bash "$tool" --review "$work/bad.tsv" "$src" >"$work/bad.log" 2>&1; then fail 'accepted Parents for a Family-only account'; fi
grep -q 'not in Parents' "$work/bad.log" || fail "wrong refusal: $(cat "$work/bad.log")"
[[ -z $(ls "$work/private") ]] || fail 'files written after a refused review'

# 3. A good review writes both files 0600 and they load with the repository code.
cat >"$work/good.tsv" <<'EOF'
# edited by hand
1	yes	admin-person	Admin	20G   # comment kept
2  yes  parent-one  Parents  30G
3 yes kid-two Family 20G
4 no guest-person - 20G
5 yes kid-two-2 Family 5G
6 no user-9-lives-unicode - 20G
EOF
bash "$tool" --review "$work/good.tsv" "$src" >"$work/good.log" 2>&1 || fail "good review refused: $(cat "$work/good.log")"
! grep -q -e '@example' -e 'aaaaaaaa-' -e 'gggggggg-' "$work/good.log" || fail 'an id or email reached the terminal'
for f in sandbox.json sandbox-users.json; do
  [[ $(stat -c '%a' "$work/private/$f" 2>/dev/null || stat -f '%Lp' "$work/private/$f") == 600 ]] || fail "$f is not 0600"
done
python3 - "$src/lib/workstation" "$work/private/sandbox.json" <<'PY' || fail 'repository loader rejected the output'
import json, sys
sys.path.insert(0, sys.argv[1])
import sandbox
c = sandbox.read_config(sys.argv[2])
raw = json.load(open(sys.argv[2]))
assert set(raw) == {"image", "egress_image", "parents_group_id", "family_group_id", "family_enabled", "group_resolution",
                    "egress_service_ips", "api_server_cidr", "api_server_port", "homes", "family_dns"}, raw.keys()
assert raw["egress_service_ips"] == {"family": "10.43.0.201", "parents": "10.43.0.202"}, raw["egress_service_ips"]
assert raw["api_server_cidr"] == "192.0.2.11/32" and raw["api_server_port"] == 6443
lock = dict(line.split("=", 1) for line in open(sys.argv[1] + "/../../versions.lock").read().splitlines()
            if "=" in line and not line.startswith("#"))
assert raw["image"] == lock["ASSISTANT_SANDBOX_CANDIDATE_IMAGE"] and raw["egress_image"] == lock["SANDBOX_EGRESS_CANDIDATE_IMAGE"]
assert raw["family_enabled"] is True and raw["group_resolution"] == "most-permissive"
assert raw["parents_group_id"].endswith("p") and raw["family_group_id"].endswith("f")
assert raw["family_dns"] == ["1.1.1.3", "1.0.0.3"]
names = {u["name"]: (u["group"], u["quota"]) for u in c["users"]}
assert names == {"admin-person": ("Admin", "20G"), "parent-one": ("Parents", "30G"), "kid-two": ("Family", "20G"),
                 "kid-two-2": ("Family", "5G")}, names
objects = sandbox.render(c, None)
print("render:", len(objects), "objects;", sorted({o["kind"] for o in objects}))
PY
echo "written: $(grep -c . "$work/good.log") log lines"

# 4. Existing files are kept without --force; --force backs them up.
if bash "$tool" --review "$work/good.tsv" "$src" >"$work/again.log" 2>&1; then fail 'overwrote without --force'; fi
grep -q 'rerun with --force' "$work/again.log" || fail 'no --force hint'
bash "$tool" --force --review "$work/good.tsv" "$src" >"$work/force.log" 2>&1 || fail "--force failed: $(cat "$work/force.log")"
backups=("$work"/private/*.bak-*)
((${#backups[@]} == 2)) || fail 'no backups after --force'

# 5. A taken preferred address moves to the next free one in the static band.
rm -f "$work/private"/sandbox*.json*
FAKE_TAKEN=10.43.0.201 bash "$tool" --review "$work/good.tsv" "$src" >"$work/taken.log" 2>&1 || fail "taken address: $(cat "$work/taken.log")"
python3 -c 'import json,sys; v=json.load(open(sys.argv[1]))["egress_service_ips"]; assert v=={"family":"10.43.0.202","parents":"10.43.0.255"}, v' "$work/private/sandbox.json" ||
  fail 'wrong fallback addresses'

# 6. An image pin that differs from versions.lock is refused before anything is read.
rm -f "$work/private"/sandbox*.json*
if SANDBOX_IMAGE=example.invalid/other@sha256:0000 bash "$tool" --review "$work/good.tsv" "$src" >"$work/pin.log" 2>&1; then
  fail 'accepted a sandbox image pin that differs from versions.lock'
fi
grep -q 'not the one versions.lock records' "$work/pin.log" || fail "wrong refusal: $(cat "$work/pin.log")"
[[ -z $(ls "$work/private") ]] || fail 'files written after a refused pin'

# 7. A versions.lock with a repeated or missing image pin is refused.
mkdir -p "$work/tree/lib"
ln -s "$src/lib/workstation" "$work/tree/lib/workstation"
for case in repeated missing; do
  if [[ $case == repeated ]]; then
    { cat "$src/versions.lock" && grep '^ASSISTANT_SANDBOX_CANDIDATE_IMAGE=' "$src/versions.lock"; } >"$work/tree/versions.lock"
  else
    grep -v '^SANDBOX_EGRESS_CANDIDATE_IMAGE=' "$src/versions.lock" >"$work/tree/versions.lock"
  fi
  if bash "$tool" --review "$work/good.tsv" "$work/tree" >"$work/lock.log" 2>&1; then fail "accepted a $case image pin"; fi
  grep -q 'Missing or duplicate lock value' "$work/lock.log" || fail "$case pin: $(cat "$work/lock.log")"
  [[ -z $(ls "$work/private") ]] || fail "files written after a $case pin"
done
echo 'make-sandbox-config offline test: PASS'
