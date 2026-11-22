#!/usr/bin/env python3
"""Build and install vector-lake-core (Rust native acceleration) into current Python environment."""

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CRATE_DIR = REPO_ROOT / "crates" / "vector_lake_core"


def main() -> int:
    print(f"==> Building vector-lake-core from {CRATE_DIR}...")
    try:
        subprocess.run(
            ["maturin", "build", "--release", "--locked"],
            cwd=CRATE_DIR,
            check=True,
        )
    except FileNotFoundError:
        print("Error: 'maturin' executable not found. Install it with: pip install maturin", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        print(f"Error: maturin build failed with exit code {exc.returncode}", file=sys.stderr)
        return exc.returncode

    wheel_dir = CRATE_DIR / "target" / "wheels"
    wheels = sorted(wheel_dir.glob("*.whl"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not wheels:
        print("Error: No built wheel found in", wheel_dir, file=sys.stderr)
        return 1

    latest_wheel = wheels[0]
    print(f"==> Installing {latest_wheel.name} into {sys.executable}...")
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "--force-reinstall", str(latest_wheel)],
        check=True,
    )

    print("==> Verifying import...")
    res = subprocess.run(
        [sys.executable, "-c", "import vector_lake_core; print('vector_lake_core version:', vector_lake_core.version())"],
        check=True,
        capture_output=True,
        text=True,
    )
    print("Success:", res.stdout.strip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
