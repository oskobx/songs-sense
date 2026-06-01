"""Load data/seed_list.csv into the songs table. Idempotent — safe to re-run."""

import math
import os
from pathlib import Path

import pandas as pd
import psycopg
from dotenv import load_dotenv

load_dotenv()

CSV_PATH = Path(__file__).parents[2] / "data" / "seed_list.csv"

INSERT_SQL = """
INSERT INTO songs (artist, featured_artists, title, year, genre, genius_id, tier)
VALUES (%s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (artist, title) DO NOTHING
"""


def _nullable_int(val) -> int | None:
    if val is None or (isinstance(val, float) and math.isnan(val)):
        return None
    return int(val)


def load() -> None:
    df = pd.read_csv(CSV_PATH)
    csv_rows = len(df)
    print(f"CSV rows:          {csv_rows}")

    keep = ["artist", "featured_artists", "title", "year", "genre", "genius_id", "tier"]
    df = df[keep]

    rows = [
        (
            row["artist"],
            row["featured_artists"] if pd.notna(row["featured_artists"]) else None,
            row["title"],
            _nullable_int(row["year"]),
            row["genre"] if pd.notna(row["genre"]) else None,
            _nullable_int(row["genius_id"]),
            row["tier"],
        )
        for _, row in df.iterrows()
    ]

    url = os.environ["DATABASE_URL"]
    with psycopg.connect(url) as conn:
        before = conn.execute("SELECT COUNT(*) FROM songs").fetchone()[0]

        with conn.cursor() as cur:
            cur.executemany(INSERT_SQL, rows)

        conn.commit()

        after = conn.execute("SELECT COUNT(*) FROM songs").fetchone()[0]
        inserted = after - before
        skipped = csv_rows - inserted

        print(f"Rows inserted:     {inserted}")
        print(f"Rows skipped:      {skipped}  (conflict on artist+title)")
        print(f"Total in songs:    {after}")

        print("\nPer-tier breakdown:")
        tier_rows = conn.execute(
            "SELECT tier, COUNT(*) FROM songs GROUP BY tier ORDER BY tier"
        ).fetchall()
        for tier, count in tier_rows:
            print(f"  {tier:<20} {count}")


if __name__ == "__main__":
    load()
