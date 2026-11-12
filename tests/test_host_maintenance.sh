#!/usr/bin/env bash
# Offline: no pacman, devices, rasdaemon, systemd or cluster access.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
role="$root/infrastructure/ansible/roles/host_maintenance"
PYTHONDONTWRITEBYTECODE=1 python3 "$root/tests/workstation/test_host_maintenance.py" 2>/dev/null
fail() {
  printf 'FAIL: %s\n' "$*" >&2
  exit 1
}
# Monitoring only: smartd never schedules self-tests and never mails.
smartd="$role/templates/smartd.conf.j2"
grep -Eq -- '-d nvme -H -l error -l selftest -W 0,' "$smartd" || fail 'smartd directives changed'
if grep -Ev '^#' "$smartd" | grep -Eq -- ' -s | -m | -M '; then fail 'smartd schedules self-tests or mail'; fi
# Package cache: keep at least three versions, never uninstalled-only (-u) pruning.
paccache="$role/templates/paccache.conf.j2"
grep -Fq 'ExecStart=/usr/bin/paccache -r -c /var/cache/pacman/pkg -k {{ lab_paccache_keep }} -i' "$paccache" || fail 'paccache arguments changed'
if grep -Eq -- 'paccache .* -u|--uninstalled|Nice=|IOSchedulingClass=' "$paccache"; then
  fail 'paccache drop-in prunes uninstalled packages or overrides the packaged priority'
fi
# The retention report has no deletion path.
program="$role/files/workstation_maintenance.py"
if grep -Eq 'rmtree|os\.remove|rmdir|podman (rmi|image rm|system prune)' "$program"; then
  fail 'maintenance program contains a deletion path'
fi
# The only removal is write_atomic's cleanup of its own temporary file.
[[ $(grep -c 'unlink(' "$program") == 1 && $(grep -c 'Path(temporary).unlink(missing_ok=True)' "$program") == 1 ]] ||
  fail 'unexpected file removal in the program'
# Every maintenance unit records its result through the root job-result hook.
for unit in "$role/files/workstation-advisories.service" "$role/templates/workstation-hardware-health.service.j2" \
  "$role/templates/workstation-retention-report.service.j2" "$role/templates/paccache.conf.j2" "$role/files/job-result.conf"; do
  grep -Eq '^ExecStopPost=\+/usr/bin/python3 -I /usr/local/lib/workstation-maintenance/workstation_maintenance.py job-result ' "$unit" ||
    fail "$unit does not record its result"
done
grep -Fq 'ProtectHome=read-only' "$role/templates/workstation-retention-report.service.j2" || fail 'retention report can write home'
grep -Fq 'DynamicUser=yes' "$role/files/workstation-advisories.service" || fail 'advisory download runs privileged'
# Host Alloy: the three maintenance journal sources are narrow and templated.
alloy="$root/infrastructure/observability/host/config.alloy"
for identifier in workstation-maintenance smartd rasdaemon; do
  grep -Fq "matches = \"SYSLOG_IDENTIFIER=$identifier\"" "$alloy" || fail "missing $identifier journal source"
done
[[ $(grep -c 'drop_counter_reason = "routine_\(smartd\|rasdaemon\)_line"' "$alloy") == 2 ]] || fail 'unclassified lines are not dropped'
# Kernel plus three maintenance pipelines, each templating and outputting safe_message.
[[ $(grep -c 'source = "safe_message"' "$alloy") == 8 ]] || fail 'raw maintenance messages are forwarded'
echo 'Host maintenance reports, units and journal filters passed (offline fixtures; no host qualification)'
