"""Offline evaluation over explicit judgments/results. Never queries a KB or calls a model."""
from __future__ import annotations

from hashlib import sha256
import json
import math
from pathlib import Path

from vector_lake.retrieval_plan import PROFILES


def validate_dataset(dataset: dict) -> list[dict]:
    if type(dataset.get('schema_version')) is not int or dataset.get('schema_version') != 1 or dataset.get('label_kind') not in {'synthetic', 'human'}:
        raise ValueError('Dataset requires schema_version=1 and explicit synthetic/human label_kind')
    if dataset['label_kind'] == 'human' and (
        dataset.get('status') != 'approved' or not isinstance(dataset.get('approved_by'), str)
        or not dataset['approved_by'].strip()
    ):
        raise ValueError('Human labels require approved status and an explicit reviewer declaration')
    cases = dataset.get('cases')
    if not isinstance(cases, list) or not 1 <= len(cases) <= 1000:
        raise ValueError('Dataset requires 1..1000 cases')
    seen = set()
    for case in cases:
        if not isinstance(case, dict) or not isinstance(case.get('id'), str) or not case['id'] or case['id'] in seen:
            raise ValueError('Case IDs must be non-empty and unique')
        seen.add(case['id'])
        if not isinstance(case.get('query'), str) or not case['query'].strip() or not isinstance(case.get('domain'), str) or not case['domain'].strip():
            raise ValueError('Each case requires query and explicit domain')
        if case.get('profile', 'auto') not in PROFILES:
            raise ValueError('Unknown case retrieval profile')
        judgments = case.get('judgments')
        if not isinstance(judgments, dict) or any(not isinstance(key, str) or not key or type(grade) is not int or not 0 <= grade <= 3 for key, grade in judgments.items()):
            raise ValueError('Judgments must map page keys to integer grades 0..3')
        if case.get('should_abstain') is True and any(judgments.values()):
            raise ValueError('No-answer case cannot also have positive judgments')
        if case.get('should_abstain') is False and not any(judgments.values()):
            raise ValueError('Answerable case requires at least one positive judgment')
        if dataset['label_kind'] == 'human' and type(case.get('should_abstain')) is not bool:
            raise ValueError('Human cases require an explicit answerability decision')
        if not isinstance(case.get('aspects', {}), dict):
            raise ValueError('aspects must be an object')
        if case.get('should_abstain') is not None and type(case['should_abstain']) is not bool:
            raise ValueError('should_abstain must be boolean or omitted')
        for targets in case.get('aspects', {}).values():
            if not isinstance(targets, list) or not targets or any(key not in judgments or judgments[key] <= 0 for key in targets):
                raise ValueError('Aspect targets must be positively judged pages')
    return cases


def evaluate(dataset: dict, results: dict, k: int = 10) -> dict:
    cases = validate_dataset(dataset)
    if type(k) is not int or not 1 <= k <= 100 or not isinstance(results, dict) or set(results) != {c['id'] for c in cases}:
        raise ValueError('Results must cover exactly the case IDs; k must be 1..100')
    records = []
    for case in cases:
        value = results[case['id']]
        payload = {'pages': value} if isinstance(value, list) else value
        if not isinstance(payload, dict) or not isinstance(payload.get('pages'), list) or len(payload['pages']) > 1000:
            raise ValueError('Each result requires a pages list')
        if any(not isinstance(key, str) or not key for key in payload['pages']):
            raise ValueError('Result page keys must be non-empty strings')
        pages = list(dict.fromkeys(payload['pages']))[:k]
        judgments = case['judgments']
        positives = {key for key, grade in judgments.items() if grade > 0}
        hits = [key for key in pages if key in positives]
        first = next((rank for rank, key in enumerate(pages, 1) if key in positives), None)
        dcg = sum((2 ** judgments.get(key, 0) - 1) / math.log2(rank + 1)
                  for rank, key in enumerate(pages, 1))
        ideal = sorted(judgments.values(), reverse=True)[:k]
        idcg = sum((2 ** grade - 1) / math.log2(rank + 1) for rank, grade in enumerate(ideal, 1))
        aspects = case.get('aspects', {})
        covered = sum(bool(set(pages) & set(targets)) for targets in aspects.values())
        row = {'id': case['id'], 'profile': case.get('profile', 'auto'),
               'recall': len(hits) / len(positives) if positives else None,
               'mrr': 1 / first if first else (0.0 if positives else None),
               'ndcg': dcg / idcg if idcg else None,
               'judged_fraction': sum(key in judgments for key in pages) / len(pages) if pages else None,
               'aspect_document_coverage': covered / len(aspects) if aspects else None,
               'abstention_correct': None}
        if 'abstained' in payload:
            if type(payload['abstained']) is not bool:
                raise ValueError('abstained must be boolean when provided')
            if type(case.get('should_abstain')) is bool:
                row['abstention_correct'] = payload['abstained'] == case['should_abstain']
        records.append(row)
    def summarize(rows):
        result: dict[str, int | float | None] = {'cases': len(rows)}
        for metric in ('recall', 'mrr', 'ndcg', 'judged_fraction', 'aspect_document_coverage', 'abstention_correct'):
            values = [float(row[metric]) for row in rows if row[metric] is not None]
            result[metric] = sum(values) / len(values) if values else None
            result[metric + '_measured_cases'] = len(values)
        return result
    def digest(value):
        return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()
    return {'schema_version': 1, 'label_kind': dataset['label_kind'], 'k': k,
            'dataset_digest': digest(dataset), 'run_digest': digest(results),
            'label_provenance': 'declared by dataset; not independently authenticated',
            'profile_semantics': 'by_profile groups requested dataset profiles; resolved auto policy is not inferred',
            'limitations': ['Recall is relative to judged positives, not exhaustive corpus recall',
                            'Aspect coverage measures documents, not claim faithfulness',
                            'Answer/citation accuracy and production performance are not measured'],
            'summary': summarize(records),
            'by_profile': {profile: summarize([r for r in records if r['profile'] == profile])
                           for profile in sorted({r['profile'] for r in records})},
            'cases': records}


def evaluate_files(dataset_path: str, results_path: str, k: int = 10) -> dict:
    # Explicit local files only. Nothing is archived or uploaded automatically.
    def read_json(path):
        try:
            with Path(path).open('rb') as stream:
                data = stream.read(8 * 1024 * 1024 + 1)
            if len(data) > 8 * 1024 * 1024:
                raise ValueError('Evaluation file exceeds 8 MiB budget')
            return json.loads(data.decode('utf-8-sig'))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(f'Evaluation input error: {type(exc).__name__}') from exc
    dataset = read_json(dataset_path)
    results = read_json(results_path)
    return evaluate(dataset, results, k)
