"""Test improvements and extensions on the vector-lake MCP server surface."""

from __future__ import annotations

import json
from unittest.mock import patch

from vector_lake import mcp_server


def test_inspect_projections_is_registered_and_returns_status():
    names = mcp_server.registered_tool_names(mcp_server.mcp)
    assert "inspect_projections" in names

    output = mcp_server.inspect_projections()
    assert isinstance(output, str)
    assert "memory_gram" in output
    assert "vectors" in output
    assert "fts_index" in output
    assert "timeline_events" in output


def test_search_vector_lake_forwards_advanced_filters():
    with patch("vector_lake.tools.search_vector_lake") as mock_search:
        mock_search.return_value = "mocked results"
        res = mcp_server.search_vector_lake(
            query="信创替代",
            top_k=7,
            mode="page",
            domain="HIT",
            cluster="Architecture",
            include_history=True,
            as_xml=True,
        )
        assert res == "mocked results"
        mock_search.assert_called_once_with(
            "信创替代",
            7,
            as_xml=True,
            domain="HIT",
            cluster="Architecture",
            include_history=True,
            mode="page",
            allow_external_embedding=False,
            profile="auto",
            use_chunks=False,
        )


def test_update_operational_memory_supports_direct_text_content():
    with patch("vector_lake.tool_memory.update_operational_memory") as mock_update:
        mock_update.return_value = "Memory persisted."
        res = mcp_server.update_operational_memory(
            memory_type="preference",
            content="优先考虑信创路线",
        )
        assert res == "Memory persisted."
        mock_update.assert_called_once_with("preference", "优先考虑信创路线")


def test_resolve_governance_item_supports_direct_manifest_json():
    with patch("vector_lake.tools.review_vector_lake") as mock_review:
        mock_review.return_value = "Resolved item."
        res = mcp_server.resolve_governance_item(
            item_id="gov_123",
            resolution="skip",
            manifest_json=json.dumps({"reason": "not applicable"}),
        )
        assert res == "Resolved item."
        mock_review.assert_called_once_with(
            action="resolve",
            index="gov_123",
            resolution="skip",
            change_manifest={"reason": "not applicable"},
        )
