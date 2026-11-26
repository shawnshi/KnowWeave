"""SEP-2640 (io.modelcontextprotocol/skills) provider wiring for KnowWeave MCP."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import anyio
from fastmcp.skills import Skill
from fastmcp.skills.provider import SkillCatalog, SkillCatalogProvider
from fastmcp.utilities.logging import get_logger

logger = get_logger(__name__)

SEP_2640_EXTENSION_ID = "io.modelcontextprotocol/skills"
DEFAULT_SKILLS_SCOPE = "mcp"
VALID_SKILLS_SCOPES = frozenset({"mcp", "all", "off"})

# Skills whose workflow requires local shell execution (`cli.py` / `scripts/ingest_runner.py`).
# Default MCP exposure (`scope="mcp"`) excludes these so remote/sandboxed MCP clients only
# receive self-contained MCP-native skills unless an operator sets VECTOR_LAKE_MCP_SKILLS_SCOPE=all.
CLI_DEPENDENT_SKILLS = frozenset(
    {
        "vector-lake-delete",
        "vector-lake-gc",
        "vector-lake-projections",
        "vector-lake-research",
        "vector-lake-review",
        "vector-lake-sync",
        "vector-lake-watchdog",
    }
)

ENV_SKILLS_SCOPE = "VECTOR_LAKE_MCP_SKILLS_SCOPE"
ENV_SKILLS_DIR = "VECTOR_LAKE_SKILLS_DIR"
ENV_SKILLS_RELOAD = "VECTOR_LAKE_MCP_SKILLS_RELOAD"

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSY = frozenset({"0", "false", "no", "off"})

_LOADED_SNAPSHOT: dict[str, Any] = {"status": "not_configured"}


def resolve_skills_scope(scope: str | None = None) -> str:
    """Resolve and validate the MCP skills exposure scope (`mcp`, `all`, or `off`)."""
    raw = os.environ.get(ENV_SKILLS_SCOPE, DEFAULT_SKILLS_SCOPE) if scope is None else scope
    if not isinstance(raw, str):
        raise ValueError(f"Invalid skills scope type: {type(raw).__name__}")
    normalized = raw.strip().lower()
    if not normalized:
        normalized = DEFAULT_SKILLS_SCOPE
    if normalized not in VALID_SKILLS_SCOPES:
        raise ValueError(
            f"Unsupported {ENV_SKILLS_SCOPE}={raw!r}; expected one of {sorted(VALID_SKILLS_SCOPES)}"
        )
    return normalized


def resolve_skills_reload(reload: bool | None = None) -> bool:
    """Resolve whether the skills directory should be re-scanned per request."""
    if reload is not None:
        if not isinstance(reload, bool):
            raise ValueError(f"Invalid skills reload type: {type(reload).__name__}")
        return reload
    raw = os.environ.get(ENV_SKILLS_RELOAD, "")
    normalized = raw.strip().lower()
    if not normalized:
        return False
    if normalized in _TRUTHY:
        return True
    if normalized in _FALSY:
        return False
    raise ValueError(
        f"Unsupported {ENV_SKILLS_RELOAD}={raw!r}; expected boolean flag"
    )


def resolve_skills_dir(skills_dir: str | Path | None = None) -> Path:
    """Resolve and validate the skills root directory, rejecting anchors and escapes."""
    if skills_dir is None:
        env_dir = os.environ.get(ENV_SKILLS_DIR, "").strip()
        candidate = Path(env_dir) if env_dir else Path(__file__).resolve().parents[1] / "skills"
    else:
        raw_str = str(skills_dir).strip()
        if not raw_str:
            raise ValueError("Skills directory path must not be empty")
        candidate = Path(raw_str)

    expanded = candidate.expanduser()
    if expanded.is_symlink():
        parent_resolved = expanded.parent.resolve()
        if not expanded.resolve().is_relative_to(parent_resolved):
            raise ValueError(f"Skills directory symlink escapes parent: {expanded}")
    resolved = expanded.resolve()
    if resolved == resolved.parent:
        raise ValueError(f"Skills directory cannot be a filesystem anchor: {resolved}")
    if not resolved.exists() or not resolved.is_dir():
        raise ValueError(f"Skills directory does not exist or is not a directory: {resolved}")
    return resolved


def _catalog_digest(skills: Sequence[Skill]) -> str:
    payload = [
        [skill.name, rel_path, file_info.digest]
        for skill in sorted(skills, key=lambda item: item.name)
        for rel_path, file_info in sorted(skill.manifest.files.items())
    ]
    encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def discover_skills(
    *,
    scope: str | None = None,
    skills_dir: str | Path | None = None,
) -> tuple[list[Skill], dict[str, Any]]:
    """Discover, validate, and scope-filter KnowWeave Agent Skills from disk."""
    active_scope = resolve_skills_scope(scope)
    if active_scope == "off":
        snapshot = {
            "status": "disabled",
            "protocol": SEP_2640_EXTENSION_ID,
            "scope": "off",
            "reload": False,
            "loaded_count": 0,
            "excluded_count": 0,
            "skill_names": [],
            "excluded_names": [],
            "catalog_digest": None,
        }
        return [], snapshot

    root = resolve_skills_dir(skills_dir)
    selected: list[Skill] = []
    excluded_names: list[str] = []

    for entry in sorted(root.iterdir(), key=lambda p: p.name):
        if entry.name.startswith("."):
            continue
        if entry.is_symlink():
            raise ValueError(f"Skill directory must not be a symlink: {entry.name}")
        if not entry.is_dir():
            continue
        skill_md = entry / "SKILL.md"
        if skill_md.is_symlink() or not skill_md.is_file():
            raise ValueError(f"Skill directory missing regular SKILL.md: {entry.name}")
        skill = Skill.from_directory(entry)
        if active_scope == "mcp" and skill.name in CLI_DEPENDENT_SKILLS:
            excluded_names.append(skill.name)
            continue
        selected.append(skill)

    skill_names = [skill.name for skill in selected]
    snapshot = {
        "status": "active",
        "protocol": SEP_2640_EXTENSION_ID,
        "scope": active_scope,
        "reload": False,
        "loaded_count": len(selected),
        "excluded_count": len(excluded_names),
        "skill_names": skill_names,
        "excluded_names": excluded_names,
        "catalog_digest": _catalog_digest(selected),
    }
    return selected, snapshot


class _ScopedReloadSkillCatalogProvider(SkillCatalogProvider):
    """Dynamic reloadable SkillCatalogProvider honoring scope and root validation."""

    def __init__(
        self,
        initial_skills: Sequence[Skill],
        *,
        scope: str,
        skills_dir: Path,
    ) -> None:
        self._scope = scope
        self._skills_dir = skills_dir
        self._last_good = list(initial_skills)
        super().__init__(self._last_good)

    async def load_skills(self) -> SkillCatalog:
        try:
            skills, _ = await anyio.to_thread.run_sync(
                lambda: discover_skills(scope=self._scope, skills_dir=self._skills_dir)
            )
            self._last_good = skills
        except ValueError as exc:
            logger.error("Keeping last valid KnowWeave skills catalog; %s", exc)
        return self._last_good

    def _owns_skill_resource(self, uri: str) -> bool:
        return uri.startswith("skill://")


def build_skills_provider(
    *,
    scope: str | None = None,
    skills_dir: str | Path | None = None,
    reload: bool | None = None,
    update_global_snapshot: bool = False,
) -> tuple[SkillCatalogProvider | None, dict[str, Any]]:
    """Build the FastMCP SEP-2640 skills provider and observation snapshot."""
    global _LOADED_SNAPSHOT

    active_scope = resolve_skills_scope(scope)
    active_reload = resolve_skills_reload(reload)
    skills, snapshot = discover_skills(scope=active_scope, skills_dir=skills_dir)
    snapshot["reload"] = active_reload if active_scope != "off" else False

    provider: SkillCatalogProvider | None
    if active_scope == "off":
        provider = None
    elif active_reload:
        resolved_dir = resolve_skills_dir(skills_dir)
        provider = _ScopedReloadSkillCatalogProvider(
            skills,
            scope=active_scope,
            skills_dir=resolved_dir,
        )
    else:
        provider = SkillCatalogProvider(skills)

    if update_global_snapshot:
        _LOADED_SNAPSHOT = dict(snapshot)

    return provider, snapshot


def get_loaded_skills_snapshot() -> dict[str, Any]:
    """Return a copy of the module-level SEP-2640 loaded skills snapshot without disk I/O."""
    snapshot = dict(_LOADED_SNAPSHOT)
    if "skill_names" in snapshot:
        snapshot["skill_names"] = list(snapshot["skill_names"])
    if "excluded_names" in snapshot:
        snapshot["excluded_names"] = list(snapshot["excluded_names"])
    return snapshot


def audit_skills_directory(skills_dir: str | Path | None = None) -> dict[str, Any]:
    """Read-only validation summary of all skill directories for ``doctor_vector_lake``."""
    root = resolve_skills_dir(skills_dir)
    mcp_native: list[str] = []
    cli_gated: list[str] = []
    errors: list[str] = []

    for entry in sorted(root.iterdir(), key=lambda p: p.name):
        if entry.name.startswith("."):
            continue
        if entry.is_symlink():
            errors.append(f"{entry.name}: symlinked skill directory")
            continue
        if not entry.is_dir():
            continue
        try:
            skill = Skill.from_directory(entry)
        except Exception as exc:  # noqa: BLE001 - report per-skill validation failure
            errors.append(f"{entry.name}: {exc}")
            continue
        if skill.name in CLI_DEPENDENT_SKILLS:
            cli_gated.append(skill.name)
        else:
            mcp_native.append(skill.name)

    return {
        "skills_dir": str(root),
        "total": len(mcp_native) + len(cli_gated),
        "mcp_native_count": len(mcp_native),
        "cli_gated_count": len(cli_gated),
        "mcp_native_names": mcp_native,
        "cli_gated_names": cli_gated,
        "errors": errors,
    }
