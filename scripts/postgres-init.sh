#!/usr/bin/env bash
# Mounted into pgvector image's /docker-entrypoint-initdb.d/
# Runs once on first init of the data volume. Idempotent (uses IF NOT EXISTS).
set -e
psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" <<'SQL'
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS vector;
SQL
