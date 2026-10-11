"""Public query expansion snapshot and immutable offline tokenizer profiles.

No user dictionaries, environment settings or filesystem sources are consulted.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass


_EXPANSION_SNAPSHOT = (
    ("医疗信息化", ("HIT", "卫宁", "电子病历", "医疗IT")),
    ("大模型", ("LLM", "大语言模型", "Agent", "智能体")),
    ("医疗AI", ("临床Agent", "大模型医疗落地", "电子病历 智能化")),
)

# Compatibility callers may edit their copy, never the seed snapshot.
QUERY_EXPANSION_DICT = {key: list(values) for key, values in _EXPANSION_SNAPSHOT}


def _canonical_terms(terms) -> tuple[str, ...]:
    if isinstance(terms, (str, bytes, bytearray)):
        raise TypeError("terms must be an iterable of strings, not a string")
    result = set()
    total_bytes = 0
    for count, term in enumerate(terms, 1):
        if count > 256:
            raise ValueError("profile accepts at most 256 input terms")
        if not isinstance(term, str):
            raise TypeError("profile terms must be strings")
        term = str.__str__(term)
        if not term or len(term) > 64 or any(char.isspace() for char in term):
            raise ValueError("terms must be nonempty, whitespace-free and at most 64 characters")
        total_bytes += len(term.encode("utf-8"))
        if total_bytes > 16384:
            raise ValueError("profile terms exceed 16384 UTF-8 bytes")
        result.add(term)
    return tuple(sorted(result))


@dataclass(frozen=True, slots=True, init=False)
class FrozenDomainProfile:
    """Bounded exact terms; profile hash includes every tokenization policy."""

    name: str
    version: str
    terms: tuple[str, ...]
    hmm: bool = True
    frequency_policy: str = "sorted-add-word-default"
    whitespace_policy: str = "split-unicode-whitespace"
    contract_version: str = "profile-tokenizer-v1"

    def __init__(self, name: str, version: str, terms: Iterable[str], *,
                 hmm: bool = True, frequency_policy: str = "sorted-add-word-default",
                 whitespace_policy: str = "split-unicode-whitespace",
                 contract_version: str = "profile-tokenizer-v1"):
        for label, value in (("name", name), ("version", version), ("hmm", hmm),
                             ("frequency_policy", frequency_policy),
                             ("whitespace_policy", whitespace_policy),
                             ("contract_version", contract_version)):
            object.__setattr__(self, label, value)
        object.__setattr__(self, "terms", _canonical_terms(terms))
        self.__post_init__()

    def __post_init__(self):
        for label, value in (("name", self.name), ("version", self.version)):
            if not isinstance(value, str):
                raise TypeError(f"profile {label} must be a string")
            value = str.__str__(value)
            object.__setattr__(self, label, value)
            if not value or value != value.strip():
                raise ValueError(f"profile {label} must be nonempty and trimmed")
            value.encode("utf-8")
        if self.hmm is not True:
            raise ValueError("profile-tokenizer-v1 requires HMM=True")
        for label, expected in (
            ("frequency_policy", "sorted-add-word-default"),
            ("whitespace_policy", "split-unicode-whitespace"),
            ("contract_version", "profile-tokenizer-v1"),
        ):
            value = getattr(self, label)
            if not isinstance(value, str):
                raise TypeError(f"profile {label} must be a string")
            value = str.__str__(value)
            if value != expected:
                raise ValueError(f"unsupported {label}")
            object.__setattr__(self, label, value)

    @property
    def profile_hash(self) -> str:
        payload = {
            "name": self.name, "version": self.version, "terms": self.terms,
            "hmm": self.hmm, "frequency_policy": self.frequency_policy,
            "whitespace_policy": self.whitespace_policy,
            "contract_version": self.contract_version,
        }
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()


PUBLIC_QUERY_PROFILE = FrozenDomainProfile(
    name="public-query-expansion-v1", version="1",
    terms=tuple(term for key, values in _EXPANSION_SNAPSHOT
                for text in (key, *values) for term in text.split()),
)
