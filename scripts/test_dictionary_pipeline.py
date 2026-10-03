"""Infrastructure tests. Synthetic translations are not linguistic evaluation evidence."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from dictionary_pipeline import (Factory, PipelineError, atomic_json, cache_key, digest,
                                 dumps, normalize, response_schema, validate_schema)


def record(term="dog", meaning="A faithful friend.", language="en", name="fixture"):
    source = dict(language=language, term=term, meaning=meaning, source_name=name,
                  source_reference="https://example.test/fixture", license_note="synthetic test only")
    return dict(entry_id=digest(source), source=source, origin=dict(entry_index=0, meaning_index=0),
                aggregate_sources=[dict(name=name)], aliases=[term.upper()])


def response(request, ja="忠実な友人を表す。", term="犬"):
    return {"results": [dict(id=item["id"], status="translated", action="replace", reason="",
        senses=[dict(sense_key="1", ja_term=term, aliases=[], units=[dict(source_quote=item["meaning"],
            ja=ja, kind="interpretation")], tone="positive", ambiguous=False,
            translation_confidence=.98, semantic_confidence=.97, flags=[])]) for item in request["items"]]}


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.factory = Factory(self.root / "work")

    def tearDown(self):
        self.factory.close()
        self.temp.cleanup()

    def prepare(self, records=None):
        self.factory.prepare(records or [record()])
        return self.factory.export_batch()

    def finish(self, request=None):
        request = request or self.prepare()
        result = response(request)
        self.factory.ingest(request["batch_id"], result, "synthetic-test-model")
        return request, result

    def test_roundtrip_provenance_aliases(self):
        self.finish()
        master = self.factory.export_master()[0]
        self.assertEqual(master["source"], record()["source"])
        self.assertEqual(master["source_aliases"], ["DOG"])
        self.assertEqual(master["qa"]["status"], "complete")
        self.assertIsNone(master["processing"]["model_effective"])

    def test_no_order_based_alignment(self):
        request = self.prepare([record(), record("cat", "An independent friend.")])
        result = response(request)
        result["results"].reverse()
        self.factory.ingest(request["batch_id"], result, "fixture")
        self.assertEqual(self.factory.statistics()["states"]["complete"], 2)

    def test_missing_extra_duplicate_ids_rejected(self):
        request = self.prepare([record(), record("cat", "Other meaning.")])
        result = response(request)
        for kind in ["missing", "extra", "duplicate"]:
            broken = copy.deepcopy(result)
            if kind == "missing":
                broken["results"].pop()
            elif kind == "extra":
                broken["results"][0]["id"] = "unknown"
            else:
                broken["results"][1] = broken["results"][0]
            with self.assertRaisesRegex(PipelineError, "ID_MISMATCH"):
                self.factory.ingest(request["batch_id"], broken, "fixture")
        self.assertEqual(self.factory.statistics()["states"]["processing"], 2)
        self.assertIsNotNone(self.factory.meta("halt"))

    def test_source_tail_cannot_disappear(self):
        request = self.prepare([record(meaning="A faithful friend. But then betrayal.")])
        result = response(request)
        result["results"][0]["senses"][0]["units"][0]["source_quote"] = "A faithful friend."
        with self.assertRaisesRegex(PipelineError, "SOURCE_COVERAGE"):
            self.factory.ingest(request["batch_id"], result, "fixture")

    def test_long_meanings_not_truncated(self):
        meaning = "A faithful friend. " * 600
        request = self.prepare([record(meaning=meaning)])
        self.assertEqual(request["items"][0]["meaning"], meaning.strip())
        self.finish(request)
        self.assertEqual(self.factory.export_master()[0]["source"]["meaning"], meaning)

    def test_cache_survives_model_change_and_normalized_duplicate(self):
        self.finish()
        self.factory.prepare([record(meaning=" A faithful friend. ")])
        self.assertEqual(self.factory.statistics()["cache_hits"], 1)
        self.assertIsNone(self.factory.export_batch())
        self.assertEqual(self.factory.statistics()["llm_processed_count"], 1)

    def test_cache_checksum_rejects_corruption(self):
        self.finish()
        with self.factory.db:
            self.factory.db.execute("UPDATE cache SET checksum='corrupt'")
        with self.assertRaisesRegex(PipelineError, "CACHE_CORRUPTION"):
            self.factory.cached(cache_key(record()["source"]))

    def test_exact_duplicate_retains_origins(self):
        first = record()
        second = copy.deepcopy(first)
        second["origin"]["entry_index"] = 42
        request = self.prepare([first, second])
        self.assertEqual(len(request["items"]), 1)
        self.finish(request)
        self.assertEqual(len(self.factory.export_master()[0]["origins"]), 2)

    def test_distinct_source_not_collapsed(self):
        self.finish(self.prepare([record(), record(name="another source")]))
        index = self.factory.build(self.root / "staging", 2)
        self.assertEqual(index["entry_count"], 1)
        shard, pos = index["entries"][0][4:]
        senses = json.loads((self.root / "staging" / f"meanings-{shard:02d}.min.json").read_text(encoding="utf-8"))[pos]
        self.assertEqual(len(senses), 2)
        self.assertEqual({s["s"][0] for s in senses}, {"fixture", "another source"})

    def test_recover_saved_response_after_crash(self):
        request = self.prepare()
        atomic_json(self.factory.root / "responses" / (request["batch_id"] + ".json"), response(request))
        self.factory.close()
        self.factory = Factory(self.root / "work")
        self.factory.recover("fixture")
        self.assertEqual(self.factory.statistics()["states"]["complete"], 1)

    def test_uncertain_dispatch_does_not_resend(self):
        self.prepare()
        with self.assertRaisesRegex(PipelineError, "UNCERTAIN_DISPATCH"):
            self.factory.recover("fixture")
        with self.assertRaisesRegex(PipelineError, "Outstanding batch"):
            self.factory.export_batch()

    def test_replay_idempotent_and_conflict_rejected(self):
        request, result = self.finish()
        self.factory.ingest(request["batch_id"], result, "different-model")
        self.assertEqual(self.factory.statistics()["llm_processed_count"], 1)
        result["results"][0]["reason"] = "different"
        with self.assertRaisesRegex(PipelineError, "conflicting replay"):
            self.factory.ingest(request["batch_id"], result, "fixture")

    def test_malformed_is_quarantined(self):
        malformed = record(meaning=None)
        self.factory.prepare([malformed])
        self.assertIsNone(self.factory.export_batch())
        self.assertEqual(self.factory.statistics()["states"]["unresolved"], 1)

    def test_changed_source_replaces_snapshot_not_cache(self):
        self.finish()
        self.factory.prepare([record(meaning="A new source meaning.")])
        self.assertEqual(self.factory.statistics()["entries"], 1)
        self.assertEqual(self.factory.statistics()["states"]["pending"], 1)
        self.assertEqual(self.factory.db.execute("SELECT count(*) FROM cache").fetchone()[0], 1)

    def test_pending_and_quality_halt_block_build(self):
        self.factory.prepare([record()])
        with self.assertRaisesRegex(PipelineError, "BUILD_GATE"):
            self.factory.build(self.root / "staging")
        self.finish(self.factory.export_batch())
        self.factory.meta("halt", {"reason": "test"})
        with self.assertRaisesRegex(PipelineError, "BUILD_GATE"):
            self.factory.build(self.root / "staging")

    def test_numeric_loss_routes_review_and_halts(self):
        request = self.prepare([record(meaning="3 friends.")])
        self.factory.ingest(request["batch_id"], response(request), "fixture")
        self.assertEqual(self.factory.statistics()["states"]["needs_review"], 1)
        with self.assertRaisesRegex(PipelineError, "QUALITY_HALT"):
            self.factory.export_batch(pass_kind="review")
        self.factory.acknowledge_halt("Confirmed numeric mismatch; route this isolated record to correction.")
        review = self.factory.export_batch(pass_kind="review")
        self.factory.ingest(review["batch_id"], response(review, ja="3人の友人を表す。"), "fixture")
        self.assertEqual(self.factory.statistics()["states"]["complete"], 1)

    def test_review_keep_cannot_paraphrase(self):
        request = self.prepare()
        result = response(request)
        result["results"][0]["senses"][0]["flags"] = ["SOURCE_QUALITY_ISSUE:archaic"]
        self.factory.ingest(request["batch_id"], result, "fixture")
        review = self.factory.export_batch(pass_kind="review")
        kept = copy.deepcopy(review["items"][0]["current"])
        kept.update(id=review["items"][0]["id"], action="keep", reason="The source is archaic; the translation is faithful.")
        changed = copy.deepcopy(kept)
        changed["senses"][0]["ja_term"] = "猫"
        with self.assertRaisesRegex(PipelineError, "keep must preserve"):
            self.factory.ingest(review["batch_id"], {"results": [changed]}, "fixture")
        self.factory.ingest(review["batch_id"], {"results": [kept]}, "fixture")
        self.factory.acknowledge_halt("Rejected synthetic paraphrase; unchanged review response validated.")
        self.assertEqual(self.factory.statistics()["states"]["complete"], 1)
        self.factory.build(self.root / "staging", 2)

    def test_strict_schema_no_nan_booleans_or_extra(self):
        request = self.prepare()
        for value in [True, float("nan"), 1.1, -1]:
            result = response(request)
            result["results"][0]["senses"][0]["translation_confidence"] = value
            with self.assertRaises(PipelineError):
                validate_schema(result, response_schema())

    def test_random_audit_reproducible(self):
        self.finish()
        self.factory.random_audit()
        first = (self.factory.root / "random-audit.json").read_bytes()
        self.factory.random_audit()
        self.assertEqual(first, (self.factory.root / "random-audit.json").read_bytes())

    def test_html_normalization_word_boundaries(self):
        self.assertEqual(normalize("<p>A</p><p>B<br>C</p>"), "A B C")

    def test_oversized_source_kept_intact_but_not_dispatched(self):
        meaning = "A long source. " * 2000
        self.factory.prepare([record(meaning=meaning)])
        self.assertIsNone(self.factory.export_batch())
        master = self.factory.export_master()[0]
        self.assertEqual(master["source"]["meaning"], meaning)
        self.assertIn("oversized", master["qa"]["reason"])

    def test_phase_one_dispatch_limit(self):
        self.factory.prepare([record(str(i), f"Meaning {i}.") for i in range(201)])
        with self.factory.db:
            self.factory.db.execute("UPDATE entries SET attempts=1,status='complete' WHERE id IN (SELECT id FROM entries ORDER BY id LIMIT 200)")
        with self.assertRaisesRegex(PipelineError, "PHASE_GATE"):
            self.factory.export_batch()

    def test_activation_requires_pass_and_confirmation(self):
        self.factory.prepare([record()])
        state = self.root / "state.md"
        state.write_text("PHASE_1_GATE: NOT_YET_PASSED", encoding="utf-8")
        with self.assertRaisesRegex(PipelineError, "PHASE_GATE"):
            self.factory.activate_phase2(state, "USER_REQUESTED_CONTINUE_WITH_LUNA")
        state.write_text("PHASE_1_GATE: PASS", encoding="utf-8")
        with self.assertRaisesRegex(PipelineError, "PHASE_GATE"):
            self.factory.activate_phase2(state, "not approved")
        self.factory.activate_phase2(state, "USER_REQUESTED_CONTINUE_WITH_LUNA")
        self.assertEqual(self.factory.meta("phase"), "phase2")

    def test_retry_requires_proof_and_changes_batch_id(self):
        request = self.prepare()
        with self.assertRaisesRegex(PipelineError, "RETRY_REQUIRES_PROOF"):
            self.factory.retry_unprocessed(request["batch_id"], "network disconnected", False)
        self.factory.retry_unprocessed(request["batch_id"], "Provider rejected request before inference; verified from dispatch log.", True)
        retry = self.factory.export_batch()
        self.assertNotEqual(request["batch_id"], retry["batch_id"])
        self.finish(retry)
        self.assertEqual(self.factory.statistics()["translation_retry_rate"], 1)
        with self.assertRaisesRegex(PipelineError, "RETRY_UNSAFE"):
            self.factory.retry_unprocessed(retry["batch_id"], "Cannot retry a completed response; this is a test.", True)

    def test_language_and_number_schema_flags(self):
        request = self.prepare([record(meaning="3 friends.")])
        result = response(request, ja="Three friends come to your home.", term="friends")
        self.factory.ingest(request["batch_id"], result, "fixture")
        flags = self.factory.export_master()[0]["qa"]["flags"]
        self.assertIn("TRANSLATION_ISSUE:unexpected_language", flags)
        self.assertIn("TRANSLATION_ISSUE:numeric_mismatch", flags)
        self.assertIn("TRANSLATION_ISSUE:headword_language", flags)

    def test_many_senses_and_long_original_preserved(self):
        original_term = "source " * 15
        request = self.prepare([record(original_term, "One. Two. Three. Four. Five.")])
        result = response(request)
        template = result["results"][0]["senses"][0]
        senses = []
        for i, quote in enumerate(["One.", "Two.", "Three.", "Four.", "Five."]):
            sense = copy.deepcopy(template)
            sense.update(sense_key=str(i))
            sense["units"][0].update(source_quote=quote, ja=f"意味{i}を示す。")
            # Numbers exist only in the fixture label, not its source. Use words instead.
            sense["units"][0]["ja"] = ["第一の意味を示す。", "第二の意味を示す。", "第三の意味を示す。", "第四の意味を示す。", "第五の意味を示す。"][i]
            senses.append(sense)
        result["results"][0]["senses"] = senses
        self.factory.ingest(request["batch_id"], result, "fixture")
        index = self.factory.build(self.root / "staging", 2)
        shard, pos = index["entries"][0][4:]
        payload = json.loads((self.root / "staging" / f"meanings-{shard:02d}.min.json").read_text(encoding="utf-8"))[pos]
        self.assertEqual(len(payload), 5)
        self.assertTrue(all(s["o"] == original_term for s in payload))

    def test_cache_import_without_reprocessing(self):
        self.finish()
        other = Factory(self.root / "other")
        try:
            self.assertEqual(other.import_cache(self.root / "work")["cache_records_imported"], 1)
            other.prepare([record()])
            self.assertIsNone(other.export_batch())
            self.assertEqual(other.statistics()["cache_hits"], 1)
            self.assertEqual(other.statistics()["llm_processed_count"], 0)
        finally:
            other.close()

    def test_qa_all_detects_checkpoint_inconsistency(self):
        self.factory.prepare([record()])
        with self.factory.db:
            self.factory.db.execute("UPDATE entries SET status='processing'")
        with self.assertRaisesRegex(PipelineError, "CHECKPOINT_INCONSISTENCY"):
            self.factory.qa_all()

    def test_qa_all_is_idempotent_without_llm(self):
        self.finish()
        first = self.factory.qa_all()
        self.assertEqual(first, self.factory.qa_all())
        self.assertEqual(first["llm_processed_count"], 1)


if __name__ == "__main__":
    unittest.main()
