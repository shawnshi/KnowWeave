#!/usr/bin/env python
"""Gemini/Codex/Antigravity model seam: one task packet on stdin, one semantic JSON result on stdout."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

from vector_lake.ingest_backend import BACKEND_COMMANDS, IngestBackend, check_ingest_backend  # noqa: E402
from vector_lake.ingest_cli import invoke_cli  # noqa: E402


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", required=True, choices=("codex", "gemini", "agy"))
    parser.add_argument("--check", action="store_true", help="Check local CLI capabilities without model calls or jobs.")
    args = parser.parse_args()
    try:
        if args.check:
            selection = IngestBackend(args.backend, BACKEND_COMMANDS[args.backend], "cli")
            result = check_ingest_backend(selection)
        else:
            result = invoke_cli(json.loads(sys.stdin.read()), args.backend)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except subprocess.TimeoutExpired:
        print(f"{args.backend} model seam: execution timed out", file=sys.stderr)
        return 4
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"{args.backend} model seam: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
