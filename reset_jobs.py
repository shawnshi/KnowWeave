"""Recover jobs stranded in the ``dispatched`` state.

Dry-run by default.  Run from the repository root:

    python reset_jobs.py            # report what would be reset
    python reset_jobs.py --apply    # perform the reset

Prefer ``python cli.py ingest-tasks --expire-stale`` for the normal recovery
path; this script is the explicit, audited escape hatch.
"""
import argparse
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from vector_lake.db_store import get_connection, init_db, transaction  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Recover jobs stranded in 'dispatched'.")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Persist the reset. Defaults to a dry run.",
    )
    args = parser.parse_args()

    init_db()
    conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()
    expired = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE status = 'dispatched' "
        "AND (lease_until IS NULL OR lease_until < ?)",
        (now,),
    ).fetchone()[0]
    total = conn.execute("SELECT COUNT(*) FROM jobs WHERE status = 'dispatched'").fetchone()[0]

    if not args.apply:
        print(
            f"[DRY RUN] {expired} of {total} 'dispatched' job(s) have an expired or missing lease "
            "and would be returned to 'queued'. Re-run with --apply to persist."
        )
        return 0

    with transaction():
        cursor = conn.execute(
            "UPDATE jobs "
            "SET status = 'queued', "
            "    lease_owner = NULL, "
            "    lease_token = NULL, "
            "    lease_until = NULL, "
            "    lease_generation = COALESCE(lease_generation, 0) + 1 "
            "WHERE status = 'dispatched' AND (lease_until IS NULL OR lease_until < ?)",
            (now,),
        )
        reset = cursor.rowcount
    print(f"Reset {reset} expired dispatched job(s) to 'queued' (of {total} dispatched).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
