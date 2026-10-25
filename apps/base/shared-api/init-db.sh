#!/usr/bin/env bash
# Only the official PostgreSQL image's empty-datadir init invokes this script.
set -euo pipefail
# psql reads the protected password file internally; never put it in argv.
# The application owns only its database and cannot create roles/databases.
psql --username postgres --dbname postgres --no-psqlrc --set ON_ERROR_STOP=1 <<'SQL'
\set app_password `cat /run/postgres/litellm-password`
CREATE ROLE litellm LOGIN PASSWORD :'app_password' NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
CREATE DATABASE litellm OWNER litellm;
REVOKE ALL ON DATABASE litellm FROM PUBLIC;
\connect litellm
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE, CREATE ON SCHEMA public TO litellm;
SQL
