"""End-to-end SEP-2640 (io.modelcontextprotocol/skills) protocol and governance tests."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path

import pytest
from fastmcp import Client, FastMCP
from fastmcp.skills.client import SKILLS_EXTENSION_ID, SkillsClientExtension

from vector_lake import mcp_server, mcp_skills

ROOT = Path(__file__).resolve().parents[1]
SKILLS_ROOT = ROOT / "skills"


def _sha256_prefixed(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def _make_skills_server(*, scope: str | None = None, skills_dir: Path | None = None, reload: bool | None = None) -> FastMCP:
    provider, _ = mcp_skills.build_skills_provider(
        scope=scope,
        skills_dir=skills_dir,
        reload=reload,
    )
    return FastMCP("knowweave-skills-test", providers=[provider] if provider is not None else [])


def test_default_mcp_server_advertises_sep2640_and_exposes_13_mcp_skills():
    async def run():
        async with Client(mcp_server.mcp, extensions=[SkillsClientExtension()]) as client:
            caps = client.session.server_capabilities
            assert caps is not None
            assert caps.resources is not None
            assert caps.extensions is not None
            assert SKILLS_EXTENSION_ID in caps.extensions

            skills = await client.list_skills()
            tools = await client.list_tools()
            return skills, tools

    skills, tools = asyncio.run(run())
    assert len(tools) == 23

    all_on_disk = {p.parent.name for p in SKILLS_ROOT.glob("*/SKILL.md")}
    expected_names = all_on_disk - mcp_skills.CLI_DEPENDENT_SKILLS
    assert len(expected_names) == 13

    returned_names = {entry.frontmatter.name for entry in skills}
    assert returned_names == expected_names

    for entry in skills:
        name = entry.frontmatter.name
        assert entry.uri == f"skill://{name}/SKILL.md"
        assert entry.frontmatter.description.strip()
        disk_bytes = (SKILLS_ROOT / name / "SKILL.md").read_bytes()
        by_uri = {res.uri: res for res in entry.resources}
        assert entry.uri in by_uri
        assert by_uri[entry.uri].size == len(disk_bytes)
        assert by_uri[entry.uri].digest == _sha256_prefixed(disk_bytes)


def test_all_scope_exposes_20_skills_and_cli_skills_declare_compatibility():
    server = _make_skills_server(scope="all")

    async def run():
        async with Client(server, extensions=[SkillsClientExtension()]) as client:
            return await client.list_skills()

    skills = asyncio.run(run())
    all_on_disk = {p.parent.name for p in SKILLS_ROOT.glob("*/SKILL.md")}
    assert len(all_on_disk) == 20
    assert {entry.frontmatter.name for entry in skills} == all_on_disk

    by_name = {entry.frontmatter.name: entry for entry in skills}
    for cli_skill in mcp_skills.CLI_DEPENDENT_SKILLS:
        compat = by_name[cli_skill].frontmatter.compatibility
        assert isinstance(compat, str) and compat.strip(), f"{cli_skill} missing compatibility"


def test_off_scope_disables_sep2640_extension_and_resources():
    provider, snapshot = mcp_skills.build_skills_provider(scope="off")
    assert provider is None
    assert snapshot["status"] == "disabled"
    assert snapshot["loaded_count"] == 0

    server = _make_skills_server(scope="off")

    async def run():
        async with Client(server, extensions=[SkillsClientExtension()]) as client:
            caps = client.session.server_capabilities
            resources = await client.list_resources()
            return caps, resources

    caps, resources = asyncio.run(run())
    assert caps is not None
    assert SKILLS_EXTENSION_ID not in (caps.extensions or {})
    assert not [res for res in resources if str(res.uri).startswith("skill://")]


def test_skills_get_and_resources_read_work_with_and_without_extension():
    target_uri = "skill://vector-lake-query/SKILL.md"
    disk_bytes = (SKILLS_ROOT / "vector-lake-query" / "SKILL.md").read_bytes()
    expected_text = disk_bytes.decode("utf-8")

    async def run():
        async with Client(mcp_server.mcp, extensions=[SkillsClientExtension()]) as ext_client:
            got = await ext_client.get_skill(target_uri)
            ext_read = await ext_client.read_resource(target_uri)
        async with Client(mcp_server.mcp) as plain_client:
            plain_read = await plain_client.read_resource(target_uri)
        return got, ext_read, plain_read

    got, ext_read, plain_read = asyncio.run(run())
    assert got.uri == target_uri
    assert got.resources[0].digest == _sha256_prefixed(disk_bytes)
    assert ext_read[0].text == expected_text
    assert plain_read[0].text == expected_text
    assert _sha256_prefixed(plain_read[0].text.encode("utf-8")) == got.resources[0].digest


def test_negative_invalid_scope_reload_and_skills_dir_fail_closed(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="Unsupported VECTOR_LAKE_MCP_SKILLS_SCOPE"):
        mcp_skills.resolve_skills_scope("bogus")

    monkeypatch.setenv("VECTOR_LAKE_MCP_SKILLS_RELOAD", "maybe")
    with pytest.raises(ValueError, match="Unsupported VECTOR_LAKE_MCP_SKILLS_RELOAD"):
        mcp_skills.resolve_skills_reload()
    monkeypatch.delenv("VECTOR_LAKE_MCP_SKILLS_RELOAD", raising=False)

    with pytest.raises(ValueError, match="does not exist"):
        mcp_skills.resolve_skills_dir(tmp_path / "missing-dir")

    anchor = Path(tmp_path.anchor)
    with pytest.raises(ValueError, match="filesystem anchor"):
        mcp_skills.resolve_skills_dir(anchor)


def test_negative_path_traversal_resource_read_is_rejected():
    async def check_traversal(uri: str):
        async with Client(mcp_server.mcp) as client:
            await client.read_resource(uri)

    for bad_uri in (
        "skill://vector-lake-query/../../config.json",
        "skill://vector-lake-query/../vector-lake-sync/SKILL.md",
    ):
        with pytest.raises(Exception):
            asyncio.run(check_traversal(bad_uri))


def test_negative_symlink_inside_skill_directory_is_rejected(tmp_path):
    outside = tmp_path / "secret.txt"
    outside.write_text("top-secret", encoding="utf-8")
    skills_root = tmp_path / "skills"
    skill_dir = skills_root / "sample-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: sample-skill\ndescription: Sample skill.\n---\n\nBody\n",
        encoding="utf-8",
    )
    link_path = skill_dir / "leak.txt"
    try:
        os.symlink(outside, link_path)
    except OSError as exc:
        pytest.skip(f"Symlink creation unavailable in this environment: {exc}")

    with pytest.raises(ValueError, match="symlink"):
        mcp_skills.discover_skills(scope="all", skills_dir=skills_root)


def test_runtime_identity_and_doctor_observe_sep2640_skills():
    identity = json.loads(mcp_server.runtime_identity())
    ext = identity["skills_extension"]
    assert ext["status"] == "active"
    assert ext["protocol"] == mcp_skills.SEP_2640_EXTENSION_ID
    assert ext["scope"] == "mcp"
    assert ext["loaded_count"] == 13
    assert ext["excluded_count"] == 7
    assert len(ext["catalog_digest"]) == 64

    doctor_out = mcp_server.doctor_vector_lake()
    assert "[OK] MCP Skills (SEP-2640): scope=mcp reload=False exposed=13 (13 MCP-native, 7 CLI-gated)" in doctor_out
