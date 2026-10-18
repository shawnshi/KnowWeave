"""Measure the retrieval chains against the lake in use, without writing to it.

Three chains, three questions, and each one is a shape of query a person actually types:

    python scripts/benchmark_retrieval.py page      # lexical + graph + rerank, hit-heavy mix
    python scripts/benchmark_retrieval.py memory    # operational memory, unscoped and scoped
    python scripts/benchmark_retrieval.py query     # context assembly
    python scripts/benchmark_retrieval.py timeline  # timeline projection

The numbers are only comparable inside one host and one lake: the payload scans are
I/O-bound and this machine measured 1.5-5x swings with the same code when memory was
tight.  The script therefore prints the environment it ran in, and it forces the search
ledger off so a benchmark never lands in the file the doctor reads.

What it reports is a distribution, not a score: p50/p90/p99 and, for the page mix, the
share of queries that came back empty.  An empty answer is the fastest answer, so a
latency p50 without its empty share describes two different systems.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import sys
import time
from pathlib import Path

os.environ.setdefault("VECTOR_LAKE_SEARCH_LEDGER", "0")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def stats(values: list[float]) -> dict:
    if not values:
        return {}
    ordered = sorted(values)
    pick = lambda fraction: round(ordered[min(len(ordered) - 1, int(len(ordered) * fraction))], 1)
    return {"n": len(ordered), "p50": pick(0.5), "p90": pick(0.9), "p99": pick(0.99),
            "max": round(ordered[-1], 1), "mean": round(statistics.fmean(ordered), 1)}


def environment() -> dict:
    from vector_lake import db_store, memory_gram_index, scoped_retrieval_index

    conn = db_store.get_connection()
    counts = {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
              for table in ("claims", "operational_memory", "timeline_events",
                            "page_index_nodes", "page_index_edges", "vec_embeddings")}
    return {
        "db": str(db_store.get_db_path()),
        "db_bytes": db_store.get_db_path().stat().st_size,
        "mmap_bytes": conn.execute("PRAGMA mmap_size").fetchone()[0],
        "cache_kib": conn.execute("PRAGMA cache_size").fetchone()[0],
        "counts": counts,
        "gram_index_usable": memory_gram_index.gram_index_usable(),
        "memory_accelerators": scoped_retrieval_index.status(conn)["memory_acceleration"],
        "ledger": "off",
    }


def page_mix(limit: int = 40, seed: int = 7) -> list[tuple[str, str]]:
    """Real titles plus natural-language questions: the failure mode differs between them."""
    from vector_lake import db_store

    rows = db_store.get_connection().execute(
        "SELECT title, domain FROM page_index_nodes "
        "WHERE title IS NOT NULL AND length(title) BETWEEN 3 AND 40 "
        "AND domain IN ('Medical_IT','Artificial_Intelligence','Policy_and_Governance',"
        "'Enterprise_Software','Strategy_and_Business')"
    ).fetchall()
    mix = [(str(row[0]), str(row[1])) for row in rows]
    random.Random(seed).shuffle(mix)
    mix = mix[:limit]
    mix.extend([
        ("电子病历系统应用水平分级评价六级", "Medical_IT"),
        ("医疗人工智能在医院的落地路径与风险", "Artificial_Intelligence"),
        ("DRG/DIP 支付方式改革对公立医院运营的影响", "Policy_and_Governance"),
        ("医院核心业务系统信创替代与解耦架构", "Medical_IT"),
        ("互联互通五乙测评要点", "Medical_IT"),
    ])
    return mix


def _run_page(top_k: int) -> dict:
    from vector_lake import retrieval_runtime as runtime, tool_search

    mix = page_mix()
    for query, domain in mix[:5]:
        with runtime.request_scope(query, top_k=top_k):
            tool_search.search_vector_lake(query, top_k=top_k, domain=domain, profile="auto")
    rows = []
    for query, domain in mix:
        started = time.perf_counter()
        with runtime.request_scope(query, top_k=top_k):
            answer = tool_search.search_vector_lake(query, top_k=top_k, domain=domain,
                                                    profile="auto")
        rows.append({"ms": (time.perf_counter() - started) * 1000,
                     "answered": "No matching pages" not in answer})
    answered = [row["ms"] for row in rows if row["answered"]]
    return {"top_k": top_k, "all": stats([row["ms"] for row in rows]),
            "answered": stats(answered),
            "empty_share": round(1 - len(answered) / len(rows), 3)}


def _run_memory() -> dict:
    from vector_lake import retrieval_runtime as runtime, tool_search

    query = "电子病历 评级 DRG 支付"
    out = {}
    for name, kwargs in (("unscoped", {}),
                         ("Medical_IT", {"domain": "Medical_IT"}),
                         ("Artificial_Intelligence", {"domain": "Artificial_Intelligence"})):
        samples = []
        for _ in range(3):
            started = time.perf_counter()
            with runtime.request_scope(query, top_k=5):
                tool_search.search_vector_lake(query, top_k=5, mode="memory", **kwargs)
            samples.append((time.perf_counter() - started) * 1000)
        out[name] = stats(samples)
    return out


def _run_query() -> dict:
    from vector_lake import retrieval_runtime as runtime, tool_query

    query = "医疗人工智能在医院的落地路径与风险"
    out = {}
    for name, kwargs in (("unscoped", {}), ("Medical_IT", {"domain": "Medical_IT"}),
                         ("Artificial_Intelligence", {"domain": "Artificial_Intelligence"})):
        samples = []
        for _ in range(3):
            started = time.perf_counter()
            with runtime.request_scope(query, max_chars=200000):
                tool_query.assemble_context(query, profile="auto", **kwargs)
            samples.append((time.perf_counter() - started) * 1000)
        out[name] = stats(samples)
    return out


def _run_timeline() -> dict:
    from vector_lake import tool_timeline

    out = {}
    for name, kwargs in (("limit10", {}), ("entity", {"entity_name": "卫宁健康"}),
                         ("action", {"action": "Observation"})):
        samples = []
        for _ in range(10):
            started = time.perf_counter()
            tool_timeline.search_timeline_events(**kwargs)
            samples.append((time.perf_counter() - started) * 1000)
        out[name] = stats(samples)
    tool_timeline.invalidate_timeline_parity_cache()
    started = time.perf_counter()
    tool_timeline.search_timeline_events(limit=10)
    out["cold_parity_ms"] = round((time.perf_counter() - started) * 1000, 1)
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("chain", choices=("all", "page", "memory", "query", "timeline"))
    parser.add_argument("--top-k", type=int, default=5)
    args = parser.parse_args(argv)

    report = {"environment": environment()}
    if args.chain in ("all", "page"):
        report["page"] = _run_page(args.top_k)
    if args.chain in ("all", "timeline"):
        report["timeline"] = _run_timeline()
    if args.chain in ("all", "memory"):
        report["memory"] = _run_memory()
    if args.chain in ("all", "query"):
        report["query"] = _run_query()
    print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
