#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
backup_file="$(mktemp -t oge-backup.XXXXXX)"
restore_db="oge_restore_$$"
created=0
started_at="$(date +%s)"

cleanup() {
  if [[ "$created" == "1" ]]; then
    docker compose exec -T -e RESTORE_DB="$restore_db" db sh -ec \
      'dropdb --if-exists -U "$POSTGRES_USER" "$RESTORE_DB"' >/dev/null || true
  fi
  rm -f "$backup_file"
}
trap cleanup EXIT

docker compose exec -T db sh -ec \
  'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > "$backup_file"
test -s "$backup_file"

docker compose exec -T -e RESTORE_DB="$restore_db" db sh -ec \
  'createdb -U "$POSTGRES_USER" "$RESTORE_DB"'
created=1
docker compose exec -T -e RESTORE_DB="$restore_db" db sh -ec \
  'pg_restore --exit-on-error --single-transaction --no-owner -U "$POSTGRES_USER" -d "$RESTORE_DB"' \
  < "$backup_file"

count_query="SELECT json_build_array((SELECT count(*) FROM users),
  (SELECT count(*) FROM exams), (SELECT count(*) FROM exam_assignments),
  (SELECT count(*) FROM kim_package_versions), (SELECT count(*) FROM result_versions));"
original="$(docker compose exec -T -e COUNT_QUERY="$count_query" db sh -ec \
  'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At -c "$COUNT_QUERY"')"
restored="$(docker compose exec -T -e RESTORE_DB="$restore_db" -e COUNT_QUERY="$count_query" db sh -ec \
  'psql -U "$POSTGRES_USER" -d "$RESTORE_DB" -At -c "$COUNT_QUERY"')"

if [[ "$original" != "$restored" ]]; then
  echo "Backup/restore mismatch: original=$original restored=$restored" >&2
  exit 1
fi
elapsed="$(( $(date +%s) - started_at ))"
echo "Backup/restore PASS: counts=$restored elapsed=${elapsed}s"
