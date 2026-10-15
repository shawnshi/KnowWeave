"""The packet-carried output contract, and the runner's bounded repair round.

Both seams exist because of one measured sequence.  On 2026-09-28 the ingest contract was fixed
six times and each fix moved the failure to the next unchecked field: ``has_part`` ->
``evolved-from`` -> a missing ``.md`` on the target -> an undated timeline bullet ->
``event_tag: paper-analysis`` -> a non-numeric ``confidence``.  Every fix was correct, and every
one was a rule the validator enforced that only lived in the code.

So:

1. The machine-checked output shape travels with the packet, and the model seam appends it to the
   *end* of the brief -- the last thing the child reads -- instead of its old position at ~79% of
   a 50 KB prompt.
2. A finalize rejection is handed back to the model once, so a wording slip costs one extra model
   call rather than a job attempt, and not (at the cap) an abandoned source.

The runner tests publish a real packet, claim it the way the runner does, and then stub only the
model seam, so the lease fields and the job state machine are exercised for real.
"""

import json

from scripts import ingest_runner
from scripts.ingest_model_pi_subagents import _brief
from vector_lake import db_store, tool_ingest
from vector_lake.ingest_output_contract import build_output_contract
from vector_lake.schema_validator import INGEST_EVENT_TAGS, INGEST_INTEGRATION_PREDICATES
from vector_lake.wiki_utils import projection_hash

from tests.test_ingest_manifest import (
    _SOURCE_PAGE,
    _manifest,
    _raw_and_candidate,
    _relation,
)
from tests.test_mutation_coordinator import _source_content


def _packet(**metadata_overrides):
    metadata = {
        "processed_data": {"filepath": "raw/x.md", "canonical_name": _SOURCE_PAGE},
        "output_contract": "CONTRACT-MARKER",
    }
    metadata.update(metadata_overrides)
    return {"prompt": "PROMPT", "metadata": metadata}


def _claimed_task(isolated_memory, raw, candidate):
    """Publish a packet and claim it, so ``processed_data`` carries real lease fields.

    The job payload and the packet must agree on ``filepath``, ``hash``, ``canonical_name`` and
    ``source_hash``: ``validate_ingest_job_finalization`` compares them before finalizing.
    """
    source_text = raw.read_text(encoding="utf-8")
    shared = {
        "filepath": str(raw),
        "hash": tool_ingest.calculate_hash(str(raw)),
        "canonical_name": _SOURCE_PAGE,
        # Empty is what the dispatcher records while the Source page is not yet published,
        # and ``finalize_ingest`` compares it against the canonical page version.
        "source_hash": "",
    }
    processed = {
        **shared,
        "ingest_contract_version": tool_ingest.INGEST_CONTRACT_VERSION,
        "source_projection_hash": projection_hash(source_text),
        "integration_candidates": [candidate],
    }
    packet = {
        "prompt": "P",
        "metadata": {
            "processed_data": processed,
            "output_contract": build_output_contract(),
        },
    }
    packet_file = isolated_memory / "packet.json"
    packet_file.write_text(json.dumps(packet), encoding="utf-8")

    job_id = db_store.enqueue_job("ingest", dict(processed))
    db_store.mark_job_awaiting_subagent(job_id, str(packet_file))
    claimed = json.loads(tool_ingest.claim_ingest_tasks(limit=1))
    assert claimed, "the runner must be able to claim the packet it just published"
    return claimed[0]


# --- 2. the contract travels with the packet ----------------------------------------------


def test_the_output_contract_publishes_the_validator_vocabularies():
    """The text the model reads is generated from the sets the validator enforces."""
    contract = build_output_contract()
    for predicate in sorted(INGEST_INTEGRATION_PREDICATES):
        assert predicate in contract, predicate
    for tag in INGEST_EVENT_TAGS:
        assert tag in contract, tag
    assert "JSON NUMBER" in contract
    assert "YYYY-MM-DD" in contract
    assert "bare word" in contract


def test_the_brief_ends_with_the_packets_own_contract():
    brief = _brief(_packet())
    assert brief.startswith("PROMPT")
    assert brief.rstrip().endswith("CONTRACT-MARKER")


def test_a_pre_v9_packet_still_gets_a_contract_hint():
    packet = _packet()
    del packet["metadata"]["output_contract"]
    brief = _brief(packet)
    assert "OUTPUT CONTRACT" in brief
    assert "verbatim" in brief


def test_the_repair_round_is_stated_after_the_contract():
    packet = _packet()
    packet["metadata"]["repair"] = {
        "previous_output": {"integration": {"relations": [{"confidence": "high"}]}},
        "validation_error": "integration confidence must be numeric for Concept_X.md",
    }
    brief = _brief(packet)
    assert "REPAIR ROUND" in brief
    assert "must be numeric" in brief
    assert "high" in brief
    assert brief.index("CONTRACT-MARKER") < brief.index("REPAIR ROUND")


def test_a_packet_without_a_repair_carries_no_repair_block():
    assert "REPAIR ROUND" not in _brief(_packet())


# --- 1. one bounded repair round in the runner --------------------------------------------


def _stub_model(monkeypatch, relations):
    """Return a model seam that yields each relation in turn and records the packets it saw."""
    calls = []

    def fake_run_model(packet, model_cmd, *args, **kwargs):
        calls.append(packet)
        relation = relations[min(len(calls) - 1, len(relations) - 1)]
        return {
            "files_written": [{"filename": _SOURCE_PAGE, "content": _source_content()}],
            "integration": {"disposition": "integrated", "relations": [relation]},
        }, ""

    monkeypatch.setattr(ingest_runner, "run_model", fake_run_model)
    return calls


def _good_and_bad(isolated_memory):
    raw = _raw_and_candidate(isolated_memory)
    candidate = _manifest(raw)[0]
    good = _relation(
        candidate["target"],
        candidate["target_hash"],
        projection=candidate["target_projection_hash"],
    )
    return raw, candidate, good, {**good, "confidence": "high"}


def test_a_finalize_rejection_is_handed_back_once_and_then_finalizes(isolated_memory, monkeypatch):
    raw, candidate, good, bad = _good_and_bad(isolated_memory)
    calls = _stub_model(monkeypatch, [bad, good])
    monkeypatch.setattr(ingest_runner, "REPAIR_ATTEMPTS", 1)
    task = _claimed_task(isolated_memory, raw, candidate)

    res, err = ingest_runner._process_task(
        task, False, "fake-model", ingest_runner.raw_publication_index()
    )

    assert res["finalized"] == 1, err
    assert len(calls) == 2, "the runner must spend exactly one repair round"
    repair = calls[1]["metadata"]["repair"]
    assert "must be numeric" in repair["validation_error"]
    assert repair["previous_output"]["integration"]["relations"][0]["confidence"] == "high"


def test_stale_token_spends_no_model_repair_round(isolated_memory, monkeypatch):
    raw, candidate, good, _bad = _good_and_bad(isolated_memory)
    calls = _stub_model(monkeypatch, [{**good, "target_hash": "obsolete-token"}])
    task = _claimed_task(isolated_memory, raw, candidate)
    res, err = ingest_runner._process_task(task, False, "fake-model", ingest_runner.raw_publication_index())
    assert res["finalized"] == 0
    assert "target_hash is stale" in err
    assert len(calls) == 1


def test_missing_token_is_repairable_and_deadline_is_shared(isolated_memory, monkeypatch):
    raw, candidate, good, _bad = _good_and_bad(isolated_memory)
    bad = {key: value for key, value in good.items() if key != "target_hash"}
    calls = _stub_model(monkeypatch, [bad, good])
    task = _claimed_task(isolated_memory, raw, candidate)
    res, err = ingest_runner._process_task(task, False, "fake-model", ingest_runner.raw_publication_index())
    assert res["finalized"] == 1, err
    assert len(calls) == 2
    assert calls[0]["_host_deadline_monotonic"] == calls[1]["_host_deadline_monotonic"]


def test_a_source_that_fails_twice_is_not_repaired_forever(isolated_memory, monkeypatch):
    """Bounded on purpose: a repeat rejection is a real problem, not a slip."""
    raw, candidate, _good, bad = _good_and_bad(isolated_memory)
    calls = _stub_model(monkeypatch, [bad, bad])
    monkeypatch.setattr(ingest_runner, "REPAIR_ATTEMPTS", 1)
    task = _claimed_task(isolated_memory, raw, candidate)

    res, err = ingest_runner._process_task(
        task, False, "fake-model", ingest_runner.raw_publication_index()
    )

    assert res["finalized"] == 0
    assert res["errors"] == 1
    assert len(calls) == 2, "one model call plus one repair round, then stop"
    assert "must be numeric" in err
    assert "after 1 repair round" in err


def test_several_repair_rounds_converge_when_each_one_fixes_one_fault(
    isolated_memory, monkeypatch
):
    """A source can stack independent faults: each round must see the newest rejection.

    Measured 2026-09-28 on a paper source: round one cleared a naming violation and round two
    then had to clear a missing tension slot.  The budget is a count because one round fixes one
    fault, and the repair brief has to carry the *latest* message rather than the first.
    """
    raw, candidate, good, bad = _good_and_bad(isolated_memory)
    calls = _stub_model(monkeypatch, [bad, bad, good])
    monkeypatch.setattr(ingest_runner, "REPAIR_ATTEMPTS", 2)
    task = _claimed_task(isolated_memory, raw, candidate)

    res, err = ingest_runner._process_task(
        task, False, "fake-model", ingest_runner.raw_publication_index()
    )

    assert res["finalized"] == 1, err
    assert len(calls) == 3, "one model call plus two repair rounds"
    assert "must be numeric" in calls[1]["metadata"]["repair"]["validation_error"]
    assert "must be numeric" in calls[2]["metadata"]["repair"]["validation_error"]
    assert calls[2]["metadata"]["repair"]["previous_output"]["integration"]["relations"][0][
        "confidence"
    ] == "high", "the second round must be given the answer the first round produced"


def test_the_repair_budget_is_bounded_even_when_rounds_are_allowed(
    isolated_memory, monkeypatch
):
    raw, candidate, _good, bad = _good_and_bad(isolated_memory)
    calls = _stub_model(monkeypatch, [bad, bad, bad, bad])
    monkeypatch.setattr(ingest_runner, "REPAIR_ATTEMPTS", 2)
    task = _claimed_task(isolated_memory, raw, candidate)

    res, err = ingest_runner._process_task(
        task, False, "fake-model", ingest_runner.raw_publication_index()
    )

    assert res["finalized"] == 0
    assert res["errors"] == 1
    assert len(calls) == 3, "one model call plus at most two repair rounds"
    assert "after 2 repair round(s)" in err


def test_the_repair_round_can_be_switched_off(isolated_memory, monkeypatch):
    raw, candidate, _good, bad = _good_and_bad(isolated_memory)
    calls = _stub_model(monkeypatch, [bad])
    monkeypatch.setattr(ingest_runner, "REPAIR_ATTEMPTS", 0)
    task = _claimed_task(isolated_memory, raw, candidate)

    res, _ = ingest_runner._process_task(
        task, False, "fake-model", ingest_runner.raw_publication_index()
    )

    assert len(calls) == 1
    assert res["finalized"] == 0
