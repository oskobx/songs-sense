#!/usr/bin/env bash
#
# Dump the retrieval tables for restore into Neon, and report the size so it can
# be checked against the hosting plan's storage limit before you upload anything.
#
# By default the dump EXCLUDES two things the hosted app never reads:
#
#   embedding_multi + its HNSW index  — bge-m3 vectors; production is
#     English-only, and this is over half the restored size.
#   passage_tsv + its GIN index + trigger — full-text column serving BM25,
#     which only the unexposed "Find the Song" profile uses.
#
# Stripping before upload beats restoring everything and dropping it after:
# DROP COLUMN does not reclaim space without a VACUUM FULL, which on a small
# hosted instance is slow and can run the database out of storage mid-restore.
#
# The strip is done on a throwaway TEMPLATE copy of the database, so the local
# database is never modified and every other index, constraint and trigger
# survives untouched.
#
# --with-multi and --with-fts each put one back; use both for a full backup.
#
# pg_dump runs inside the Postgres container: there is no local pg_dump on this
# machine, and the server's own binary can never be too old for the server.
#
# Usage:
#   scripts/export_for_neon.sh                        # lean deploy dump (default)
#   scripts/export_for_neon.sh --with-multi --with-fts # full backup
#   scripts/export_for_neon.sh --plain                 # plain SQL instead of custom
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
WITH_FTS="no"
for arg in "$@"; do
  case "$arg" in
    --plain)      FORMAT="plain" ;;
    --with-multi) WITH_MULTI="yes" ;;
    --with-fts)   WITH_FTS="yes" ;;
    *) echo "unknown argument: $arg (expected --plain, --with-multi, --with-fts)" >&2; exit 2 ;;
  esac
done
STRIP_ANY="yes"
[[ "$WITH_MULTI" == "yes" && "$WITH_FTS" == "yes" ]] && STRIP_ANY="no"

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
if   [[ "$STRIP_ANY" == "no"   ]]; then SUFFIX=""
elif [[ "$WITH_MULTI" == "no" && "$WITH_FTS" == "no" ]]; then SUFFIX="_lean"
elif [[ "$WITH_MULTI" == "no" ]]; then SUFFIX="_nomulti"
else SUFFIX="_nofts"
fi
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
if [[ "$STRIP_ANY" == "yes" ]]; then
  echo
  echo "Building a stripped copy ($EXPORT_DB)..."
  # TEMPLATE copy is a file-level clone: fast, and it carries every index,
  # constraint and trigger, so the dump needs no manual reconstruction later.
  psql_in -d postgres -q -v ON_ERROR_STOP=1 <<SQL
SELECT pg_terminate_backend(pid) FROM pg_stat_activity
 WHERE datname = '$DB_NAME' AND pid <> pg_backend_pid();
DROP DATABASE IF EXISTS $EXPORT_DB;
CREATE DATABASE $EXPORT_DB TEMPLATE $DB_NAME;
SQL
  if [[ "$WITH_MULTI" == "no" ]]; then
    echo "  dropping embedding_multi + its HNSW index"
    psql_in -d "$EXPORT_DB" -q -v ON_ERROR_STOP=1 <<'SQL'
DROP INDEX IF EXISTS passages_embedding_multi_hnsw_idx;
ALTER TABLE passages DROP COLUMN IF EXISTS embedding_multi;
SQL
  fi
  if [[ "$WITH_FTS" == "no" ]]; then
    # The trigger must go first: it names passage_tsv as a string argument, so
    # DROP COLUMN leaves it in place and any later insert errors on a column
    # that no longer exists.
    echo "  dropping passage_tsv + its GIN index + trigger"
    psql_in -d "$EXPORT_DB" -q -v ON_ERROR_STOP=1 <<'SQL'
DROP TRIGGER IF EXISTS passages_tsv_trigger ON passages;
DROP INDEX IF EXISTS passages_passage_tsv_idx;
ALTER TABLE passages DROP COLUMN IF EXISTS passage_tsv;
SQL
  fi
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

[[ "$STRIP_ANY" == "yes" ]] && psql_in -d postgres -q -c "DROP DATABASE IF EXISTS $EXPORT_DB;"

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
