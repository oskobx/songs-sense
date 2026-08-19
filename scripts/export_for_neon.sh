#!/usr/bin/env bash
#
# Dump the retrieval tables for restore into Neon, and report the size so it can
# be checked against the hosting plan's storage limit before you upload anything.
#
# By default the dump EXCLUDES `embedding_multi` (bge-m3, 1024d) and its HNSW
# index: production is English-only, that column plus its index is over half the
# restored size, and stripping it before upload beats restoring it and dropping
# it afterwards — a free-tier database can run out of space mid-restore.
#
# The strip is done on a throwaway TEMPLATE copy of the database, so the local
# database is never modified and every other index, constraint and trigger
# survives untouched.
#
# Use --with-multi for a complete backup of both embedding columns.
#
# pg_dump runs inside the Postgres container: there is no local pg_dump on this
# machine, and the server's own binary can never be too old for the server.
#
# Usage:
#   scripts/export_for_neon.sh                 # stripped, custom format (default)
#   scripts/export_for_neon.sh --with-multi    # both embedding columns
#   scripts/export_for_neon.sh --plain         # plain SQL instead of custom
#   OUT_DIR=/tmp/dump scripts/export_for_neon.sh
#
# Env:
#   PG_CONTAINER  Postgres container name        (default: songs-sense-db)
#   OUT_DIR       where to write the dump        (default: data/neon)
#   STORAGE_LIMIT limit to compare against, MB   (default: 512, Neon free tier)
#   DATABASE_URL  only used to read the db name  (default: from .env)

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

PG_CONTAINER="${PG_CONTAINER:-songs-sense-db}"
OUT_DIR="${OUT_DIR:-data/neon}"
STORAGE_LIMIT_BYTES=$(( ${STORAGE_LIMIT:-512} * 1024 * 1024 ))
EXPORT_DB="songs_sense_export"

FORMAT="custom"
WITH_MULTI="no"
for arg in "$@"; do
  case "$arg" in
    --plain)      FORMAT="plain" ;;
    --with-multi) WITH_MULTI="yes" ;;
    *) echo "unknown argument: $arg (expected --plain and/or --with-multi)" >&2; exit 2 ;;
  esac
done

# --- database name -----------------------------------------------------------
if [[ -z "${DATABASE_URL:-}" && -f .env ]]; then
  DATABASE_URL="$(grep -E '^DATABASE_URL=' .env | head -1 | cut -d= -f2-)"
fi
if [[ -z "${DATABASE_URL:-}" ]]; then
  echo "DATABASE_URL is not set and .env has no entry for it" >&2
  exit 1
fi
DB_NAME="${DATABASE_URL##*/}"; DB_NAME="${DB_NAME%%\?*}"
DB_USER="${DATABASE_URL#*://}"; DB_USER="${DB_USER%%:*}"

if ! docker ps --format '{{.Names}}' | grep -qx "$PG_CONTAINER"; then
  echo "Postgres container '$PG_CONTAINER' is not running." >&2
  echo "Start it with: docker compose up -d" >&2
  exit 1
fi

psql_in() { docker exec -i "$PG_CONTAINER" psql -U "$DB_USER" "$@"; }

mkdir -p "$OUT_DIR"
EXT_FILE="$OUT_DIR/01_extension.sql"
SUFFIX=""; [[ "$WITH_MULTI" == "no" ]] && SUFFIX="_nomulti"
if [[ "$FORMAT" == "custom" ]]; then
  DUMP_FILE="$OUT_DIR/02_songs_passages${SUFFIX}.dump"
else
  DUMP_FILE="$OUT_DIR/02_songs_passages${SUFFIX}.sql"
fi

echo "Source: $DB_NAME (container $PG_CONTAINER)"
psql_in -d "$DB_NAME" -tAc "
  SELECT '  rows: songs=' || (SELECT count(*) FROM songs)
      || ', passages=' || (SELECT count(*) FROM passages);"

# --- choose the source database ----------------------------------------------
SOURCE_DB="$DB_NAME"
if [[ "$WITH_MULTI" == "no" ]]; then
  echo
  echo "Building a stripped copy ($EXPORT_DB) without embedding_multi..."
  # TEMPLATE copy is a file-level clone: fast, and it carries every index,
  # constraint and trigger, so the dump needs no manual reconstruction later.
  psql_in -d postgres -q -v ON_ERROR_STOP=1 <<SQL
SELECT pg_terminate_backend(pid) FROM pg_stat_activity
 WHERE datname = '$DB_NAME' AND pid <> pg_backend_pid();
DROP DATABASE IF EXISTS $EXPORT_DB;
CREATE DATABASE $EXPORT_DB TEMPLATE $DB_NAME;
SQL
  psql_in -d "$EXPORT_DB" -q -v ON_ERROR_STOP=1 <<'SQL'
DROP INDEX IF EXISTS passages_embedding_multi_hnsw_idx;
ALTER TABLE passages DROP COLUMN IF EXISTS embedding_multi;
SQL
  psql_in -d "$EXPORT_DB" -tAc "
    SELECT '  columns kept: ' || string_agg(column_name, ', ' ORDER BY ordinal_position)
    FROM information_schema.columns WHERE table_name='passages';"
  SOURCE_DB="$EXPORT_DB"
fi

# --- dump --------------------------------------------------------------------
printf 'CREATE EXTENSION IF NOT EXISTS vector;\n' > "$EXT_FILE"

echo
echo "Dumping songs + passages ($FORMAT format)..."
PG_DUMP_ARGS=(-U "$DB_USER" -d "$SOURCE_DB" --no-owner --no-privileges -t public.songs -t public.passages)
[[ "$FORMAT" == "custom" ]] && PG_DUMP_ARGS+=(--format=custom --compress=9)
docker exec -i "$PG_CONTAINER" pg_dump "${PG_DUMP_ARGS[@]}" > "$DUMP_FILE"

[[ "$WITH_MULTI" == "no" ]] && psql_in -d postgres -q -c "DROP DATABASE IF EXISTS $EXPORT_DB;"

# --- report ------------------------------------------------------------------
bytes_of() { wc -c < "$1" | tr -d ' '; }
human() { awk -v b="$1" 'BEGIN{s="B KB MB GB";split(s,u," ");for(i=1;b>=1024&&i<4;i++)b/=1024;printf "%.1f %s",b,u[i]}'; }

DUMP_BYTES=$(bytes_of "$DUMP_FILE")
TOTAL=$(( $(bytes_of "$EXT_FILE") + DUMP_BYTES ))

echo
echo "Wrote:"
printf '  %-44s %s\n' "$EXT_FILE" "$(human "$(bytes_of "$EXT_FILE")")"
printf '  %-44s %s\n' "$DUMP_FILE" "$(human "$DUMP_BYTES")"

echo
echo "Dump file vs storage limit $(human "$STORAGE_LIMIT_BYTES"): $(human "$TOTAL")"
cat <<'MSG'

  NOTE: the dump is compressed. What the hosting plan actually bills is the
  RESTORED size, which is larger — vectors do not compress and the HNSW index
  is rebuilt on top. Check the restored size against your plan, not this file.
MSG

echo "Restore:"
echo "  docker exec -i $PG_CONTAINER psql \"\$NEON_URL\" -f -  < $EXT_FILE"
if [[ "$FORMAT" == "custom" ]]; then
  echo "  docker exec -i $PG_CONTAINER pg_restore --no-owner --no-privileges -d \"\$NEON_URL\" < $DUMP_FILE"
else
  echo "  docker exec -i $PG_CONTAINER psql \"\$NEON_URL\" -f - < $DUMP_FILE"
fi
echo "See docs/deploy.md for the full procedure."
