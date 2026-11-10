#!/usr/bin/env bash
# Runs inside the pinned .NET SDK image (DOTNET_SDK_IMAGE). Builds SwarmUI at
# SWARMUI_COMMIT the way its launcher does (dotnet build into
# src/bin/live_release), self-contained for linux-x64, with the offline guard
# patch: SwarmUI must never pip-install into the image's ComfyUI environment.
# NuGet restore writes packages.lock.json, which the receipt records as the
# resolved .NET dependency lock. Outputs the source tree plus the build.
set -euo pipefail
: "${SWARMUI_REPOSITORY:?}" "${SWARMUI_COMMIT:?}"
run=/work/run
out=$run/out
src=$run/src/SwarmUI
export DOTNET_CLI_TELEMETRY_OPTOUT=1 DOTNET_NOLOGO=1 DOTNET_SKIP_FIRST_TIME_EXPERIENCE=1 NUGET_PACKAGES=$run/nuget
git config --global --add safe.directory '*'
[[ -d /work/src/swarmui/.git ]] || git clone --quiet "$SWARMUI_REPOSITORY" /work/src/swarmui
git -C /work/src/swarmui fetch --quiet origin
git -C /work/src/swarmui cat-file -e "$SWARMUI_COMMIT^{commit}"
rm -rf -- "$src" "$out/SwarmUI"
git clone --quiet --shared --no-checkout /work/src/swarmui "$src"
git -C "$src" checkout --quiet --detach "$SWARMUI_COMMIT"
for patch in /pipeline/swarmui/*.patch; do
  git -C "$src" apply --check "$patch"
  git -C "$src" apply "$patch"
  echo "APPLIED=${patch##*/}"
done
cd "$src"
dotnet --version
# Upstream's launcher: dotnet build src/SwarmUI.csproj ... -o ./src/bin/live_release.
# (dotnet publish flattens LLamaSharp's per-CPU native variants into one path.)
dotnet restore src/SwarmUI.csproj --runtime linux-x64 --use-lock-file -p:SelfContained=true
dotnet build src/SwarmUI.csproj --no-restore --configuration Release --runtime linux-x64 --self-contained true \
  -o src/bin/live_release
# The launcher rebuilds when src/bin/last_build differs from the git HEAD; the
# image has no .git, but record the commit the way upstream does.
printf '%s\n' "$SWARMUI_COMMIT" >src/bin/last_build
mkdir -p "$out/SwarmUI"
tar -C "$src" --exclude=./.git -cf - . | tar -C "$out/SwarmUI" -xf -
cp src/packages.lock.json "$out/packages.lock.json"
(cd "$out/SwarmUI/src/bin/live_release" && find . -type f -print0 | sort -z | xargs -0 sha256sum) >"$out/live_release.sha256"
sdk=$(dotnet --version)
lock_sha=$(sha256sum "$out/packages.lock.json" | cut -d' ' -f1)
build_sha=$(sha256sum "$out/live_release.sha256" | cut -d' ' -f1)
files=$(wc -l <"$out/live_release.sha256")
patches=$(for p in /pipeline/swarmui/*.patch; do printf '{"file": "%s", "sha256": "%s"}\n' "${p##*/}" "$(sha256sum "$p" | cut -d' ' -f1)"; done | paste -sd, -)
cat >"$out/RECEIPT.json" <<JSON
{
  "schema": 1,
  "stage": "swarmui",
  "swarmui_repository": "$SWARMUI_REPOSITORY",
  "swarmui_commit": "$SWARMUI_COMMIT",
  "dotnet_sdk_version": "$sdk",
  "runtime": "linux-x64",
  "self_contained": true,
  "patches": [$patches],
  "packages_lock_sha256": "$lock_sha",
  "live_release_manifest_sha256": "$build_sha",
  "live_release_files": $files
}
JSON
echo "SWARMUI_STAGE=built; sdk=$sdk; files=$files"
