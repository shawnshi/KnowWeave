"""Read-only job queue report.

Run from the repository root:

    python check_jobs.py
"""
import json
import os
import sqlite3
import sys

# Import the package from the repository it lives in; never from a hard-coded
# machine-specific path.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from vector_lake.db_store import get_db_path  # noqa: E402


def main() -> int:
    db_path = get_db_path()
    if not db_path.is_file():
        print(f"No Vector Lake database found at {db_path}; skipping job report.")
        return 0

    conn = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2.0)
    conn.row_factory = sqlite3.Row
    try:
        print("Job statuses:")
        for row in conn.execute("SELECT status, COUNT(*) AS count FROM jobs GROUP BY status"):
            print(f"  {row['status']}: {row['count']}")

        filepaths = set()
        unparsable = 0
        for row in conn.execute("SELECT payload FROM jobs WHERE status = 'queued'"):
            try:
                payload = json.loads(row["payload"])
            except (TypeError, json.JSONDecodeError):
                unparsable += 1
                continue
            if isinstance(payload, dict) and payload.get("filepath"):
                filepaths.add(payload["filepath"])
        print(f"Distinct filepaths queued: {len(filepaths)}")
        if unparsable:
            print(f"Queued jobs with unreadable payloads: {unparsable}")

        terminal = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE status IN ('failed') AND retries >= 3"
        ).fetchone()[0]
        print(f"Terminal failed jobs (retries >= 3): {terminal}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
