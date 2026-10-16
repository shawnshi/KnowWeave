"""Bounded, local query planning. This is selection/ranking policy, not authorization."""
from __future__ import annotations

from dataclasses import dataclass
import re

PROFILES = frozenset({'legacy', 'auto', 'balanced', 'entity', 'source', 'author', 'analysis'})
_POSITIVE = ('优势', '优点', 'strength', 'advantage')
_NEGATIVE = ('不足', '缺点', '风险', '局限', 'weakness', 'limitation', 'shortcoming')
_EVENT = re.compile(r'工作会议|卫生与健康大会|卫生健康大会|全国[^\s，。？?]{0,30}大会|论坛|conference|summit', re.I)
_YEAR = re.compile(r'(?<!\d)(?:19|20)\d{2}(?!\d)')
_STRONG_EVENT = re.compile(r'工作会议|卫生与健康大会|卫生健康大会|全国[^\s，。？?]{0,30}大会|\b(?:conference|summit)\b', re.I)


def normalize_name(value: str) -> str:
    return re.sub(r'[\W_]+', '', str(value).casefold())


@dataclass(frozen=True)
class QueryPlan:
    profile: str
    anchor: str
    event_year: str | None = None
    supplemental_queries: tuple[str, ...] = ()
    event_query: bool = False
    year_ambiguous: bool = False

    def exact_entity(self, node: dict) -> bool:
        target = normalize_name(self.anchor)
        aliases = node.get('aliases') or []
        if not isinstance(aliases, list):
            aliases = [aliases]
        names = [node.get('title') or '', *aliases]
        return bool(target) and any(normalize_name(name) == target for name in names)

    def year_status(self, node: dict) -> str:
        if not self.event_year:
            return 'not_requested'
        # created/updated/Observation timestamps are deliberately not event evidence.
        explicit = set()
        for field in ('event_year', 'event_date', 'start_date'):
            explicit.update(_YEAR.findall(str(node.get(field) or '')))
        if explicit:
            return 'metadata_year_match' if self.event_year in explicit else 'mismatch'
        aliases = node.get('aliases') or []
        if not isinstance(aliases, list):
            aliases = [aliases]
        stated = set(_YEAR.findall(' '.join(str(v) for v in [node.get('title') or '', *aliases])))
        if stated:
            return 'title_year_match' if self.event_year in stated else 'mismatch'
        return 'unverified'

    def accepts(self, node: dict) -> bool:
        if self.event_year and str(node.get('type') or '').lower() in {'person', 'vendor', 'product', 'institution'}:
            return False
        return self.year_status(node) != 'mismatch'

    def order(self, rows: list[tuple[float, dict]]) -> list[tuple[float, dict]]:
        def key(row):
            score, node = row
            year_tier = self.year_status(node) == 'unverified'
            if self.profile == 'entity':
                tier = not self.exact_entity(node)
            elif self.profile == 'source':
                tier = str(node.get('type') or '').lower() != 'source'
            else:
                tier = False
            return year_tier, tier, -score, str(node.get('_key') or '')
        return sorted(rows, key=key)

    def diagnostics(self) -> dict:
        # No raw entity/anchor/query text in the persistent ledger.
        return {'profile': self.profile, 'event_year_requested': self.event_year is not None,
                'supplemental_query_count': len(self.supplemental_queries)}


def plan_query(query: str, profile: str = 'auto') -> QueryPlan:
    if profile not in PROFILES:
        raise ValueError('Unknown retrieval profile: ' + str(profile))
    text = query.strip()
    if profile == 'legacy':
        return QueryPlan('legacy', text)
    if not text:
        raise ValueError('query must not be empty')
    lower = text.casefold()
    analysis = any(t in lower for t in _POSITIVE) and any(t in lower for t in _NEGATIVE)
    event_query = bool(_EVENT.search(text))
    # A forum/software keyword is a ranking hint, not sufficient temporal/entity evidence.
    years = set(_YEAR.findall(text)) if _STRONG_EVENT.search(text) else set()
    event_year = next(iter(years)) if len(years) == 1 else None
    anchor = re.sub(r'^(?:请)?(?:查询|介绍|关于|谁是|总结|概括|分析)\s*', '', text)
    anchor = re.sub(r'(?:是什么|是谁|要点|总结|核心内容)[？?。\s]*$', '', anchor).strip()
    variants = []
    if analysis:
        # Product-name anchor is supplementary, not a replacement for qualifiers.
        names = re.findall(r'(?<![A-Za-z0-9_.-])[A-Za-z][A-Za-z0-9_.-]{2,}(?![A-Za-z0-9_.-])', text)
        if names:
            anchor = names[0]
            variants.append(anchor)
    if event_year:
        event_anchor = _YEAR.sub('', anchor).lstrip('年 ').strip()
        variants.extend([f'{event_year} {event_anchor}', event_anchor])
    elif anchor != text:
        variants.append(anchor)
    if profile == 'auto':
        if analysis:
            profile = 'analysis'
        elif any(t in text for t in ('原文', '全文', '原始文件')):
            profile = 'source'
        elif any(t in text for t in ('我的观点', '作者观点', '我写过')):
            profile = 'author'
        elif event_query:
            profile = 'balanced'
        else:
            profile = 'entity' if len(anchor) <= 64 else 'balanced'
    unique = []
    for variant in variants:
        if variant and variant.casefold() != text.casefold() and variant.casefold() not in {v.casefold() for v in unique}:
            unique.append(variant)
    return QueryPlan(profile, anchor, event_year, tuple(unique[:3]), event_query, len(years) > 1)


class PlannedCatalog:
    """Apply event constraints at every candidate lookup, before widening or reranking."""
    def __init__(self, catalog, plan: QueryPlan):
        self.catalog, self.plan = catalog, plan

    def __getattr__(self, name):
        return getattr(self.catalog, name)

    def nodes_by_key(self, keys):
        return {key: node for key, node in self.catalog.nodes_by_key(keys).items()
                if self.plan.accepts(node)}

    def filter_fields(self, keys):
        if self.plan.event_query:
            return self.nodes_by_key(keys)
        cheap = getattr(self.catalog, 'filter_fields', None)
        return cheap(keys) if cheap is not None else self.nodes_by_key(keys)

    def adjacency(self):
        return self.catalog.adjacency()
