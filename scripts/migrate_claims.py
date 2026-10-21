"""Preview selected Claim migrations; --apply consumes an unchanged saved preview."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--memory-root", required=True, type=Path)
    parser.add_argument("--page", action="append", default=[], help="Explicit Markdown basename; repeat for a bounded batch")
    parser.add_argument("--plan-out", type=Path, help="Write a new preview file; never overwrite")
    parser.add_argument("--plan", type=Path, help="Previously saved preview for --apply")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    os.environ["VECTOR_LAKE_MEMORY_DIR"] = str(args.memory_root.resolve())
    from vector_lake.claim_migration import apply_claim_migration, plan_claim_migration

    if args.apply:
        if not args.plan or args.page or args.plan_out:
            parser.error("--apply requires --plan and does not accept --page or --plan-out")
        plan = json.loads(args.plan.read_text(encoding="utf-8"))
        if Path(plan["memory_root"]).resolve() != args.memory_root.resolve():
            parser.error("--memory-root differs from the saved preview")
        result = apply_claim_migration(plan)
    else:
        if args.plan or not args.page:
            parser.error("Preview requires explicit --page values and does not accept --plan")
        result = plan_claim_migration(args.memory_root, args.page)
        if args.plan_out:
            with args.plan_out.open("x", encoding="utf-8") as stream:
                json.dump(result, stream, ensure_ascii=False, indent=2)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 2 if result.get("blockers") else 0


if __name__ == "__main__":
    raise SystemExit(main())
