"""Host-runtime path conventions.

Vector Lake has no host adapter layer: the runtime recognises four environment
variables and otherwise falls back to the home directories of the hosts it has
historically been installed into.  Those fallbacks used to be literals spread
across four modules; they live here so that adding a host is a one-module change
and so that the precedence is testable.

Nothing in this module may import ``vector_lake`` at module scope: it is part of
the base layer and is imported by ``wiki_utils``.
"""

from __future__ import annotations

import os
from pathlib import Path

# Host home directories whose layout Vector Lake tolerates.  Order is
# documentation only; no caller depends on precedence between roots.
HOST_HOME_DIRS: tuple[str, ...] = ("~/.gemini", "~/.codex")

# Historical Gemini CLI install: the only place the CLI ever auto-loaded a
# dotenv file from, and therefore the only implicit GEMINI_API_KEY source.
LEGACY_ENV_FILE = "~/.gemini/.env"
# Historical MEMORY root; the last fallback in ``wiki_utils.get_memory_dir()``.
LEGACY_MEMORY_ROOT = "~/.gemini/MEMORY"
# Sandbox the Codex host writes ingest/orchestration payloads into.
CODEX_BRAIN_ROOT = "~/.codex/brain"


def _resolve(raw: str) -> Path:
    return Path(os.path.expanduser(raw)).resolve()


class PayloadRootError(ValueError):
    """``VECTOR_LAKE_PAYLOAD_ROOT`` would make the payload sandbox vacuous."""


def host_home_dirs() -> list[Path]:
    """Home directories of the hosts whose layout this runtime trusts."""
    return [_resolve(raw) for raw in HOST_HOME_DIRS]


def legacy_env_file() -> Path:
    """Dotenv file the CLI auto-loads (Gemini CLI convention)."""
    return _resolve(LEGACY_ENV_FILE)


def legacy_memory_root() -> Path:
    """Last-resort MEMORY root when neither the env var nor config.json is set."""
    return _resolve(LEGACY_MEMORY_ROOT)


def codex_brain_root() -> Path:
    """Codex host scratch tree that may hold agent payloads."""
    return _resolve(CODEX_BRAIN_ROOT)


def is_within_host_home(path: "str | os.PathLike[str]") -> bool:
    """True when ``path`` resolves under a known host home directory."""
    resolved = Path(path).resolve()
    return any(resolved.is_relative_to(root) for root in host_home_dirs())


def payload_sandbox_roots() -> list[Path]:
    """Built-in roots that may hold agent payload files.

    ``VECTOR_LAKE_PAYLOAD_ROOT`` is *additive* (see ``mcp_server._read_payload``):
    it extends this list rather than replacing it, so an operator override can no
    longer silently revoke the repository-local ``brain/`` sandbox.  Order is
    documentation only; the sandbox predicate treats every root identically.
    """
    from vector_lake import get_extension_root

    return [(get_extension_root() / "brain").resolve(), codex_brain_root()]


def payload_root_from_env() -> Path | None:
    """``VECTOR_LAKE_PAYLOAD_ROOT`` as a usable root, or ``None`` when unset.

    A configured root that resolves to a filesystem anchor (``C:\\``, ``/``, a UNC
    share root) or a broad host/user/repository/MEMORY root puts general host
    files "inside the sandbox" and disables the gate.  Reject those outright so
    the operator has to name a dedicated directory.
    """
    raw = (os.environ.get("VECTOR_LAKE_PAYLOAD_ROOT") or "").strip()
    if not raw:
        return None
    from vector_lake import get_extension_root

    resolved = _resolve(raw)
    disallowed = {
        Path(resolved.anchor),
        Path.home().resolve(),
        *host_home_dirs(),
        get_extension_root().resolve(),
    }
    mem_override = (os.environ.get("VECTOR_LAKE_MEMORY_DIR") or "").strip()
    if mem_override:
        disallowed.add(_resolve(mem_override))
    disallowed.add(legacy_memory_root())
    if resolved in disallowed:
        raise PayloadRootError(
            f"VECTOR_LAKE_PAYLOAD_ROOT={raw!r} resolves to {resolved}, which is too broad "
            "and would make the payload sandbox vacuous. Point it at a dedicated directory instead."
        )
    return resolved
