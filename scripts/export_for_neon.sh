#!/usr/bin/env bash
#
# Dump the retrieval tables for restore into Neon, and report the size so it can
# be checked against the free tier's 0.5 GB storage limit before you commit to it.
#
# Dumps schema + data for `songs` and `passages`, including BOTH the `embedding`
# (bge-base, 768d) and `embedding_multi` (bge-m3, 1024d) columns, plus their
# indexes. pg_dump does not emit CREATE EXTENSION for a table-scoped dump, so
# the vector extension is written to a separate file that restores first.
#
# pg_dump runs inside the Postgres container by default: there is no local
# pg_dump on this machine, and using the server's own binary guarantees the
# client is never older than the server.
#
# Usage:
#   scripts/export_for_neon.sh                 # custom format, compressed
#   scripts/export_for_neon.sh --plain         # plain SQL, restorable with psql
#   OUT_DIR=/tmp/dump scripts/export_for_neon.sh
#
# Env:
#   PG_CONTAINER  Postgres container name        (default: songs-sense-db)
#   OUT_DIR       where to write the dump        (default: data/neon)
#   DATABASE_URL  only used to read the db name  (default: from .env)

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

PG_CONTAINER="${PG_CONTAINER:-songs-sense-db}"
OUT_DIR="${OUT_DIR:-data/neon}"
NEON_FREE_TIER_BYTES=$((512 * 1024 * 1024))   # 0.5 GB

FORMAT="custom"
if [[ "${1:-}" == "--plain" ]]; then
  FORMAT="plain"
elif [[ -n "${1:-}" ]]; then
  echo "unknown argument: $1 (expected --plain)" >&2
  exit 2
fi

# --- database name -----------------------------------------------------------
if [[ -z "${DATABASE_URL:-}" && -f .env ]]; then
  DATABASE_URL="$(grep -E '^DATABASE_URL=' .env | head -1 | cut -d= -f2-)"
fi
if [[ -z "${DATABASE_URL:-}" ]]; then
  echo "DATABASE_URL is not set and .env has no entry for it" >&2
  exit 1
fi
DB_NAME="${DATABASE_URL##*/}"
DB_NAME="${DB_NAME%%\?*}"
DB_USER="${DATABASE_URL#*://}"
DB_USER="${DB_USER%%:*}"

if ! docker ps --format '{{.Names}}' | grep -qx "$PG_CONTAINER"; then
  echo "Postgres container '$PG_CONTAINER' is not running." >&2
  echo "Start it with: docker compose up -d" >&2
  exit 1
fi

mkdir -p "$OUT_DIR"
EXT_FILE="$OUT_DIR/01_extension.sql"
if [[ "$FORMAT" == "custom" ]]; then
  DUMP_FILE="$OUT_DIR/02_songs_passages.dump"
else
  DUMP_FILE="$OUT_DIR/02_songs_passages.sql"
fi

# --- sanity: the columns we promise to carry ---------------------------------
echo "Source: $DB_NAME (container $PG_CONTAINER)"
docker exec -i "$PG_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -tAc "
  SELECT '  passages.' || column_name
  FROM information_schema.columns
  WHERE table_name = 'passages' AND column_name IN ('embedding', 'embedding_multi')
  ORDER BY column_name;"

docker exec -i "$PG_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -tAc "
  SELECT '  rows: songs=' || (SELECT count(*) FROM songs)
      || ', passages=' || (SELECT count(*) FROM passages)
      || ', with embedding=' || (SELECT count(*) FROM passages WHERE embedding IS NOT NULL)
      || ', with embedding_multi=' || (SELECT count(*) FROM passages WHERE embedding_multi IS NOT NULL);"

# --- dump --------------------------------------------------------------------
printf 'CREATE EXTENSION IF NOT EXISTS vector;\n' > "$EXT_FILE"

echo
echo "Dumping songs + passages ($FORMAT format)..."
PG_DUMP_ARGS=(-U "$DB_USER" -d "$DB_NAME" --no-owner --no-privileges -t public.songs -t public.passages)
if [[ "$FORMAT" == "custom" ]]; then
  PG_DUMP_ARGS+=(--format=custom --compress=9)
fi
docker exec -i "$PG_CONTAINER" pg_dump "${PG_DUMP_ARGS[@]}" > "$DUMP_FILE"

# --- report ------------------------------------------------------------------
bytes_of() { wc -c < "$1" | tr -d ' '; }
human() { awk -v b="$1" 'BEGIN{s="B KB MB GB";split(s,u," ");for(i=1;b>=1024&&i<4;i++)b/=1024;printf "%.1f %s",b,u[i]}'; }

EXT_BYTES=$(bytes_of "$EXT_FILE")
DUMP_BYTES=$(bytes_of "$DUMP_FILE")
TOTAL=$((EXT_BYTES + DUMP_BYTES))

echo
echo "Wrote:"
printf '  %-40s %s\n' "$EXT_FILE" "$(human "$EXT_BYTES")"
printf '  %-40s %s\n' "$DUMP_FILE" "$(human "$DUMP_BYTES")"
printf '  %-40s %s\n' "total" "$(human "$TOTAL")"

echo
echo "Neon free tier storage: $(human "$NEON_FREE_TIER_BYTES")"
if (( TOTAL > NEON_FREE_TIER_BYTES )); then
  cat <<MSG
  DOES NOT FIT. Note the dump is compressed; restored size will be larger still,
  and HNSW indexes add more on top.

  Options, cheapest first:
    1. Restore, then drop the multilingual column and its index:
         ALTER TABLE passages DROP COLUMN embedding_multi;
       Production runs EMBED_MULTILINGUAL=false anyway, so nothing breaks.
    2. Restore fewer songs (filter by tier) for the hosted demo.
    3. Pay for a larger Neon plan.
MSG
else
  echo "  Fits, with $(human $((NEON_FREE_TIER_BYTES - TOTAL))) to spare (before index rebuild)."
fi

cat <<MSG

Restore into Neon:
  psql "\$NEON_URL" -f $EXT_FILE
MSG
if [[ "$FORMAT" == "custom" ]]; then
  echo "  pg_restore --no-owner --no-privileges -d \"\$NEON_URL\" $DUMP_FILE"
else
  echo "  psql \"\$NEON_URL\" -f $DUMP_FILE"
fi
echo "See docs/deploy.md for the full procedure."
