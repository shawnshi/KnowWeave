from pathlib import Path

import pytest

from vector_lake.subagent_handoff import SubagentTaskRequired, generate_json_array, in_process_text_generation_ready


ROOT = Path(__file__).resolve().parents[1]


def test_text_generation_boundary_does_not_call_generate_content():
    source = (ROOT / "vector_lake" / "subagent_handoff.py").read_text(encoding="utf-8")
    assert "generate_content" not in source
    assert "genai.Client" not in source


def test_generate_json_array_creates_subagent_packet(monkeypatch, tmp_path):
    monkeypatch.setenv("VECTOR_LAKE_SUBAGENT_RUN_ID", "pytest-text-boundary")
    ok, detail = in_process_text_generation_ready()
    assert ok is False
    assert "subagent" in detail

    with pytest.raises(SubagentTaskRequired) as exc:
        generate_json_array("return []")

    task_path = exc.value.task_path
    assert task_path.exists()
    assert "brain" in task_path.parts
    assert "subagent_tasks" in task_path.parts
    task_path.unlink()
