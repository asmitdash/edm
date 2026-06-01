#!/usr/bin/env bash
# Deploy EDM as a docker-compose stack on this host.
# Usage: ./scripts/edm-deploy.sh [up|down|logs|rebuild]

set -euo pipefail
cd "$(dirname "$0")/.."

CMD="${1:-up}"
COMPOSE="docker compose -f docker-compose.bundle.yml"

case "$CMD" in
  up)
    if [ ! -f .env ]; then
      echo "ERROR: .env missing. Copy .env.example to .env and fill in keys." >&2
      exit 1
    fi
    $COMPOSE up -d --build
    echo
    echo "Waiting for /api/health..."
    for i in $(seq 1 60); do
      if curl -fsS http://127.0.0.1:8088/api/health > /dev/null 2>&1; then
        echo "EDM is up at http://127.0.0.1:8088/"
        echo "First-time use: open the URL — the setup wizard will guide you."
        exit 0
      fi
      sleep 1
    done
    echo "Timed out. See logs:"
    $COMPOSE logs --tail=80 app
    exit 1
    ;;
  down)
    $COMPOSE down
    ;;
  logs)
    $COMPOSE logs -f --tail=200
    ;;
  rebuild)
    $COMPOSE up -d --build --force-recreate
    ;;
  *)
    echo "Usage: $0 [up|down|logs|rebuild]"
    exit 2
    ;;
esac
