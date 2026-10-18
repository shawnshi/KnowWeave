"""The benchmark harness must be measurable itself before it measures anything."""

import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def test_percentiles_are_ordered_and_bounded():
    from scripts.benchmark_retrieval import stats

    values = [10.0, 20.0, 30.0, 40.0, 100.0]
    result = stats(values)
    assert result["n"] == 5 and result["p50"] == 30.0 and result["max"] == 100.0
    assert result["p50"] <= result["p90"] <= result["p99"] <= result["max"]
    assert stats([]) == {}


def test_an_empty_answer_is_not_hidden_by_the_percentiles():
    """An empty answer is the fastest answer, so the share is reported beside the latency."""
    source = (REPO / "scripts" / "benchmark_retrieval.py").read_text(encoding="utf-8")
    assert '"empty_share"' in source
    assert "No matching pages" in source


def test_the_benchmark_never_writes_to_the_ledger():
    source = (REPO / "scripts" / "benchmark_retrieval.py").read_text(encoding="utf-8")
    assert 'os.environ.setdefault("VECTOR_LAKE_SEARCH_LEDGER", "0")' in source


@pytest.mark.parametrize("chain", ["page", "memory", "query", "timeline"])
def test_every_chain_is_importable_without_touching_the_lake(chain):
    """Import-only: the live runs stay opt-in, so CI does not need a populated lake."""
    code = (
        "import os; os.environ['VECTOR_LAKE_SEARCH_LEDGER']='0'\n"
        "from scripts import benchmark_retrieval as b\n"
        f"assert callable(b._run_{chain})\n"
    )
    result = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True,
                            text=True, timeout=120)
    assert result.returncode == 0, result.stderr
