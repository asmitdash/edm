#!/usr/bin/env bash
# One-shot launcher for the WSL/Linux dev workflow.
# Starts Postgres (if installed locally), applies migrations, seeds demo data,
# runs the pipeline against whichever providers .env selects, and starts the UI.

set -euo pipefail

cd "$(dirname "$0")/.."

if [ ! -d .venv ]; then
  echo "Creating venv..."
  python3 -m venv .venv
  .venv/bin/pip install -e .
fi

if command -v service >/dev/null 2>&1; then
  if ! pgrep -x postgres >/dev/null; then
    echo "Starting Postgres (sudo)..."
    sudo service postgresql start
  fi
fi

echo "Applying migration..."
.venv/bin/edm db init || echo "(migration may already be applied)"

if [ "${EDM_SEED_DEMO:-1}" = "1" ]; then
  echo "Seeding demo data..."
  .venv/bin/python scripts/seed_demo.py
fi

if [ "${EDM_RUN_PIPELINE:-1}" = "1" ]; then
  echo "Running pipeline..."
  .venv/bin/edm process || true
fi

echo
echo "EDM is ready."
echo "  UI:        http://${EDM_HOST:-127.0.0.1}:${EDM_PORT:-8088}/"
echo "  Login:     ${EDM_ADMIN_USER:-admin} / ${EDM_ADMIN_PASSWORD:-edm-admin}"
echo "  Findings:  http://${EDM_HOST:-127.0.0.1}:${EDM_PORT:-8088}/findings"
echo "  Graph:     http://${EDM_HOST:-127.0.0.1}:${EDM_PORT:-8088}/graph"
echo
exec .venv/bin/edm serve
