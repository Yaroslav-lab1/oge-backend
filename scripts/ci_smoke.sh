#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p work
export SEED_PASSWORD="${SEED_PASSWORD:-CI-synthetic-password-12345}"
docker compose up --build -d --wait --wait-timeout 180
docker compose exec -T -e SEED_PASSWORD api python -m scripts.http_smoke prepare > work/fault-state.json
docker compose restart api
for attempt in $(seq 1 60); do
  if curl -fsS http://127.0.0.1:8000/api/v1/health/ready >/dev/null; then
    break
  fi
  sleep 1
done
docker compose exec -T -e SEED_PASSWORD api python -m scripts.http_smoke verify < work/fault-state.json
bash scripts/backup_restore_test.sh
