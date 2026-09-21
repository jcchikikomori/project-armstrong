#!/usr/bin/env bash
#
# Export every MediKeep table to JSON for downstream LLM processing.
#
# Bind-mounting the Postgres data directory gives you binary heap files, not
# records. The only readable export path is through psql, which is what this
# does. It reads the table list out of information_schema rather than hardcoding
# it, so it survives MediKeep's alembic migrations.
#
# Output: <stack>/data/exports/<UTC timestamp>/
#   tables/<table>.json  one JSON array per table
#   dataset.json         everything in one object, for feeding to a model
#   manifest.json        row counts, so a truncated export is obvious
# and a `latest` symlink pointing at the newest run.

set -euo pipefail

STACK_DIR="${STACK_DIR:-/opt/medikeep}"
ENV_FILE="$STACK_DIR/.env"
COMPOSE_FILE="$STACK_DIR/docker-compose.yml"
EXPORT_DIR="$STACK_DIR/data/exports"

die() {
	echo "export-json: $*" >&2
	exit 1
}

[ -f "$ENV_FILE" ] || die "no .env at $ENV_FILE"
[ -f "$COMPOSE_FILE" ] || die "no docker-compose.yml at $COMPOSE_FILE"

# shellcheck disable=SC1090
set -a
. "$ENV_FILE"
set +a

DB_NAME="${DB_NAME:-medical_records}"
DB_USER="${DB_USER:-medapp}"
RETENTION_DAYS="${EXPORT_RETENTION_DAYS:-14}"
PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

compose() {
	docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" "$@"
}

# -At: unaligned, tuples only. Every query here returns a single scalar.
psql_scalar() {
	compose exec -T postgres \
		psql -U "$DB_USER" -d "$DB_NAME" -At -v ON_ERROR_STOP=1 -c "$1"
}

compose ps --status running --services 2>/dev/null | grep -qx postgres ||
	die "postgres container is not running"

TS="$(date -u +%Y-%m-%dT%H-%M-%SZ)"
ISO="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
RUN_DIR="$EXPORT_DIR/$TS"
mkdir -p "$RUN_DIR/tables"
chmod 700 "$RUN_DIR"

mapfile -t TABLES < <(psql_scalar "
	SELECT table_name
	FROM information_schema.tables
	WHERE table_schema = 'public'
	  AND table_type = 'BASE TABLE'
	  AND table_name <> 'alembic_version'
	ORDER BY table_name;
")

[ "${#TABLES[@]}" -gt 0 ] || die "no tables found in schema public -- has the app finished migrating?"

echo "export-json: $ISO -- ${#TABLES[@]} tables -> $RUN_DIR"

declare -A ROW_COUNTS
for tbl in "${TABLES[@]}"; do
	# json_agg over zero rows is NULL, hence the coalesce to an empty array.
	psql_scalar "SELECT coalesce(json_agg(t), '[]'::json) FROM public.\"$tbl\" t;" \
		>"$RUN_DIR/tables/$tbl.json"
	ROW_COUNTS["$tbl"]="$(psql_scalar "SELECT count(*) FROM public.\"$tbl\";")"
	printf '  %-40s %s rows\n' "$tbl" "${ROW_COUNTS[$tbl]}"
done

# Assembled by hand rather than with jq: the per-table files are already valid
# JSON documents, so concatenating them needs no extra dependency.
{
	printf '{\n  "exported_at": "%s",\n  "database": "%s",\n  "tables": {\n' "$ISO" "$DB_NAME"
	first=1
	for tbl in "${TABLES[@]}"; do
		[ "$first" -eq 1 ] || printf ',\n'
		first=0
		printf '    "%s": ' "$tbl"
		cat "$RUN_DIR/tables/$tbl.json"
	done
	printf '\n  }\n}\n'
} >"$RUN_DIR/dataset.json"

{
	printf '{\n  "exported_at": "%s",\n  "database": "%s",\n  "row_counts": {\n' "$ISO" "$DB_NAME"
	first=1
	for tbl in "${TABLES[@]}"; do
		[ "$first" -eq 1 ] || printf ',\n'
		first=0
		printf '    "%s": %s' "$tbl" "${ROW_COUNTS[$tbl]}"
	done
	printf '\n  }\n}\n'
} >"$RUN_DIR/manifest.json"

if command -v jq >/dev/null 2>&1; then
	jq empty "$RUN_DIR/dataset.json" || die "dataset.json failed JSON validation"
fi

# These are medical records in plaintext. Do not leave them world-readable.
chmod 600 "$RUN_DIR"/*.json "$RUN_DIR"/tables/*.json
chown -R "$PUID:$PGID" "$RUN_DIR" 2>/dev/null || true

ln -sfn "$RUN_DIR" "$EXPORT_DIR/latest"

# Prune old runs. -maxdepth 1 keeps this from ever descending into a run dir.
find "$EXPORT_DIR" -mindepth 1 -maxdepth 1 -type d -mtime "+$RETENTION_DAYS" -exec rm -rf {} + 2>/dev/null || true

echo "export-json: done -> $RUN_DIR/dataset.json (latest -> $EXPORT_DIR/latest)"
