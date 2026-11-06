#!/usr/bin/env bash
# Demonstrate migration, synthetic KIM imports and the end-to-end API tests
# without Docker or an external network. Dependencies must already be installed.
set -euo pipefail

cd "$(dirname "$0")/.."
python_bin="${PYTHON_BIN:-.venv/bin/python}"
alembic_bin="${ALEMBIC_BIN:-.venv/bin/alembic}"
temporary_dir="$(mktemp -d -t oge-demo.XXXXXX)"
trap 'rm -rf "$temporary_dir"' EXIT

export DATABASE_URL="sqlite+aiosqlite:///$temporary_dir/demo.db"
"$alembic_bin" upgrade head
"$python_bin" -m scripts.seed
"$python_bin" -m scripts.check_openapi
"$python_bin" -m pytest -q \
  tests/test_auth.py tests/test_kim.py tests/test_session.py \
  tests/test_review_results.py tests/test_ops_openapi.py
echo "Offline demonstration PASS"
