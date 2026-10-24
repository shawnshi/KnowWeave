"""Additive observation only: preserve calls, failures and the parent timer."""

import pytest

from vector_lake import page_index_projection, retrieval_runtime, search_ledger, tool_search


@pytest.fixture
def observed(monkeypatch):
    calls = []
    records = []
    catalog = object()
    monkeypatch.setattr(page_index_projection, "index_file_stamp", lambda: ("synthetic",))
    monkeypatch.setattr(retrieval_runtime, "defer_record", lambda *args, **kwargs: False)
    monkeypatch.setattr(search_ledger, "record", lambda *args, **kwargs: records.append(kwargs))

    def names(*args):
        calls.append(("names", args))
        return []

    def intent(query):
        calls.append(("intent", query))
        return "general"

    def expand(query):
        calls.append(("expand", query))
        return []

    monkeypatch.setattr(tool_search, "_name_entry_candidates", names)
    monkeypatch.setattr(tool_search, "_keyword_intent", intent)
    monkeypatch.setattr(tool_search, "_expand_query_locally", expand)
    return catalog, calls, records


@pytest.mark.parametrize("profile", ["legacy", "source"])
def test_children_preserve_call_order_and_parent(observed, profile):
    catalog, calls, records = observed
    timings = {"stale": 123}
    result = tool_search._search_scored_pages(
        "synthetic", 4, "Medical_IT", "Example", False, None, catalog,
        timings=timings, allow_external_embedding=False, profile=profile,
    )
    assert result == ([], [], "No valid search tokens.")
    assert calls == [
        ("names", ("synthetic", catalog, "Medical_IT", "Example", False, None)),
        ("intent", "synthetic"), ("expand", "synthetic"),
    ]
    assert "stale" not in timings
    children = [timings[f"query_processing.{name}"] for name in (
        "name_candidates", "keyword_intent", "local_expansion"
    )]
    assert all(value >= 0 for value in children)
    assert sum(children) <= timings["query_processing"]
    assert records[-1]["timings"]["query_processing.local_expansion"] >= 0


def test_child_exception_is_not_changed_or_hidden(observed, monkeypatch):
    catalog, calls, records = observed
    failure = RuntimeError("synthetic expansion failure")

    def fail(query):
        raise failure

    monkeypatch.setattr(tool_search, "_expand_query_locally", fail)
    timings = {}
    with pytest.raises(RuntimeError) as error:
        tool_search._search_scored_pages(
            "synthetic", 4, catalog=catalog, timings=timings,
            allow_external_embedding=False,
        )
    assert error.value is failure
    assert timings["query_processing.local_expansion"] >= 0
    assert "query_processing" not in timings
    assert records == []
