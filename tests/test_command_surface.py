import inspect
import json
import re
import shlex
from pathlib import Path

from vector_lake import mcp_server


ROOT = Path(__file__).resolve().parents[1]

#: The published MCP server key, shared by both host manifests and the README example.
EXPECTED_MCP_SERVER = "Reson-KnowWeave-mcp"


def test_query_and_timeline_mcp_tools_are_registered():
    """The two capabilities the old compat layer mapped to must still exist."""
    names = mcp_server.registered_tool_names(mcp_server.mcp)

    assert "query_logic_lake" in names
    assert "search_timeline" in names
    assert callable(mcp_server.query_logic_lake)
    assert callable(mcp_server.search_timeline)


def test_query_and_timeline_codex_skills_are_packaged():
    assert (ROOT / "skills" / "vector-lake-query" / "SKILL.md").is_file()
    assert (ROOT / "skills" / "vector-lake-timeline" / "SKILL.md").is_file()


def test_all_packaged_skills_use_vector_lake_prefix():
    """All skills exposed by Vector Lake must be namespaced with vector-lake-* to avoid global collisions."""
    skills_dir = ROOT / "skills"
    assert skills_dir.is_dir()
    for child in skills_dir.iterdir():
        if child.is_dir() and (child / "SKILL.md").is_file():
            assert child.name.startswith("vector-lake-"), f"Skill {child.name} lacks vector-lake-* prefix"


def test_the_mcp_query_tool_forwards_the_documented_dry_run_switch(monkeypatch):
    """The prompt template documents ``dry_run: true``; the wrapper dropped the argument.

    ``prepare_query_context`` has always accepted it and the CLI passes it, so an MCP caller
    had no way to reach the behaviour the template promises.
    """
    parameters = inspect.signature(mcp_server.query_logic_lake).parameters
    assert "dry_run" in parameters, parameters

    seen = []

    def fake(query_str, dry_run=False, **scope):
        seen.append((query_str, dry_run, scope))
        return "rendered"

    monkeypatch.setattr(mcp_server.tools, "prepare_query_context", fake)

    assert mcp_server.query_logic_lake("q", domain="Medical_IT") == "rendered"
    assert mcp_server.query_logic_lake("q", dry_run=True, domain="Medical_IT") == "rendered"
    scope = {"domain": "Medical_IT", "cluster": None, "allow_external_embedding": False,
             "profile": "auto", "use_chunks": False}
    assert seen == [("q", False, scope), ("q", True, scope)], seen


def test_host_mcp_manifests_are_identical():
    """Hosts look for different manifest filenames; keep one source, assert the mirror.

    ``.mcp.json`` is canonical and ``mcp_config.json`` is a byte-identical
    mirror for the other host convention.  They are not produced by a generator
    script, so this test is the only thing preventing silent drift.

    The published server name is pinned here *and* cross-checked against the README, because the
    2026-09 rename (``vector-lake-mcp`` -> ``mentat-mind-mcp``) landed in the manifests and the
    README but left this assertion behind: the guard then failed on the new name instead of
    catching a rename that only reached one surface.  A name is published in three places, so the
    assertion has to look at all three.
    """
    canonical = (ROOT / ".mcp.json").read_bytes()
    assert (ROOT / "mcp_config.json").read_bytes() == canonical, (
        ".mcp.json and mcp_config.json diverged; .mcp.json is canonical"
    )

    servers = json.loads(canonical)["mcpServers"]
    assert set(servers) == {EXPECTED_MCP_SERVER}, (
        f"the host manifests must publish exactly {EXPECTED_MCP_SERVER!r}, got {sorted(servers)}"
    )
    entry = servers[EXPECTED_MCP_SERVER]
    assert entry["args"] == ["-m", "vector_lake.mcp_server"]
    assert entry["env"]["PYTHONPATH"] == "."

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert f'"{EXPECTED_MCP_SERVER}"' in readme, (
        f"README must document the published MCP server name {EXPECTED_MCP_SERVER!r}"
    )
    stale = "vector-lake-mcp"
    stale_files = [
        str(path.relative_to(ROOT))
        for path in list(ROOT.glob("skills/*/SKILL.md"))
        if stale in path.read_text(encoding="utf-8")
    ]
    assert stale_files == [], (
        f"these shipped skills still call the MCP server {stale!r}: {stale_files}"
    )


def test_no_slash_command_compat_layer_ships():
    """`commands/` was removed on purpose; the MCP surface is the command surface.

    It held two `commands/*.toml` files that no code read, and CONTEXT.md section 4
    advertised sixteen of them at one point.  This guard fires on a silent
    re-introduction and on documentation that drifts back to claiming they ship.
    """
    assert not (ROOT / "commands").exists(), (
        "commands/ reappeared; the slash-command compat layer was removed in the "
        "host-convention batch. Update CONTEXT.md section 4 and README.md before "
        "bringing it back"
    )

    context = (ROOT / "CONTEXT.md").read_text(encoding="utf-8")
    assert "commands/*.toml` files ship" not in context
    assert "Gemini CLI compatibility commands" not in context

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "commands/query.toml" not in readme
    assert "commands/timeline.toml" not in readme


def test_mcp_server_registers_its_tool_surface():
    """The server registers the optional strict facade and inline preview."""
    names = mcp_server.registered_tool_names(mcp_server.mcp)

    # Retain the original 21-tool boundary and admit only the two reviewed additions:
    # scoped relation reads and canonical/outbox mutation receipts.
    expected = {
        "search_timeline", "inspect_projections", "get_query_evidence", "search_vector_lake",
        "update_operational_memory", "lint_vector_lake", "preview_query_context",
        "query_logic_lake", "finalize_query_synthesis", "review_governance_list",
        "resolve_governance_item", "get_governance_debt", "trigger_audit_graph",
        "runtime_identity", "doctor_vector_lake", "rename_entity", "trace_vector_lake",
        "merge_suggestions_vector_lake", "check_duplicate_entity", "visualize_vector_lake",
        "write_wiki_page", "get_relations", "get_mutation_status",
    }
    assert set(names) == expected


def test_operations_maintenance_examples_match_parser_and_bind_approval():
    from scripts import repair_orphan_memory

    operations = (ROOT / "docs" / "OPERATIONS.md").read_text(encoding="utf-8")
    commands = [line for line in operations.splitlines() if line.startswith("python scripts/repair_orphan_memory.py ")]
    assert len(commands) == 5
    parser_source = inspect.getsource(repair_orphan_memory.main)
    documented_modes = set()
    for line in commands:
        documented_modes.add(re.search(r"--mode ([a-z-]+)", line).group(1))
        for option in re.findall(r"--[a-z0-9-]+", line):
            assert f'"{option}"' in parser_source
        if "--mode apply " in line or "--mode restore " in line or "--mode refresh-triggers " in line:
            assert "--approved-count $ApprovedCount" in line
            assert "--approved-sha256 $ApprovedScopeHash" in line
    assert documented_modes == {"freeze", "dry-run", "refresh-triggers", "apply", "restore"}


def test_readme_json_examples_are_parseable_and_use_the_published_module():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    blocks = re.findall(r"```json\s*\n(.*?)```", readme, re.DOTALL)
    assert blocks, "README must include a copyable MCP configuration"
    configs = []
    for block in blocks:
        parsed = json.loads(block)
        if "mcpServers" in parsed:
            configs.append(parsed)
    assert len(configs) == 1
    entry = configs[0]["mcpServers"][EXPECTED_MCP_SERVER]
    assert entry["args"] == ["-m", "vector_lake.mcp_server"]
    assert entry["env"]["PYTHONPATH"] == "C:/path/to/KnowWeave"
    assert entry["command"] == "C:/path/to/KnowWeave/.venv/Scripts/python.exe"
    assert "GEMINI_API_KEY" not in entry["env"]


def test_readme_tool_table_matches_registered_tools():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    table = readme.split("## Commands", 1)[1].split("基础体检：", 1)[0]
    names = set()
    for line in table.splitlines():
        if line.startswith("|**"):
            tool_cell = line.split("|")[2]
            names.update(re.findall(r"`([a-z_]+)`", tool_cell))
    assert names == set(mcp_server.registered_tool_names(mcp_server.mcp))


def test_readme_cli_examples_parse_without_running_commands():
    from vector_lake.cli_app import build_parser

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    parser = build_parser()
    examples = 0
    for block in re.findall(r"```(?:powershell|bash|sh)\s*\n(.*?)```", readme, re.DOTALL):
        for escaped in (r"\_", r"\[", r"\]", r"\*"):
            assert escaped not in block, "Markdown escapes inside code break copy/paste"
        for line in block.splitlines():
            command = line.strip()
            if command.startswith("python cli.py "):
                tokens = shlex.split(command)
                parser.parse_args(tokens[2:])
                examples += 1
    assert examples >= 20, "the operational CLI examples must remain covered"


def test_documented_source_paths_keep_the_python_package_name():
    for name in ("README.md", "CONTEXT.md"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert "`KnowWeave/" not in text, "Repository rename must not rename Python source paths"
        paths = re.findall(r"`(vector_lake/[a-z_]+\.py)`", text)
        assert paths, f"{name} must document the Python source entry points"
        for path in paths:
            assert (ROOT / path).is_file(), f"{name} references missing source {path}"


def test_documented_deployment_roots_use_the_repository_name():
    for name in ("README.md", "docs/CONFIGURATION.md", "docs/OPERATIONS.md"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert "C:/path/to/vector-lake" not in text
        assert "C:/path/to/KnowWeave" in text


def _documented_skill_arguments(name):
    text = (ROOT / "skills" / name / "SKILL.md").read_text(encoding="utf-8")
    blocks = re.findall(r"```json\s*\n(.*?)```", text, re.DOTALL)
    assert len(blocks) == 1, "A copyable scoped MCP argument example is required"
    arguments = json.loads(blocks[0])
    assert arguments["domain"] == "<authorized-domain>"
    assert arguments["allow_external_embedding"] is False
    assert arguments["profile"] == "auto"
    assert "General" in text and "授权" in text
    assert "<thought>" not in text
    return arguments


def test_query_skill_requires_explicit_authorized_domain():
    arguments = _documented_skill_arguments("vector-lake-query")
    assert arguments["query"] == "<core-concepts-and-necessary-aliases>"
    assert arguments["mode"] == "page"
    assert arguments["use_chunks"] is False


def test_search_skill_requires_explicit_authorized_domain():
    arguments = _documented_skill_arguments("vector-lake-search")
    assert arguments["query"] == "<task-query>"
    assert arguments["top_k"] == 5
    text = (ROOT / "skills" / "vector-lake-search" / "SKILL.md").read_text(encoding="utf-8")
    assert "(e.g. 'HIT', 'Clinical')" not in text


def test_operations_retrieval_examples_require_domain_without_execution():
    from vector_lake.cli_app import build_parser

    text = (ROOT / "docs" / "OPERATIONS.md").read_text(encoding="utf-8")
    commands = re.findall(r"`(python cli.py (?:search|query) [^`]+)`", text)
    assert len(commands) >= 2
    parser = build_parser()
    for command in commands:
        tokens = shlex.split(command)
        assert "--domain" in tokens
        args = parser.parse_args(tokens[2:])
        assert args.domain.strip()


def test_all_current_entrypoint_retrieval_examples_parse_with_domain():
    from vector_lake.cli_app import build_parser

    parser = build_parser()
    examples = 0
    for relative in ("README.md", "CONTEXT.md", "docs/OPERATIONS.md", "docs/DEVELOPMENT.md"):
        text = (ROOT / relative).read_text(encoding="utf-8")
        commands = re.findall(r"\bpython cli\.py (?:search|query) [^`\n]+", text)
        for command in commands:
            tokens = shlex.split(command)
            assert "--domain" in tokens, relative
            assert parser.parse_args(tokens[2:]).domain.strip()
            examples += 1
    assert examples >= 15
