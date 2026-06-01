"""Run once (or re-run safely) to create the database schema."""

import os
from pathlib import Path

import psycopg
from dotenv import load_dotenv

load_dotenv()

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def init_db() -> None:
    database_url = os.environ["DATABASE_URL"]
    sql = SCHEMA_PATH.read_text()

    with psycopg.connect(database_url) as conn:
        conn.execute(sql)
        conn.commit()

        rows = conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
        ).fetchall()
        tables = [r[0] for r in rows]

    print("Tables in public schema:", tables)
    for name in ("songs", "passages"):
        status = "OK" if name in tables else "MISSING"
        print(f"  {name}: {status}")


if __name__ == "__main__":
    init_db()
