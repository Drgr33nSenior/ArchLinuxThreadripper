# Owner tools

Scripts the owner runs by hand for rollouts and live tests: the workstation's runtime
rollout, sandbox go-live, memory and soak measurements, and the elastic KV trial. They run
from a shipped checkout of this repository and read site values (addresses, SSH
destinations, local paths) from outside it. Every script prints its usage with `--help`.

These tools act on the live workstation, cluster or NAS where the table says so. Tests and
CI never run them against a live system; `tests/test_tools.sh` checks them offline.

## The tools

| Tool | Runs on | What it does | Live changes |
| --- | --- | --- | --- |
| [`rollout/install-runtime.sh`](rollout/install-runtime.sh) `SHA12` | Workstation | Builds and installs the runtime packages from a shipped tree; checks the installed `sandbox.py` matches it | Host packages |
| [`rollout/rollout-main.sh`](rollout/rollout-main.sh) `SHA12` | Workstation | Runtime package, plan-only family render, resource and SGLang checks against live ([`rollout_check.py`](rollout/rollout_check.py)), `workstationctl mode family --execute`, post-rollout checks | Host packages, AI service |
| [`rollout/key-limits.sh`](rollout/key-limits.sh) `SHA12 TPM` | Workstation | Sets tokens-a-minute on the gateway keys named in `GATEWAY_KEY_ALIASES` with `service limits`; lists every key's stored limits | Gateway key limits |
| [`measure/comfy-mem.sh`](measure/comfy-mem.sh) | Workstation | Both ComfyUI pods' cgroup memory through a cold and a warm pair of pictures ([`comfy_mem.py`](measure/comfy_mem.py)) | Pictures only |
| [`measure/comfy-ab.sh`](measure/comfy-ab.sh) | Workstation | A/B of the ComfyUI memory limits (current vs `B_MEMORY`, default 9Gi), pictures and an optional clip per arm; restores the limits on exit ([`comfy_summary.py`](measure/comfy_summary.py)) | ComfyUI limits, restored |
| [`measure/soak.sh`](measure/soak.sh) `[ROUNDS] [INTERVAL]` | Workstation | Picture-driven elastic KV cycles with tail state, VRAM, faults and restarts | Pictures only |
| [`measure/soak_picture.py`](measure/soak_picture.py) | service-tools pod | One picture through the live endpoint; used by the three scripts above | Pictures only |
| [`measure/parallel_tools_probe.py`](measure/parallel_tools_probe.py) `URL [RUNS]` | Anywhere SGLang answers | Streams a three-tool-call reply and counts text inside open tool arguments | None |
| [`sandbox/make-sandbox-config.sh`](sandbox/make-sandbox-config.sh) `SOURCE_DIR` | Workstation | Writes `~/ai-stack/private/sandbox.json` and `sandbox-users.json` from Open WebUI's accounts after review ([`sandbox_config_helper.py`](sandbox/sandbox_config_helper.py)) | None (private files) |
| [`sandbox/nas-homes.sh`](sandbox/nas-homes.sh) `NAS_COMMIT WORKSTATION_COMMIT` | Owner's Mac | Sandbox home datasets and exports on the NAS: dry run, review, apply | NAS datasets, exports |
| [`sandbox/sandbox-golive.sh`](sandbox/sandbox-golive.sh) `SHA12` | Workstation | Sandbox go-live with preflight, capacity, allowlisted diffs and one confirmation ([`golive_check.py`](sandbox/golive_check.py), [`kdiff.sh`](sandbox/kdiff.sh), [`nfs4_probe.py`](sandbox/nfs4_probe.py)); `--plan-only` and `--probe-only` | Sandbox bundle, homes, Open WebUI |
| [`sandbox/validate-on-workstation.sh`](sandbox/validate-on-workstation.sh) `SHA12` | Workstation | The go-live render, checks and server-side diffs with a fake config in a scratch directory | None |
| [`sandbox/home-test.sh`](sandbox/home-test.sh) `HOME_NAME` | Workstation | Persistence and ownership of one NAS-backed home through a short-lived pod; lists its snapshots | One test pod, files in the home |
| [`kv-trial/collect.sh`](kv-trial/collect.sh) | Workstation, user timer | Appends SGLang's elastic KV tail lines to `~/ai-stack/kv-trial/events.log` | None |
| [`kv-trial/kv_trial_report.py`](kv-trial/kv_trial_report.py) | Anywhere Prometheus answers | Hand-backs against SGLang's cache counters | None |

Each script keeps its safety checks: the resource, image and SGLang environment diff before
an apply, idle checks before live changes (pictures and video in every tool that touches the
AI service; deep research too in `rollout-main.sh` and `sandbox-golive.sh`, whose changes
restart service-tools or Open WebUI), a `STOP: ...` line and exit 1 on any failed check, and
a confirmation before live changes where the original asked.

`rollout-main.sh` allows a reviewed sizing change only as named: with
`EXPECT_CHANGED="deployment=container:memory"` the rendered Deployment must equal live except
that container's memory request and limit, both set to the value given. Any other change (CPU,
other resources, other containers or init containers) stops the rollout.

`SHA12` names a shipped tree: `$WORKSTATION_SRC_DIR/archlinuxthreadripper-SHA12`
(default `~/src`). Output stays where it was: `~/build/workstation-runtime-SHA12/` for
rollouts and `~/ai-stack/` for measurements, go-live logs and the KV trial.

## Site values

Values that identify the site are never in this repository. A tool reads each value from
the environment first, then from the site file:

```text
${WORKSTATION_SITE_ENV:-${XDG_CONFIG_HOME:-$HOME/.config}/workstation/site.env}
```

The file holds `KEY=VALUE` lines and `#` comments. It is parsed, never run as shell
([`lib/common.sh`](../lib/common.sh) `common::load_config`), and an unknown or repeated key
stops the tool. Values are literal: write `NAME=value`, not `NAME="value"`, because quotes
become part of the value. The file must be a regular file owned by you and writable by nobody
else (`chmod 600`): its values choose scripts and package trees that the tools run, some
with `sudo`. A tool that needs a missing value stops with
`STOP: set NAME in the environment or in <site file>`. SSH destinations must be plain
`[user@]host` and dataset names plain ZFS names; anything else stops the tool.

| Name | Used by | Value |
| --- | --- | --- |
| `WORKSTATION_SRC_DIR` | Every tool that takes `SHA12` | Directory of shipped trees (default `~/src`) |
| `NAS_ADDRESS` | `sandbox-golive.sh`, `validate-on-workstation.sh` | The NAS address the node mounts homes from; the network probe also checks workspaces cannot reach it |
| `WORKSTATION_LAN_ADDRESS` | `sandbox-golive.sh`, `validate-on-workstation.sh` | The workstation's LAN address; the network probe checks workspaces cannot reach it |
| `GATEWAY_KEY_ALIASES` | `key-limits.sh` | Gateway key aliases to change, separated by spaces |
| `PROMETHEUS_URL` | `kv_trial_report.py` | Prometheus base URL (or pass `--prometheus`) |
| `CHAT_PROBE` | `rollout-main.sh`, `soak.sh` (optional) | Path of a chat probe script taking `URL COUNT` and printing JSON with `decode_tok_per_s`; unset skips the chat check. No probe ships in this repository: point it at the owner's existing `chat_probe.py` |
| `NAS_SSH` | `home-test.sh` (optional) | SSH destination on the NAS with passwordless `sudo -n zfs`; unset skips the snapshot listing |
| `NAS_SANDBOX_DATASET` | `home-test.sh` (optional), `nas-homes.sh` | Parent ZFS dataset of the sandbox homes, as the NAS repository creates it |
| `WORKSTATION_SSH` | `nas-homes.sh` | SSH destination of the workstation from the Mac |
| `NAS_REPO` | `nas-homes.sh` | Local clone of the NAS repository |
| `NAS_COMMISSIONING_DIR` | `nas-homes.sh` | Directory with `controller.inventory.ini` and `controller.config.yml` |
| `NAS_OWNER_PROFILE` | `nas-homes.sh` | The latest owner profile JSON |
| `NAS_ANSIBLE_VENV` | `nas-homes.sh` | Ansible virtual environment (default `~/.venvs/nas-ansible`) |

Run-time choices stay environment variables of the run, not site values:
`EXPECT_CHANGED` and `EXPECT_SGLANG_ENV` (`rollout-main.sh`), `EXPECT_ENV` and
`TELEMETRY_RENDER` (`sandbox-golive.sh`), `SKIP_CLIP`, `ARMS` and `B_MEMORY`
(`comfy-ab.sh`), `KV_TRIAL_SINCE` (`collect.sh`), `SANDBOX_IMAGE` and
`SANDBOX_EGRESS_IMAGE` (`make-sandbox-config.sh` digest pins) and `WORKSTATION_REPO`
(`nas-homes.sh`, default: the clone holding the script).

Example, on the workstation (placeholders; use your own values):

```sh
install -d -m 700 ~/.config/workstation
cat >~/.config/workstation/site.env <<'EOF'
NAS_ADDRESS=192.0.2.10
WORKSTATION_LAN_ADDRESS=198.51.100.40
GATEWAY_KEY_ALIASES=browser ide
PROMETHEUS_URL=https://prometheus.example.invalid
EOF
chmod 600 ~/.config/workstation/site.env
```

## Switching from the workstation copies

The owner's earlier copies under `~/ai-stack/` (the rollout and measurement scripts,
`~/ai-stack/sandbox/`, `~/ai-stack/kv-trial/`) are replaced by the versions in a shipped
tree. Results, logs and `events.log` stay where they are.

1. Ship merged main to the workstation as usual, as a full tree at
   `~/src/archlinuxthreadripper-SHA12` (the tools need `tools/` and `lib/common.sh`).
2. Write the site file above with the values the old copies had in their source.
3. Point a stable link at the tools of the tree you run, and move it after each ship:

   ```sh
   ln -sfn ~/src/archlinuxthreadripper-SHA12/tools ~/.local/share/workstation-tools
   ```

4. Call the tools through the link with the new arguments. The changes:
   - `sandbox-golive.sh`, `validate-on-workstation.sh` and `make-sandbox-config.sh` no
     longer default to a fixed commit: pass `SHA12` (or the tree for
     `make-sandbox-config.sh`). The sandbox images come from that tree's `versions.lock`.
   - `key-limits.sh SHA12 TPM` takes the limit as an argument and the aliases from
     `GATEWAY_KEY_ALIASES`.
   - `nas-homes.sh NAS_COMMIT WORKSTATION_COMMIT` runs from the Mac clone of this
     repository, with a site file on the Mac. It fetches both clones and stops unless
     each commit is on `origin/main`.
   - `rollout-main.sh` stops when the render changes any SGLang environment variable;
     list reviewed ones in `EXPECT_SGLANG_ENV`. It also stops while a deep-research run
     is active. `EXPECT_CHANGED` now allows only the named container's memory.
   - `kv_trial_report.py` reads `--since` from the first logged event unless given.
5. Elastic KV trial timer: install the units, which run `collect.sh` through the link.

   ```sh
   install -m 644 ~/.local/share/workstation-tools/kv-trial/kv-trial-collect.{service,timer} \
     ~/.config/systemd/user/
   systemctl --user daemon-reload
   systemctl --user restart kv-trial-collect.timer
   systemctl --user list-timers kv-trial-collect.timer
   ```

6. Keep the old copies until each replacement has run once, then retire them, except
   `chat_probe.py`: it is not in this repository, so keep the owner's copy and set
   `CHAT_PROBE` to its path to keep the chat check in `rollout-main.sh` and `soak.sh`.

## Tests

`bash tests/test_tools.sh` (with `HOME_LAB_PYTHON` set, as for `make test`) checks every
script's usage, the site-value loader, the Python helpers' unit tests in `tests/tools/` and
`make-sandbox-config.sh` end to end against a fake database and a read-only fake
`kubectl`. `make shellcheck shfmt syntax` cover the scripts with the rest of the repository.
