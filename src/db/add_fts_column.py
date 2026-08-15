"""One-time migration: add tsvector column, backfill, GIN index, update trigger.

Safe to re-run: uses IF NOT EXISTS and CREATE OR REPLACE.
"""

from __future__ import annotations

import os

import psycopg
from dotenv import load_dotenv

load_dotenv()


def main() -> None:
    url = os.environ["DATABASE_URL"]
    with psycopg.connect(url, autocommit=True) as conn:
        print("Adding passage_tsv column (IF NOT EXISTS)...")
        conn.execute(
            "ALTER TABLE passages ADD COLUMN IF NOT EXISTS passage_tsv tsvector"
        )

        print("Backfilling tsvector for all rows (may take 1-2 min for 86k rows)...")
        conn.execute(
            "UPDATE passages SET passage_tsv = to_tsvector('english', COALESCE(passage_text, ''))"
        )
        count = conn.execute(
            "SELECT COUNT(*) FROM passages WHERE passage_tsv IS NOT NULL"
        ).fetchone()[0]
        print(f"  {count:,} rows populated.")

        print("Creating GIN index (IF NOT EXISTS)...")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS passages_passage_tsv_idx ON passages USING GIN (passage_tsv)"
        )
        print("  Index created.")

        print("Creating update trigger (CREATE OR REPLACE)...")
        conn.execute("""
            CREATE OR REPLACE TRIGGER passages_tsv_trigger
            BEFORE INSERT OR UPDATE OF passage_text ON passages
            FOR EACH ROW EXECUTE FUNCTION
            tsvector_update_trigger(passage_tsv, 'pg_catalog.english', passage_text)
        """)
        print("  Trigger created.")

        # Quick verification
        idx = conn.execute(
            "SELECT indexname FROM pg_indexes WHERE indexname = 'passages_passage_tsv_idx'"
        ).fetchone()
        print(f"\nVerification:")
        print(f"  passage_tsv populated : {count:,} rows")
        print(f"  GIN index exists      : {'YES' if idx else 'NO'}")
        print("\nMigration complete.")


if __name__ == "__main__":
    main()
