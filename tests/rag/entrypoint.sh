#!/usr/bin/env bash
set -euo pipefail
umask 077
# Fixture-local key survives the tested same-volume restart; never print it.
secret=/app/backend/data/.rag-smoke-secret
if [[ ! -e $secret ]]; then
  openssl rand -hex 32 >"$secret"
fi
[[ -f $secret && ! -L $secret ]]
export WEBUI_SECRET_KEY
WEBUI_SECRET_KEY=$(cat "$secret")
cd /app/backend
exec bash start.sh
