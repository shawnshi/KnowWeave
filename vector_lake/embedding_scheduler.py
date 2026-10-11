"""Rate-aware Gemini embedding scheduler.

The scheduler treats embeddings as a resumable projection:
- never clear existing vectors just because an index rebuild starts;
- embed only missing nodes by default;
- batch by conservative token estimates;
- enforce RPM/TPM windows before requests;
- back off on provider quota errors without corrupting existing vectors.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import threading
import time
import uuid
import logging
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable

from filelock import FileLock, Timeout

from vector_lake import db_store
from vector_lake.wiki_utils import get_meta_dir

log = logging.getLogger(__name__)


DEFAULT_MODEL = "gemini-embedding-2"
DEFAULT_RPM = 3000
DEFAULT_TPM = 1_000_000
DEFAULT_DIMENSION = 3072


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return max(0.01, min(1.0, float(os.environ.get(name, default))))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class EmbeddingRateConfig:
    model: str = DEFAULT_MODEL
    rpm: int = DEFAULT_RPM
    tpm: int = DEFAULT_TPM
    utilization: float = 0.80
    max_batch_items: int = 100
    max_batch_tokens: int = 200_000
    max_chars_per_item: int = 15_000
    max_tokens_per_item: int = 7_500
    max_retries: int = 5
    dimension: int = DEFAULT_DIMENSION
    max_consecutive_failed_batches: int = 3

    @property
    def effective_rpm(self) -> int:
        return max(1, int(self.rpm * self.utilization))

    @property
    def effective_tpm(self) -> int:
        return max(1, int(self.tpm * self.utilization))


def load_embedding_rate_config() -> EmbeddingRateConfig:
    return EmbeddingRateConfig(
        model=os.environ.get("VECTOR_LAKE_EMBEDDING_MODEL", DEFAULT_MODEL),
        rpm=_env_int("VECTOR_LAKE_EMBEDDING_RPM", DEFAULT_RPM),
        tpm=_env_int("VECTOR_LAKE_EMBEDDING_TPM", DEFAULT_TPM),
        utilization=_env_float("VECTOR_LAKE_EMBEDDING_UTILIZATION", 0.80),
        max_batch_items=_env_int("VECTOR_LAKE_EMBEDDING_MAX_BATCH_ITEMS", 100),
        max_batch_tokens=_env_int("VECTOR_LAKE_EMBEDDING_MAX_BATCH_TOKENS", 200_000),
        max_chars_per_item=_env_int("VECTOR_LAKE_EMBEDDING_MAX_CHARS_PER_ITEM", 15_000),
        max_tokens_per_item=_env_int("VECTOR_LAKE_EMBEDDING_MAX_TOKENS_PER_ITEM", 7_500),
        max_retries=_env_int("VECTOR_LAKE_EMBEDDING_MAX_RETRIES", 5),
        dimension=_env_int("VECTOR_LAKE_EMBEDDING_DIMENSION", DEFAULT_DIMENSION),
        max_consecutive_failed_batches=_env_int("VECTOR_LAKE_EMBEDDING_MAX_CONSECUTIVE_FAILURES", 3),
    )


def embedding_text_for_node(node: dict[str, Any], max_chars: int = 15_000, body_text: str | None = None) -> str:
    """Embedding input text.  ``body_text`` supplies the page body, which is no
    longer carried in index.json."""
    aliases = node.get("aliases") or []
    aliases_text = " ".join(str(item) for item in aliases) if isinstance(aliases, list) else str(aliases)
    body = body_text if body_text is not None else node.get("raw_text")
    text = " ".join(
        str(part or "")
        for part in [node.get("title"), aliases_text, node.get("summary"), body]
    )
    return re.sub(r"\s+", " ", text).strip()[:max_chars]


def estimate_embedding_tokens(text: str) -> int:
    """Conservative tokenizer-free estimate for mixed Chinese/English content."""
    if not text:
        return 1
    cjk_chars = len(re.findall(r"[\u3400-\u9fff]", text))
    non_cjk = re.sub(r"[\u3400-\u9fff]", "", text)
    latin_words = len(re.findall(r"[A-Za-z0-9_]+", non_cjk))
    residual_chars = len(re.sub(r"[A-Za-z0-9_\s]", "", non_cjk))
    return max(1, int(cjk_chars + math.ceil(latin_words * 1.3) + math.ceil(residual_chars / 2)))


def clamp_to_token_budget(text: str, max_tokens: int) -> tuple[str, int]:
    """Trim one item so a single embedding request cannot exceed the model window.

    The character cap alone was unsafe for Chinese, where one character is
    roughly one token.
    """
    tokens = estimate_embedding_tokens(text)
    if tokens <= max_tokens:
        return text, tokens
    ratio = len(text) / max(1, tokens)
    truncated = text[: max(200, int(max_tokens * ratio))]
    while estimate_embedding_tokens(truncated) > max_tokens and len(truncated) > 200:
        truncated = truncated[: int(len(truncated) * 0.9)]
    return truncated, estimate_embedding_tokens(truncated)


def existing_embedding_ids() -> set[str]:
    """Node keys with a stored vector.  Raises if the vector table is unreadable:
    silently returning an empty set turned a broken table into "everything is
    missing" and triggered a full re-embed."""
    conn = db_store.get_connection()
    return {row["page_key"] for row in conn.execute("SELECT page_key FROM vec_embeddings")}


def page_bodies_for_keys(keys: list[str] | tuple[str, ...]) -> dict[str, str]:
    """Canonical ``raw_text`` bodies for the given page keys, read in one statement per chunk.

    ``index.json`` no longer carries page bodies, so a candidate's text has to come from
    SQLite.  Loading every entity body to embed a handful of nodes is what made an automatic
    backfill unaffordable: the periodic sweep runs every 15 minutes, and the corpus is now
    7 974 entities.  Chunked ``IN`` lists keep the statement inside SQLite's variable limit
    while staying a bounded number of round trips.

    This is the one implementation of that read.  The CLI backfill used to parse ``data_json``
    itself, which meant two body sources that could disagree about the same page while the
    staleness ledger compared their digests.
    """
    wanted = [str(key) for key in keys if key]
    if not wanted:
        return {}
    bodies: dict[str, str] = {}
    conn = db_store.get_connection()
    for start in range(0, len(wanted), 400):
        chunk = wanted[start:start + 400]
        placeholders = ",".join("?" for _ in chunk)
        # ``json_extract`` rather than parsing ``data_json`` in Python: the corpus is 22 MB of
        # JSON around 13 MB of text, and parsing the wrapper cost 2.59 s per pass against 0.39 s
        # for this (measured on 7 175 nodes, 2026-09-21) -- which matters because the staleness
        # check runs on every sweep.  ``json_valid`` keeps a malformed row from failing the whole
        # statement; it is skipped, exactly as the per-row decode used to skip it.
        rows = conn.execute(
            "SELECT f_page_key AS page_key, json_extract(data_json, '$.raw_text') AS body "
            f"FROM entities WHERE f_page_key IN ({placeholders}) AND json_valid(data_json)",
            chunk,
        )
        for row in rows:
            bodies[row["page_key"]] = str(row["body"] or "")
    return bodies


def embedding_input_digest(
    node: dict[str, Any],
    body_text: str | None,
    config: EmbeddingRateConfig | None = None,
) -> str:
    """Digest of every field :func:`embedding_text_for_node` reads, plus the two knobs that clip it.

    Digesting the *inputs* is equivalent to digesting the derived text -- the text is a pure
    function of these fields -- and avoids re-running the whitespace collapse and the token
    estimate over the whole corpus on every staleness check.  The clipping knobs are part of it so
    that lowering ``VECTOR_LAKE_EMBEDDING_MAX_CHARS_PER_ITEM`` marks the projection stale instead
    of silently leaving every vector built to the old limit.

    A field separator is written after every part so that two different field splits cannot
    collide into one digest.
    """
    config = config or load_embedding_rate_config()
    aliases = node.get("aliases") or []
    aliases_text = " ".join(str(item) for item in aliases) if isinstance(aliases, list) else str(aliases)
    body = body_text if body_text is not None else node.get("raw_text")
    digest = hashlib.sha256()
    for part in (
        node.get("title"),
        aliases_text,
        node.get("summary"),
        body,
        config.max_chars_per_item,
        config.max_tokens_per_item,
    ):
        digest.update(str(part if part is not None else "").encode("utf-8"))
        digest.update(b"\x1f")
    return digest.hexdigest()


def stale_embedding_keys(
    index_data: dict[str, Any],
    bodies: dict[str, str],
    config: EmbeddingRateConfig | None = None,
    recorded: dict[str, str] | None = None,
) -> set[str]:
    """Nodes whose stored vector was built from input that no longer matches, or was never stamped.

    Both cases have to be re-embedded: the digest can only be recorded by the same call that
    writes the vector, so an unstamped row cannot be verified without rebuilding it.  That is
    deliberate -- stamping the *current* input onto an old vector would assert a provenance the
    projection never established, which is the same mistake the model marker refuses to make.

    Measured 2026-09-21: re-embedding all 7 175 nodes and diffing bit for bit left 7 172 identical
    and 3 changed, and the provider is bit-deterministic -- so the 3 were stale inputs, not noise.
    That is the class this finds, and it was previously findable only by paying for the re-embed.
    """
    config = config or load_embedding_rate_config()
    recorded = db_store.embedding_input_digests() if recorded is None else recorded
    stale: set[str] = set()
    for node_key, node in (index_data.get("nodes") or {}).items():
        if recorded.get(node_key) != embedding_input_digest(node, bodies.get(node_key), config):
            stale.add(node_key)
    return stale


def embedding_coverage(index_data: dict[str, Any]) -> dict[str, int]:
    node_keys = set((index_data.get("nodes") or {}).keys())
    existing = existing_embedding_ids()
    return {
        "nodes": len(node_keys),
        "embedded": len(node_keys & existing),
        "missing": len(node_keys - existing),
        "stale": len(existing - node_keys),
    }


def _candidate_items(
    index_data: dict[str, Any],
    *,
    include_existing: bool = False,
    limit: int | None = None,
    config: EmbeddingRateConfig | None = None,
    bodies: dict[str, str] | None = None,
    body_loader: Callable[[list[str]], dict[str, str]] | None = None,
    extra_keys: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Nodes that still need a vector, in a stable key order.

    ``body_loader`` is the bounded path: a caller that cannot afford to materialise all
    7 974 page bodies -- the periodic sweep -- hands in a loader and only the keys about to
    be embedded are read.  Passing ``bodies`` keeps the whole-corpus behaviour for callers
    that already hold them (the CLI/MCP backfill).

    ``extra_keys`` forces a rebuild of nodes that already have a vector, which is how a stale
    input is repaired; see :func:`stale_embedding_keys`.
    """
    config = config or load_embedding_rate_config()
    nodes = index_data.get("nodes") or {}
    existing = existing_embedding_ids() if not include_existing else set()
    must_refresh = set(extra_keys or ())
    pending = [key for key in sorted(nodes) if key not in existing or key in must_refresh]
    if limit is not None:
        pending = pending[: max(1, int(limit))]
    if body_loader is not None and bodies is None:
        bodies = body_loader(pending)
    bodies = bodies or {}
    items: list[dict[str, Any]] = []
    for node_key in pending:
        node = nodes[node_key]
        digest = embedding_input_digest(node, bodies.get(node_key), config)
        text = embedding_text_for_node(
            node, max_chars=config.max_chars_per_item, body_text=bodies.get(node_key)
        )
        text, tokens = clamp_to_token_budget(text, config.max_tokens_per_item)
        if not text:
            continue
        items.append({"node_key": node_key, "text": text, "tokens": tokens, "content_digest": digest})
    if limit is not None:
        items = items[: max(1, int(limit))]
    return items


def _batch_items(items: list[dict[str, Any]], config: EmbeddingRateConfig) -> list[list[dict[str, Any]]]:
    batches: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_tokens = 0
    batch_token_cap = max(1, min(config.max_batch_tokens, config.effective_tpm))
    for item in items:
        item_tokens = max(1, int(item["tokens"]))
        if current and (
            len(current) >= config.max_batch_items
            or current_tokens + item_tokens > batch_token_cap
        ):
            batches.append(current)
            current = []
            current_tokens = 0
        current.append(item)
        current_tokens += item_tokens
    if current:
        batches.append(current)
    return batches


class MinuteRateLimiter:
    """Cross-process rolling-window limiter backed by canonical SQLite."""

    def __init__(self, config: EmbeddingRateConfig):
        self.config = config

    def reserve(self, request_tokens: int, deadline: float | None = None, durable: bool = True):
        """Claim a slot in the rolling window.

        ``deadline`` is a ``time.monotonic()`` instant.  Without one the limiter
        waits as long as the window requires, which is right for batch backfills;
        with one, an unaffordable wait raises :class:`EmbeddingBudgetExceeded`
        rather than sleeping past the caller's own deadline.

        ``durable=False`` makes the check read-only, for requests that run on a
        read path.  A durable reservation needs ``BEGIN IMMEDIATE``, so recording
        every query embedding put the write lock back on a path that was just
        cleaned of it -- a query would queue behind whatever ingest was writing.
        The cost is that interactive requests are not counted, so the window can be
        exceeded by however many of them land inside it.  That is a deliberate
        trade at the observed scale (a handful of queries per minute against
        ``rpm`` 3000), and the provider's own 429 is now bounded by the caller's
        budget instead of a flat 60 s sleep.
        """
        request_tokens = max(1, int(request_tokens))
        if request_tokens > self.config.effective_tpm:
            raise ValueError(
                f"Embedding request tokens {request_tokens} exceed effective TPM {self.config.effective_tpm}"
            )
        db_store.init_db()
        while True:
            now = time.time()
            cutoff = now - 60.0
            wait_seconds = 0.0
            conn = db_store.get_connection()
            if durable:
                with db_store.transaction():
                    conn.execute(
                        "DELETE FROM embedding_rate_reservations WHERE reserved_at <= ?",
                        (cutoff,),
                    )
                    row = conn.execute(
                        "SELECT COUNT(*) AS requests, COALESCE(SUM(token_count), 0) AS tokens, "
                        "MIN(reserved_at) AS oldest FROM embedding_rate_reservations"
                    ).fetchone()
                    requests = int(row["requests"] or 0)
                    tokens = int(row["tokens"] or 0)
                    if (
                        requests + 1 <= self.config.effective_rpm
                        and tokens + request_tokens <= self.config.effective_tpm
                    ):
                        conn.execute(
                            "INSERT INTO embedding_rate_reservations "
                            "(reservation_id, reserved_at, token_count) VALUES (?, ?, ?)",
                            (uuid.uuid4().hex, now, request_tokens),
                        )
                        return
                    oldest = float(row["oldest"] or now)
                    wait_seconds = max(0.01, oldest + 60.0 - now + 0.05)
            else:
                row = conn.execute(
                    "SELECT COUNT(*) AS requests, COALESCE(SUM(token_count), 0) AS tokens, "
                    "MIN(reserved_at) AS oldest FROM embedding_rate_reservations "
                    "WHERE reserved_at > ?",
                    (cutoff,),
                ).fetchone()
                requests = int(row["requests"] or 0)
                tokens = int(row["tokens"] or 0)
                if (
                    requests + 1 <= self.config.effective_rpm
                    and tokens + request_tokens <= self.config.effective_tpm
                ):
                    return
                oldest = float(row["oldest"] or now)
                wait_seconds = max(0.01, oldest + 60.0 - now + 0.05)
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if wait_seconds > remaining:
                    raise EmbeddingBudgetExceeded(
                        f"rate-limit window would hold this request for {wait_seconds:.1f}s "
                        f"but only {max(0.0, remaining):.1f}s of the caller's budget remains"
                    )
            time.sleep(wait_seconds)


class EmbeddingResponseError(RuntimeError):
    """Raised when the provider response cannot be safely mapped to inputs."""


class EmbeddingBudgetExceeded(RuntimeError):
    """The caller's deadline left no room for the wait this request needs.

    Distinct from a provider error: work could not start in time, a transport
    exceeded the budget, a late result was rejected, or a retry was refused.  Callers that hold a deadline of their own (the query
    path runs inside an MCP tool call) degrade on this instead of parking.
    """


EMBEDDING_TRANSPORT_ENV = "VECTOR_LAKE_EMBEDDING_TRANSPORT"
REST_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:batchEmbedContents"


def embedding_transport() -> str:
    """Default urllib REST, opt-in pooled REST, or explicitly selected SDK.

    A selected backend's errors never trigger automatic transport fallback.
    """
    value = str(os.environ.get(EMBEDDING_TRANSPORT_ENV, "rest") or "rest").strip().lower()
    return value if value in {"rest", "rest-httpx", "sdk"} else "rest"


def _embedding_deadline(budget_seconds: float | None) -> float | None:
    if budget_seconds is None:
        return None
    budget = float(budget_seconds)
    if not math.isfinite(budget):
        raise ValueError("Embedding budget must be finite")
    deadline = time.monotonic() + budget
    _remaining_budget(deadline)
    return deadline


def _remaining_budget(deadline: float | None) -> float | None:
    if deadline is None:
        return None
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise EmbeddingBudgetExceeded("Embedding caller deadline exhausted")
    return remaining


def _transport_timeout(deadline: float | None) -> float:
    configured = _env_int("VECTOR_LAKE_EMBEDDING_TIMEOUT_MS", 30_000) / 1000.0
    remaining = _remaining_budget(deadline)
    return configured if remaining is None else min(configured, remaining)


def _rest_embed_contents(contents: list[str], config, *, deadline: float | None = None) -> list[list[float]]:
    """One ``batchEmbedContents`` POST, with the SDK's error semantics.

    Raises on any non-200 so the caller's retry/budget loop behaves exactly as it did with the
    SDK: the loop classifies a quota error from the message text, and a 429 body carries it.
    """
    import json as _json
    import urllib.error
    import urllib.request

    api_key = str(os.environ.get("GEMINI_API_KEY") or "")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not set")
    payload = _json.dumps(
        {
            "requests": [
                {"model": f"models/{config.model}", "content": {"parts": [{"text": str(text)}]}}
                for text in contents
            ]
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        REST_ENDPOINT.format(model=config.model),
        data=payload,
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=_transport_timeout(deadline)) as response:
            body = response.read()
        _remaining_budget(deadline)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:  # noqa: BLE001 - the status is the useful part
            pass
        raise RuntimeError(f"HTTP {exc.code} from the embedding endpoint: {detail}") from exc
    payload_json = _json.loads(body.decode("utf-8"))
    embeddings = payload_json.get("embeddings") or []
    values_list = [list(item.get("values") or []) for item in embeddings]
    if len(values_list) != len(contents):
        raise EmbeddingResponseError(
            f"Embedding response count mismatch: expected {len(contents)}, got {len(values_list)}"
        )
    for index, values in enumerate(values_list):
        if len(values) != config.dimension:
            raise EmbeddingResponseError(
                f"Embedding dimension mismatch at position {index}: "
                f"expected {config.dimension}, got {len(values)}"
            )
    _remaining_budget(deadline)
    return values_list


def _load_httpx():
    try:
        import httpx
    except ModuleNotFoundError as exc:
        if exc.name == "httpx":
            raise RuntimeError("rest-httpx requires the optional httpx package") from exc
        # A broken installation/transitive import is not a missing optional backend.
        raise
    return httpx


def _httpx_route(endpoint):
    import urllib.request
    from urllib.parse import urlsplit

    try:
        target = urlsplit(endpoint)
        if target.scheme not in {"http", "https"} or not target.hostname or target.username or target.password:
            raise ValueError("invalid target")
        origin = (target.scheme, target.hostname.lower(), target.port or (443 if target.scheme == "https" else 80))
        proxy = None if urllib.request.proxy_bypass(target.netloc) else urllib.request.getproxies().get(target.scheme)
        if proxy:
            if not isinstance(proxy, str):
                raise ValueError("invalid proxy")
            if "://" not in proxy:
                proxy = "http://" + proxy
            parsed = urlsplit(proxy)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
                raise ValueError("incompatible proxy")
            _ = parsed.port
        return origin, proxy
    except (ValueError, TypeError) as exc:
        raise RuntimeError("Embedding endpoint/proxy configuration is incompatible with rest-httpx") from exc


def _create_httpx_client(proxy, timeout):
    import ssl
    from http.cookiejar import CookieJar, DefaultCookiePolicy

    httpx = _load_httpx()

    class RejectCookies(DefaultCookiePolicy):
        def set_ok(self, cookie, request):
            return False

        def return_ok(self, cookie, request):
            return False

    transport = httpx.HTTPTransport(
        verify=ssl.create_default_context(), proxy=proxy, trust_env=False,
        limits=httpx.Limits(max_connections=8, max_keepalive_connections=8, keepalive_expiry=5),
        http1=True, http2=False, retries=0,
    )
    try:
        return httpx.Client(
            transport=transport, trust_env=False, follow_redirects=False,
            timeout=timeout, cookies=CookieJar(policy=RejectCookies()),
        )
    except BaseException:
        try:
            transport.close()
        except Exception as exc:  # noqa: BLE001 - preserve original constructor error
            log.warning("Embedding HTTPX constructor cleanup failed (%s).", type(exc).__name__)
        raise


def _httpx_embed_contents(contents: list[str], config, *, deadline: float | None = None) -> list[list[float]]:
    """One pooled REST attempt; no redirects, cookies, or hidden retries."""
    import json as _json

    api_key = str(os.environ.get("GEMINI_API_KEY") or "")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not set")
    endpoint = REST_ENDPOINT.format(model=config.model)
    origin, proxy = _httpx_route(endpoint)
    httpx = _load_httpx()
    factory = _create_httpx_client
    base_timeout = _env_int("VECTOR_LAKE_EMBEDDING_TIMEOUT_MS", 30_000) / 1000.0
    key = (factory, origin, proxy, base_timeout)
    payload = _json.dumps({
        "requests": [
            {"model": f"models/{config.model}", "content": {"parts": [{"text": str(text)}]}}
            for text in contents
        ]
    }).encode("utf-8")
    try:
        with (
            _HTTPX_CLIENTS.borrow(key, lambda: factory(proxy, base_timeout), deadline=deadline) as client,
            client.stream(
                "POST", endpoint, content=payload,
                headers={"Content-Type": "application/json", "x-goog-api-key": api_key, "Accept-Encoding": "identity"},
                timeout=_transport_timeout(deadline),
            ) as response,
        ):
            body = response.read()
            if not 200 <= response.status_code < 300:
                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    # No provider body/URL echo: allowlisted quota markers suffice for
                    # the existing scheduler classifier, even for an echoed credential.
                    detail = body[:300].decode("utf-8", "replace").lower()
                    quota = " quota" if "quota" in detail or "resource_exhausted" in detail else ""
                    raise RuntimeError(f"HTTP {response.status_code} from the embedding endpoint:{quota}") from exc
    except httpx.RequestError as exc:
        raise RuntimeError(f"Embedding HTTPX transport failed ({type(exc).__name__})") from exc
    _remaining_budget(deadline)
    payload_json = _json.loads(body.decode("utf-8"))
    embeddings = payload_json.get("embeddings") or []
    values_list = [list(item.get("values") or []) for item in embeddings]
    if len(values_list) != len(contents):
        raise EmbeddingResponseError(
            f"Embedding response count mismatch: expected {len(contents)}, got {len(values_list)}"
        )
    for index, values in enumerate(values_list):
        if len(values) != config.dimension:
            raise EmbeddingResponseError(
                f"Embedding dimension mismatch at position {index}: "
                f"expected {config.dimension}, got {len(values)}"
            )
    _remaining_budget(deadline)
    return values_list


def _provider_contents(contents: list[str]) -> list[Any]:
    from google.genai import types

    return [
        types.UserContent(parts=[types.Part.from_text(text=str(content))])
        for content in contents
    ]


def _create_client():
    from google import genai
    from google.genai import types

    timeout_ms = _env_int("VECTOR_LAKE_EMBEDDING_TIMEOUT_MS", 30_000)
    return genai.Client(http_options=types.HttpOptions(
        timeout=timeout_ms, retry_options=types.HttpRetryOptions(attempts=1),
    ))


class EmbeddingClientLifecycleError(RuntimeError):
    """Client acquisition is unavailable (shutdown, fork, or invalidated construction)."""


@dataclass(eq=False)
class _ClientGeneration:
    key: tuple
    epoch: int
    state: str = "constructing"
    client: Any = None
    borrowers: int = 0


class _ClientManager:
    """Two live generations including constructors and unsuccessful closes.

    The manager owns only synchronous clients it constructs, never injected clients.
    Construction/close may block: the deadline is cooperative, not cancellation.
    """

    def __init__(self):
        self.pid = os.getpid()
        self.condition = threading.Condition()
        self.epoch = 0
        self.stopped = False
        self.generations: list[_ClientGeneration] = []

    def _check_pid(self):
        # Must precede even acquiring the inherited condition after a fork.
        if self.pid != os.getpid():
            raise EmbeddingClientLifecycleError("Inherited embedding manager cannot be reused; use spawn")

    def _close(self, generation):
        self._check_pid()
        try:
            generation.client.close()
        except Exception as exc:  # noqa: BLE001 - never mask a request's original error
            log.warning("Embedding client close failed (%s); resource retained as close_failed", type(exc).__name__)
            with self.condition:
                generation.state = "close_failed"
                self.condition.notify_all()
        else:
            with self.condition:
                self.generations.remove(generation)
                self.condition.notify_all()

    def invalidate(self, *, shutdown=False, reopen=False):
        self._check_pid()
        closing = []
        with self.condition:
            self.epoch += 1
            if shutdown:
                self.stopped = True
            elif reopen:
                self.stopped = False
            for generation in self.generations:
                if generation.state == "active":
                    generation.state = "retired"
                if generation.state == "retired" and not generation.borrowers:
                    generation.state = "closing"
                    closing.append(generation)
            self.condition.notify_all()
        for generation in closing:
            self._close(generation)

    @contextmanager
    def borrow(self, key, factory, *, deadline=None):
        self._check_pid()
        wait_deadline = time.monotonic() + _transport_timeout(deadline)
        while True:
            closing = []
            reservation = None
            with self.condition:
                if self.stopped:
                    raise EmbeddingClientLifecycleError("Embedding clients are shut down")
                _remaining_budget(wait_deadline)
                selected = next((g for g in self.generations if g.state == "active" and g.key == key), None)
                if selected is not None:
                    selected.borrowers += 1
                    break
                for generation in self.generations:
                    if generation.state == "active":
                        generation.state = "retired"
                    if generation.state == "retired" and not generation.borrowers:
                        generation.state = "closing"
                        closing.append(generation)
                if not closing and len(self.generations) < 2 and not any(
                    g.state == "constructing" for g in self.generations
                ):
                    reservation = _ClientGeneration(key, self.epoch)
                    self.generations.append(reservation)
                elif not closing:
                    self.condition.wait(timeout=_remaining_budget(wait_deadline))
            for generation in closing:
                self._close(generation)
            if reservation is None:
                continue
            try:
                reservation.client = factory()
            except BaseException:
                self._check_pid()
                with self.condition:
                    self.generations.remove(reservation)
                    self.condition.notify_all()
                raise
            self._check_pid()
            with self.condition:
                invalidated = self.stopped or reservation.epoch != self.epoch
                expired = time.monotonic() >= wait_deadline
                if invalidated or expired:
                    reservation.state = "closing"
                else:
                    reservation.state = "active"
                    reservation.borrowers = 1
                    selected = reservation
                self.condition.notify_all()
            if invalidated or expired:
                self._close(reservation)
                if invalidated:
                    raise EmbeddingClientLifecycleError("Embedding client construction was invalidated")
                raise EmbeddingBudgetExceeded("Embedding client construction exceeded acquisition budget")
            break
        assert selected is not None
        try:
            _remaining_budget(deadline)
            yield selected.client
        finally:
            self._check_pid()
            with self.condition:
                selected.borrowers -= 1
                close = selected.state == "retired" and not selected.borrowers
                if close:
                    selected.state = "closing"
                self.condition.notify_all()
            if close:
                self._close(selected)


_SDK_CLIENTS = _ClientManager()
_HTTPX_CLIENTS = _ClientManager()


def _borrow_sdk_client(*, deadline=None):
    factory = _create_client
    key = (factory, _env_int("VECTOR_LAKE_EMBEDDING_TIMEOUT_MS", 30_000))
    return _SDK_CLIENTS.borrow(key, factory, deadline=deadline)


def reset_client_cache(*, reopen: bool = False) -> None:
    """Retire clients without cancelling active leases.

    No-argument reset never reopens shutdown. ``reopen=True`` explicitly starts a
    new lifecycle for tests/embedded hosts, after the owner has stopped old work.
    SDK auth/endpoint/proxy and TLS trust changes require this explicit reset.
    """
    _SDK_CLIENTS.invalidate(reopen=reopen)
    _HTTPX_CLIENTS.invalidate(reopen=reopen)


def shutdown_embedding_clients() -> None:
    """Stop new borrowing; close idle clients, drain active ones on last release.

    Owns synchronous SDK resources only, not SDK .aio or caller-injected clients.
    Shutdown is idempotent and does not wait for or cancel active requests.
    """
    _SDK_CLIENTS.invalidate(shutdown=True)
    _HTTPX_CLIENTS.invalidate(shutdown=True)


def _sdk_prewarm_safe() -> bool:
    import sys

    base_urls = sys.modules.get("google.genai._base_url")
    if base_urls is not None and (
        getattr(base_urls, "_default_base_gemini_url", None)
        or getattr(base_urls, "_default_base_vertex_url", None)
    ):
        return False
    # Local google-genai client.py/_api_client.py: enterprise can acquire ADC at
    # construction; DebugConfig can create replay/record files. On-demand auth is unchanged.
    excluded = (
        "GOOGLE_GENAI_USE_VERTEXAI", "GOOGLE_GENAI_USE_ENTERPRISE",
        "GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION", "GOOGLE_APPLICATION_CREDENTIALS",
        "GOOGLE_GENAI_CLIENT_MODE", "GOOGLE_GENAI_REPLAYS_DIRECTORY", "GOOGLE_GENAI_REPLAY_ID",
        "GOOGLE_GEMINI_BASE_URL", "GOOGLE_VERTEX_BASE_URL",
    )
    return bool(os.environ.get("GEMINI_API_KEY")) and embedding_transport() == "sdk" and not any(
        os.environ.get(name) for name in excluded
    ) and _create_client is _DEFAULT_SDK_FACTORY


_DEFAULT_SDK_FACTORY = _create_client


def prewarm_client() -> bool:
    """Best-effort constructor warmup in restricted API-key mode; no REST warmup."""
    if not _sdk_prewarm_safe():
        return False
    try:
        with _borrow_sdk_client():
            return True
    except Exception as exc:  # noqa: BLE001 - prewarming must never fail a server start
        log.warning("Embedding client prewarm failed (%s).", type(exc).__name__)
        return False


def start_prewarm_thread() -> threading.Thread | None:
    """Start best-effort SDK warmup only when safe and not shut down."""
    if str(os.environ.get("VECTOR_LAKE_EMBEDDING_PREWARM", "")).strip().lower() in {"off", "0", "false"}:
        return None
    if not _sdk_prewarm_safe():
        return None
    _SDK_CLIENTS._check_pid()
    with _SDK_CLIENTS.condition:
        if _SDK_CLIENTS.stopped:
            return None
    thread = threading.Thread(
        target=prewarm_client, name="embedding-client-prewarm", daemon=True
    )
    thread.start()
    return thread


def _validated_response_values(response: Any, expected_count: int, dimension: int) -> list[list[float]]:
    embeddings = list(getattr(response, "embeddings", []) or [])
    if len(embeddings) != expected_count:
        raise EmbeddingResponseError(
            f"Embedding response count mismatch: expected {expected_count}, got {len(embeddings)}"
        )
    values_list: list[list[float]] = []
    for position, embedding in enumerate(embeddings):
        values = list(getattr(embedding, "values", None) or [])
        if len(values) != dimension:
            raise EmbeddingResponseError(
                f"Embedding dimension mismatch at position {position}: expected {dimension}, got {len(values)}"
            )
        values_list.append(values)
    return values_list


def _request_embeddings(
    client: Any,
    contents: list[str],
    request_tokens: int,
    config: EmbeddingRateConfig,
    limiter: MinuteRateLimiter,
    budget_seconds: float | None = None,
    durable_reservation: bool = True,
    *,
    deadline: float | None = None,
) -> list[list[float]]:
    """Embed ``contents``, optionally inside a caller-supplied time budget.

    A quota error used to sleep a flat 60 s per retry, which on its own exceeds
    the MCP client's 60 s call ceiling: the server would keep working on a request
    whose caller had already given up, and the caller saw a timeout instead of the
    real cause.  With ``budget_seconds`` the deadline is checked before every
    sleep, so an unaffordable wait surfaces as :class:`EmbeddingBudgetExceeded`
    and the search degrades with ``vector_notes`` explaining why.
    """
    last_error: Exception | None = None
    deadline = _embedding_deadline(budget_seconds) if deadline is None else deadline
    _remaining_budget(deadline)
    transport = embedding_transport()
    if transport == "sdk" and client is None:
        with _borrow_sdk_client(deadline=deadline) as owned_client:
            return _request_embeddings(
                owned_client, contents, request_tokens, config, limiter,
                durable_reservation=durable_reservation, deadline=deadline,
            )
    # Built lazily: this import is the 3.5 s the REST transport exists to avoid.
    provider_contents = _provider_contents(contents) if transport == "sdk" else []
    for attempt in range(config.max_retries + 1):
        try:
            _remaining_budget(deadline)
            limiter.reserve(request_tokens, deadline=deadline, durable=durable_reservation)
            _remaining_budget(deadline)
            if transport == "rest":
                values = _rest_embed_contents(contents, config, deadline=deadline)
            elif transport == "rest-httpx":
                values = _httpx_embed_contents(contents, config, deadline=deadline)
            else:
                timeout_ms = int(_transport_timeout(deadline) * 1000)
                if timeout_ms < 1:
                    raise EmbeddingBudgetExceeded("Less than one millisecond remains for SDK transport")
                response = client.models.embed_content(
                    model=config.model, contents=provider_contents,
                    config={"http_options": {
                        "timeout": timeout_ms, "retry_options": {"attempts": 1},
                    }},
                )
                values = _validated_response_values(response, len(contents), config.dimension)
            _remaining_budget(deadline)
            return values
        except EmbeddingBudgetExceeded:
            raise
        except Exception as exc:
            last_error = exc
            try:
                _remaining_budget(deadline)
            except EmbeddingBudgetExceeded as expired:
                raise expired from exc
            if attempt >= config.max_retries:
                break
            message = str(exc)
            is_quota = "429" in message or "RESOURCE_EXHAUSTED" in message or "quota" in message.lower()
            delay = 60.0 if is_quota else min(60.0, 2.0 ** attempt)
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if delay > remaining:
                    raise EmbeddingBudgetExceeded(
                        f"retry after {type(exc).__name__} needs {delay:.0f}s but only "
                        f"{max(0.0, remaining):.1f}s of the caller's budget remains: {message[:200]}"
                    ) from exc
            time.sleep(delay)
    assert last_error is not None
    raise last_error


def embed_texts(
    texts: list[str],
    budget_seconds: float | None = None,
    durable_reservation: bool = True,
) -> list[list[float]]:
    """Shared validated embedding entrypoint for small runtime requests.

    ``budget_seconds`` is one cooperative deadline starting before preparation:
    transport timeouts use its remainder, and expired results are rejected.
    Synchronous SDK initialization/DNS and inactivity-based socket timeouts are
    not a hard wall-clock cancellation boundary. Omit the caller budget for
    batch work where waiting is acceptable; per-attempt HTTP timeout still applies.

    ``durable_reservation=False`` keeps the request off the write lock, for
    callers on a read path; see :meth:`MinuteRateLimiter.reserve`.
    """
    if not texts or not os.environ.get("GEMINI_API_KEY"):
        return []
    deadline = _embedding_deadline(budget_seconds)
    config = load_embedding_rate_config()
    normalized = [str(text)[:config.max_chars_per_item] for text in texts]
    tokens = sum(estimate_embedding_tokens(text) for text in normalized)
    _remaining_budget(deadline)
    return _request_embeddings(
        None,
        normalized,
        tokens,
        config,
        MinuteRateLimiter(config),
        budget_seconds=budget_seconds,
        durable_reservation=durable_reservation,
        deadline=deadline,
    )


def embedding_backfill(
    index_data: dict[str, Any],
    dry_run: bool = True,
    limit: int | None = None,
    include_existing: bool = False,
    bodies: dict[str, str] | None = None,
    body_loader: Callable[[list[str]], dict[str, str]] | None = None,
    budget_seconds: float | None = None,
    extra_keys: set[str] | None = None,
) -> dict[str, Any]:
    """Backfill missing vector embeddings under Gemini RPM/TPM limits.

    ``body_loader`` lets a recurring caller embed a bounded batch without reading the whole
    body corpus; see :func:`_candidate_items`.

    ``budget_seconds`` bounds the *wait* for one batch.  A recurring caller has to pass it:
    a quota error sleeps a flat 60 s per retry, so five retries on two batches is 600 s inside
    a sweep that is meant to run every 900 s.  The overshoot surfaces as a failed batch, which
    the caller reports and the next sweep retries -- the same shape as any other provider
    outage, and cheaper than holding the loop.
    """
    config = load_embedding_rate_config()
    coverage_before = embedding_coverage(index_data)
    items = _candidate_items(
        index_data,
        include_existing=include_existing,
        limit=limit,
        config=config,
        bodies=bodies,
        body_loader=body_loader,
        extra_keys=extra_keys,
    )
    batches = _batch_items(items, config)
    estimated_tokens = sum(int(item["tokens"]) for item in items)
    plan = {
        "dry_run": dry_run,
        "model": config.model,
        "rpm": config.rpm,
        "tpm": config.tpm,
        "utilization": config.utilization,
        "effective_rpm": config.effective_rpm,
        "effective_tpm": config.effective_tpm,
        "candidates": len(items),
        "estimated_tokens": estimated_tokens,
        "estimated_requests": len(batches),
        "max_chars_per_item": config.max_chars_per_item,
        "max_tokens_per_item": config.max_tokens_per_item,
        "coverage_before": coverage_before,
        "embedded": 0,
        "failed_batches": 0,
    }
    if dry_run or not items:
        return plan
    if not os.environ.get("GEMINI_API_KEY"):
        plan["skipped"] = "GEMINI_API_KEY not set"
        return plan

    lock = FileLock(str(get_meta_dir() / ".embedding-backfill.lock"))
    try:
        lock.acquire(timeout=0)
    except Timeout:
        plan["skipped"] = "another embedding backfill is already running"
        return plan

    run_id = uuid.uuid4().hex
    plan["run_id"] = run_id
    db_store.start_embedding_run(run_id, config.model, len(items))
    last_error = ""
    consecutive_failures = 0
    try:
        limiter = MinuteRateLimiter(config)
        for batch in batches:
            contents = [item["text"] for item in batch]
            batch_tokens = sum(int(item["tokens"]) for item in batch)
            try:
                values_list = _request_embeddings(
                    None, contents, batch_tokens, config, limiter, budget_seconds=budget_seconds
                )
                for item, values in zip(batch, values_list, strict=True):
                    db_store.upsert_embedding(
                        item["node_key"], values, content_digest=item["content_digest"]
                    )
                    plan["embedded"] += 1
                consecutive_failures = 0
            except Exception as exc:
                last_error = str(exc)[:500]
                plan["failed_batches"] += 1
                plan["last_error"] = last_error
                consecutive_failures += 1
            db_store.update_embedding_run(
                run_id,
                plan["embedded"],
                plan["failed_batches"],
                last_error,
            )
            if consecutive_failures >= config.max_consecutive_failed_batches:
                plan["stopped"] = "consecutive batch failure guard reached"
                break
        status = "completed" if not plan["failed_batches"] else ("partial" if plan["embedded"] else "failed")
        db_store.finish_embedding_run(
            run_id,
            status,
            plan["embedded"],
            plan["failed_batches"],
            last_error,
        )
        if plan["embedded"]:
            # Only when rows were written: that is the one moment the stored projection and this
            # marker are guaranteed to describe the same model.
            db_store.record_embedding_projection(config.model, config.dimension)
        plan["coverage_after"] = embedding_coverage(index_data)
        return plan
    except Exception as exc:
        last_error = str(exc)[:500]
        db_store.finish_embedding_run(run_id, "failed", plan["embedded"], plan["failed_batches"], last_error)
        raise
    finally:
        lock.release()
