import copy
import json
import math
from pathlib import Path

import pytest

from vector_lake.retrieval_eval import evaluate, evaluate_files, validate_dataset


def dataset():
    return {'schema_version': 1, 'label_kind': 'synthetic', 'cases': [
        {'id': 'q', 'query': 'synthetic', 'domain': 'Medical_IT', 'profile': 'analysis',
         'judgments': {'A': 3, 'B': 1, 'C': 0}, 'aspects': {'positive': ['A'], 'negative': ['B']},
         'should_abstain': False}]}


def test_metrics_are_judgment_relative_not_invented_quality_claims():
    report = evaluate(dataset(), {'q': {'pages': ['C', 'A'], 'abstained': False}}, k=2)
    row = report['cases'][0]
    assert row['recall'] == 0.5 and row['mrr'] == 0.5
    assert row['ndcg'] == pytest.approx((7 / math.log2(3)) / (7 + 1 / math.log2(3)))
    assert row['aspect_document_coverage'] == 0.5
    assert row['abstention_correct'] is True
    assert report['label_kind'] == 'synthetic'
    assert any('not exhaustive' in text for text in report['limitations'])


def test_duplicate_pages_cannot_inflate_recall():
    report = evaluate(dataset(), {'q': ['A', 'A', 'B']}, k=2)
    assert report['summary']['recall'] == 1.0
    assert report['summary']['ndcg'] == 1.0
    assert report['summary']['abstention_correct'] is None


def test_unjudged_and_unmeasured_answer_metrics_remain_explicit():
    report = evaluate(dataset(), {'q': ['unknown']})
    assert report['summary']['judged_fraction'] == 0
    assert report['summary']['abstention_correct_measured_cases'] == 0
    assert report['cases'][0]['recall'] == 0


def test_no_answer_cases_do_not_become_fake_zero_recall():
    data = dataset()
    data['cases'][0].update(judgments={}, aspects={}, should_abstain=True)
    report = evaluate(data, {'q': {'pages': [], 'abstained': True}})
    assert report['summary']['recall'] is None
    assert report['summary']['abstention_correct'] == 1


@pytest.mark.parametrize('mutate', [
    lambda d: d.update(label_kind='human'),
    lambda d: d.update(schema_version=True),
    lambda d: d['cases'][0].update(domain=''),
    lambda d: d['cases'][0].update(domain=True),
    lambda d: d['cases'][0].update(judgments={'A': True}),
    lambda d: d['cases'][0].update(should_abstain=True),
    lambda d: d['cases'][0].update(aspects={'missing': ['unjudged']}),
    lambda d: d['cases'].append(copy.deepcopy(d['cases'][0])),
])
def test_invalid_or_unapproved_labels_are_not_evaluated(mutate):
    data = dataset()
    mutate(data)
    with pytest.raises(ValueError):
        validate_dataset(data)


def test_missing_run_is_a_failure_not_an_empty_result():
    with pytest.raises(ValueError, match='exactly'):
        evaluate(dataset(), {})


def test_offline_files_do_not_need_a_knowledge_store_or_model(tmp_path, monkeypatch):
    from vector_lake import embedding_scheduler
    monkeypatch.setattr(embedding_scheduler, 'embed_texts', lambda *a, **k: pytest.fail('external model'))
    data = tmp_path / 'dataset.json'
    results = tmp_path / 'results.json'
    data.write_text(json.dumps(dataset()), encoding='utf-8')
    results.write_text(json.dumps({'q': ['A', 'B']}), encoding='utf-8')
    assert evaluate_files(str(data), str(results))['summary']['recall'] == 1


def test_committed_fixture_is_unambiguously_synthetic():
    data = json.loads((Path(__file__).parent / 'fixtures' / 'retrieval_synthetic.json').read_text(encoding='utf-8'))
    validate_dataset(data)
    assert data['label_kind'] == 'synthetic'
    assert len(data['cases']) == 5
