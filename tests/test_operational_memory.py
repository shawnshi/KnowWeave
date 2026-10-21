import os
import unittest
from pathlib import Path
from unittest import mock

from vector_lake import governance_store, wiki_utils
from vector_lake.tool_search import build_memory_packet, search_vector_lake


class TestOperationalMemory(unittest.TestCase):
    def setUp(self):
        self.test_root = Path(os.environ["VECTOR_LAKE_MEMORY_DIR"])
        self.test_root.mkdir(parents=True, exist_ok=True)
        self.previous_memory_dir = os.environ.get("VECTOR_LAKE_MEMORY_DIR")
        os.environ["VECTOR_LAKE_MEMORY_DIR"] = str(self.test_root)
        self.meta_dir = self.test_root / "wiki" / ".meta"
        self.meta_dir.mkdir(parents=True, exist_ok=True)
        wiki_utils._META_DIR_CACHE = self.meta_dir
        from vector_lake.db_store import init_db
        init_db()

    def tearDown(self):
        if self.previous_memory_dir is None:
            os.environ.pop("VECTOR_LAKE_MEMORY_DIR", None)
        else:
            os.environ["VECTOR_LAKE_MEMORY_DIR"] = self.previous_memory_dir
        wiki_utils._META_DIR_CACHE = None

    def _seed_claims(self):
        claims = governance_store.load_claims()
        claims["items"] = {
            "claim_old": {
                "claim_id": "claim_old",
                "claim_text": "Preferred deployment target: Legacy VM",
                "claim_type": "assertion",
                "memory_type": "preference",
                "memory_key": "deployment_target",
                "status": "Active",
                "confidence": 0.78,
                "authority_score": 0.6,
                "importance_score": 0.7,
                "evidence_ids": ["ev_old"],
                "source_ids": ["src_old"],
                "source_page": "Entity_Runtime.md",
                "updated_at": "2025-01-01T00:00:00+00:00",
                "created_at": "2025-01-01T00:00:00+00:00",
                "locator": {"heading": "Runtime"},
            },
            "claim_new": {
                "claim_id": "claim_new",
                "claim_text": "Preferred deployment target: Kubernetes",
                "claim_type": "assertion",
                "memory_type": "preference",
                "memory_key": "deployment_target",
                "status": "Active",
                "confidence": 0.74,
                "authority_score": 0.6,
                "importance_score": 0.7,
                "evidence_ids": ["ev_new"],
                "source_ids": ["src_new"],
                "source_page": "Entity_Runtime.md",
                "updated_at": "2026-01-01T00:00:00+00:00",
                "created_at": "2026-01-01T00:00:00+00:00",
                "locator": {"heading": "Runtime"},
            },
            "claim_fact": {
                "claim_id": "claim_fact",
                "claim_text": "Agent memory should retrieve facts before loading full Markdown pages.",
                "claim_type": "assertion",
                "status": "Active",
                "confidence": 0.82,
                "evidence_ids": ["ev_fact"],
                "source_ids": ["src_fact"],
                "source_page": "Concept_Agent_Memory.md",
                "updated_at": "2026-01-02T00:00:00+00:00",
                "created_at": "2026-01-02T00:00:00+00:00",
                "locator": {"heading": "Agent Memory"},
            },
        }
        governance_store.save_claims(claims)

    def test_rebuild_operational_memory_resolves_typed_conflict(self):
        self._seed_claims()
        # Only this positive conflict fixture models reviewed evidence.
        # Other tests retain legacy/unverified provenance.
        claims = governance_store.load_claims()
        for record in claims["items"].values():
            record.update(evidence_support="verified", verified_evidence_ids=list(record["evidence_ids"]))
        governance_store.save_claims(claims)
        store = governance_store.rebuild_operational_memory()
        preferences = [
            item for item in store["items"].values()
            if item["memory_type"] == "preference"
        ]

        self.assertEqual(len(preferences), 2)
        old_item = next(item for item in preferences if "Legacy VM" in item["text"])
        new_item = next(item for item in preferences if "Kubernetes" in item["text"])
        self.assertEqual(old_item["validity_state"], "superseded")
        self.assertEqual(old_item["superseded_by"], new_item["memory_id"])
        self.assertEqual(new_item["conflict_resolution"]["state"], "winner")

    def test_memory_search_and_packet_prefer_runtime_memory(self):
        self._seed_claims()
        governance_store.rebuild_operational_memory()

        output = search_vector_lake("deployment target", mode="memory", top_k=3)
        self.assertIn("Kubernetes", output)
        self.assertNotIn("No operational memory", output)

        packet = build_memory_packet("deployment target", max_chars=12000)["packet"]
        self.assertIn("Current Preferences", packet)
        self.assertIn("Kubernetes", packet)
        self.assertIn("Conflicts / Stale Warnings", packet)
        self.assertIn("Legacy VM", packet)

    def test_deleting_conflict_winner_reactivates_remaining_preference(self):
        self._seed_claims()
        claims = governance_store.load_claims()
        for claim in claims["items"].values():
            claim.setdefault("locator", {})["page_key"] = "Entity_Runtime"
        governance_store.save_claims(claims)
        governance_store.rebuild_operational_memory()
        remaining = claims["items"]["claim_old"]

        governance_store.apply_change_sets_batch([{
            "affected_pages": ["Entity_Runtime.md"],
            "proposed_entities": [],
            "proposed_claims": [remaining],
            "proposed_evidence": [],
            "proposed_source_updates": [],
            "proposed_edges": [],
        }])

        memories = governance_store.load_memory_objects()["items"].values()
        preference = next(item for item in memories if item.get("source_claim_id") == "claim_old")
        self.assertEqual(preference["validity_state"], "needs-review")
        self.assertIn("legacy-unverified", preference["validity_reasons"])
        self.assertNotIn("superseded_by", preference)
        self.assertFalse(any(item.get("source_claim_id") == "claim_new" for item in memories))

    def test_the_packet_reports_true_counts_not_the_displayed_slice(self):
        """``warning_count`` and ``omitted_count`` are read by callers, so they cannot be slices.

        The packet shows at most six warnings and twelve evidence pointers.  The counters used to
        be taken from those slices: ``warning_count`` reported 6 for any larger set, and
        ``omitted_count`` was ``len(memories) - 12`` -- a number that matched neither the memories
        dropped nor the pointers capped, and that stayed 0 when no character truncation happened
        even though half the pointers had been dropped.  ``memory_warning_count`` is printed to the
        caller as "warnings" and gates the burst re-render, so a capped counter misreports all
        three.
        """
        memories = [
            {"memory_type": "fact", "memory_key": f"k{index}", "text": "x" * 300,
             "memory_score": 0.5, "validity_state": "active", "source_page": "Concept_X",
             "source_claim_id": f"claim_{index}"}
            for index in range(30)
        ]
        historical = [
            {"memory_type": "decision", "memory_key": f"h{index}", "text": "y" * 100,
             "validity_state": "superseded"}
            for index in range(9)
        ]
        with mock.patch.object(
            governance_store,
            "search_memory_packet_views",
            lambda query: (memories, historical),
        ):
            roomy = build_memory_packet("q", max_chars=60000)
            tight = build_memory_packet("q", max_chars=2000)
            zero = build_memory_packet("q", max_chars=200)

        # The true count, while the display block stays capped at six.
        for packet in (roomy, tight, zero):
            self.assertEqual(packet["warning_count"], 9)
            self.assertEqual(packet["memory_count"], 30)
        self.assertEqual(
            sum(1 for line in roomy["packet"].splitlines() if "superseded" in line), 6
        )

        # Nothing was dropped when everything fits; that is the number the field claims to be.
        self.assertEqual(roomy["omitted_count"], 0)
        # Some fit at 2000 chars, but not all.
        self.assertTrue(0 < tight["omitted_count"] < tight["memory_count"])
        # A budget that cannot even carry the frame drops every memory line, and says so.
        self.assertEqual(zero["omitted_count"], zero["memory_count"])

        # The budget invariant ``assemble_context`` depends on holds even when the frame alone
        # overruns it, and the packet stays parseable in every case.
        for packet, budget in ((roomy, 60000), (tight, 2000), (zero, 200)):
            text = packet["packet"]
            self.assertLessEqual(len(text), budget)
            self.assertEqual(text.count("<MEMORY_PACKET>"), 1)
            self.assertTrue(text.rstrip().endswith("</MEMORY_PACKET>"))


if __name__ == "__main__":
    unittest.main()
