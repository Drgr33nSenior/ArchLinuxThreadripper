#!/usr/bin/env bash
set -euo pipefail
[[ $# == 1 && $1 =~ ^[a-f0-9]{64}$ ]]
while ! timeout 5s docker exec --user 1000:1000 "$1" curl --silent --max-time 3 --fail http://127.0.0.1:8080/health >/dev/null 2>&1; do
  [[ $(timeout 5s docker inspect --format '{{.State.Status}}' "$1") == running ]] || exit 1
  sleep 2
done
