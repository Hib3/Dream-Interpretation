"""Infrastructure tests. Synthetic translations are not linguistic evaluation evidence."""
import copy
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import dictionary_pipeline
import dictionary_codex_runner
from dictionary_pipeline import (Factory, PipelineError, atomic_json, cache_key, deterministic_qa, digest,
                                 dumps, materialize_transport_result, normalize, numeric_signature, review_context,
                                 response_schema, source_grounded_profiles, validate_result, validate_schema)


def record(term="dog", meaning="A faithful friend.", language="en", name="fixture"):
    source = dict(language=language, term=term, meaning=meaning, source_name=name,
                  source_reference="https://example.test/fixture", license_note="synthetic test only")
    return dict(entry_id=digest(source), source=source, origin=dict(entry_index=0, meaning_index=0),
                aggregate_sources=[dict(name=name)], aliases=[term.upper()])


def response(request, ja="忠実な友人を表す。", term="犬"):
    return {"results": [dict(id=item["id"], status="translated", action="replace", reason="",
        senses=[dict(sense_key="1", ja_term=term, aliases=[], units=[dict(
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
        by_term = {r["source"]["term"]: r for r in self.factory.export_master()}
        self.assertEqual(by_term["dog"]["translation"]["senses"][0]["units"][0]["source_quote"], "A faithful friend.")
        self.assertEqual(by_term["cat"]["translation"]["senses"][0]["units"][0]["source_quote"], "An independent friend.")

    def test_transport_ids_are_short_but_cache_keys_remain_internal(self):
        request = self.prepare([record(), record("cat", "An independent friend.")])
        ids = [item["id"] for item in request["items"]]
        self.assertEqual(ids, ["i0001", "i0002"])
        self.assertTrue(all(len(binding["cache_key"]) == 64 for binding in request["bindings"]))
        self.assertEqual({binding["id"] for binding in request["bindings"]}, set(ids))
        result = response(request)
        self.factory.ingest(request["batch_id"], result, "fixture")
        self.assertEqual(self.factory.db.execute("SELECT count(*) FROM cache").fetchone()[0], 2)
        self.assertTrue(all(len(row[0]) == 64 for row in self.factory.db.execute("SELECT key FROM cache")))

    def test_partial_salvage_never_fuzzy_matches_malformed_id(self):
        records = [record(), record("cat", "An independent friend."), record("owl", "A watchful sign.")]
        request = self.prepare(records)
        broken = response(request)
        expected_missing = broken["results"][1]["id"]
        broken["results"][1]["id"] = expected_missing[:-2]
        atomic_json(self.factory.root / "responses" / f"{request['batch_id']}.json", broken)
        with self.assertRaisesRegex(PipelineError, "ID_MISMATCH"):
            self.factory.ingest(request["batch_id"], broken, "fixture")
        stats = self.factory.salvage_response(request["batch_id"], "fixture")
        self.assertEqual((stats["salvaged"], stats["requeued"]), (2, 1))
        statuses = {row["source"]: row["status"] for row in self.factory.db.execute(
            "SELECT e.status, e.source FROM entries e")}
        self.assertEqual(statuses[json.dumps(records[0]["source"], ensure_ascii=False, sort_keys=True,
                                             separators=(",", ":"))], "complete")
        self.assertEqual(statuses[json.dumps(records[1]["source"], ensure_ascii=False, sort_keys=True,
                                             separators=(",", ":"))], "pending")
        self.assertEqual(statuses[json.dumps(records[2]["source"], ensure_ascii=False, sort_keys=True,
                                             separators=(",", ":"))], "complete")
        self.assertEqual(self.factory.db.execute("SELECT count(*) FROM cache").fetchone()[0], 2)
        bindings = json.loads(self.factory.db.execute(
            "SELECT request FROM batches WHERE id=?", (request["batch_id"],)).fetchone()[0])["bindings"]
        for binding in bindings:
            if binding["id"] != expected_missing:
                self.factory.db.execute("UPDATE cache SET key=? WHERE key=?",
                                        (binding["id"], binding["cache_key"]))
        self.factory.db.commit()
        repaired = self.factory.repair_salvaged_cache(request["batch_id"])
        self.assertEqual(repaired["migrated"], 2)
        self.assertTrue(all(len(key) == 64 and key == json.loads(payload)["id"]
                            for key, payload in self.factory.db.execute("SELECT key,payload FROM cache")))
        audit = json.loads(self.factory.db.execute(
            "SELECT detail FROM events WHERE kind='partial_salvage'").fetchone()[0])
        self.assertEqual(audit["missing_ids"], [expected_missing])
        self.assertEqual(audit["extra_ids"], [expected_missing[:-2]])

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
        result["results"][0]["senses"][0]["units"].pop()
        with self.assertRaisesRegex(PipelineError, "SOURCE_COVERAGE"):
            self.factory.ingest(request["batch_id"], result, "fixture")

    def test_every_batch_item_gets_source_coverage_validation(self):
        request = self.prepare([record(), record("cat", "An independent friend.")])
        result = response(request)
        result["results"][0]["senses"][0]["units"] = []
        with self.assertRaisesRegex(PipelineError, "SOURCE_COVERAGE"):
            self.factory.ingest(request["batch_id"], result, "fixture")

    def test_deterministic_segments_preserve_the_whole_source(self):
        request = self.prepare([record(meaning="First.  Second.\nThird.")])
        result = response(request)
        self.factory.ingest(request["batch_id"], result, "fixture")
        quotes = self.factory.export_master()[0]["translation"]["senses"][0]["units"]
        self.assertEqual([unit["source_quote"] for unit in quotes], ["First. Second. Third."])

    def test_verb_headword_ending_in_masu_is_not_sentence_like(self):
        source = record(meaning="To encourage someone.")["source"]
        result = dict(status="translated", senses=[dict(ja_term="励ます", flags=[], ambiguous=False,
            translation_confidence=.95, semantic_confidence=.95,
            units=[dict(ja="人を励ますことを表す。", source_quote="To encourage someone.", kind="interpretation")])])
        self.assertNotIn("TRANSLATION_ISSUE:suspicious_headword", deterministic_qa(result, source))

    def test_sound_label_headword_is_not_sentence_like(self):
        source = record(term="Hissing", meaning="To dream of hearing hissing noises.") ["source"]
        result = dict(status="translated", senses=[dict(ja_term="シューという音", flags=[], ambiguous=False,
            translation_confidence=.95, semantic_confidence=.95,
            units=[dict(ja="シューという音を聞く夢を表す。", source_quote=source["meaning"], kind="interpretation")])])
        self.assertNotIn("TRANSLATION_ISSUE:suspicious_headword", deterministic_qa(result, source))

    def test_sentence_like_headword_remains_flagged(self):
        source = record()["source"]
        result = dict(status="translated", senses=[dict(ja_term="これは良い知らせということを意味します", flags=[], ambiguous=False,
            translation_confidence=.95, semantic_confidence=.95,
            units=[dict(ja="良い知らせ。", source_quote=source["meaning"], kind="interpretation")])])
        self.assertIn("TRANSLATION_ISSUE:suspicious_headword", deterministic_qa(result, source))

    def test_named_person_headword_ending_in_toiu_namae_is_allowed(self):
        source = record(term="ali isim", meaning="To dream of seeing Ali's name.", language="tr")["source"]
        result = dict(status="translated", senses=[dict(ja_term="アリーという名前", flags=[], ambiguous=False,
            translation_confidence=.95, semantic_confidence=.95,
            units=[dict(ja="アリーという名前を見る夢を表す。", source_quote=source["meaning"], kind="interpretation")])])
        self.assertNotIn("TRANSLATION_ISSUE:suspicious_headword", deterministic_qa(result, source))

    def test_named_concept_headword_for_hearing_its_name_is_allowed(self):
        source = record(term="cennet ismini duymak", meaning="Rüyada cennet ismini duymak.", language="tr")["source"]
        result = dict(status="translated", senses=[dict(ja_term="天国という名を聞く", flags=[], ambiguous=False,
            translation_confidence=.95, semantic_confidence=.95,
            units=[dict(ja="夢で天国という名を聞くことを表す。", source_quote=source["meaning"], kind="interpretation")])])
        self.assertNotIn("TRANSLATION_ISSUE:suspicious_headword", deterministic_qa(result, source))

    def test_named_person_dream_headword_remains_a_search_phrase(self):
        source = record(term="ali isimli birini", meaning="Rüyada ali isimli birini görmek.", language="tr")["source"]
        result = dict(status="translated", senses=[dict(ja_term="アリという名前の人を見る", flags=[], ambiguous=False,
            translation_confidence=.95, semantic_confidence=.95,
            units=[dict(ja="アリという名前の人を見ることを表す。", source_quote=source["meaning"], kind="interpretation")])])
        self.assertNotIn("TRANSLATION_ISSUE:suspicious_headword", deterministic_qa(result, source))

    def test_named_plant_headword_with_toiu_is_allowed(self):
        source = record(term="ribas denilen ekşi otu", meaning="A sour herb called ribas.", language="tr")["source"]
        result = dict(status="translated", senses=[dict(ja_term="リバスという酸味のある草", flags=[], ambiguous=False,
            translation_confidence=.95, semantic_confidence=.95,
            units=[dict(ja="リバスと呼ばれる酸味のある草を表す。", source_quote=source["meaning"], kind="interpretation")])])
        self.assertNotIn("TRANSLATION_ISSUE:suspicious_headword", deterministic_qa(result, source))

    def test_hearing_a_name_headword_with_toiu_is_allowed(self):
        source = record(term="berat ismini duymak", meaning="To hear the name Berat.", language="tr")["source"]
        result = dict(status="translated", senses=[dict(ja_term="ベラトという名前を聞く", flags=[], ambiguous=False,
            translation_confidence=.95, semantic_confidence=.95,
            units=[dict(ja="ベラトという名前を聞く夢を表す。", source_quote=source["meaning"], kind="interpretation")])])
        self.assertNotIn("TRANSLATION_ISSUE:suspicious_headword", deterministic_qa(result, source))

    def test_unresolved_status_canonicalizes_redundant_action_without_losing_content(self):
        request = self.prepare()
        result = dict(id=request["items"][0]["id"], status="unresolved", action="replace",
                      reason="The classical source cannot be read safely.", senses=[])
        materialized = materialize_transport_result(result, {**request["items"][0], "pass": "translate"})
        self.assertEqual(materialized["action"], "unresolved")
        self.assertEqual(materialized["reason"], result["reason"])
        self.assertEqual(materialized["senses"], [])
        self.assertEqual(materialized["id"], result["id"])

    def test_unresolved_with_senses_is_still_rejected(self):
        source = record()["source"]
        result = dict(id="i0001", status="unresolved", action="replace", reason="Cannot resolve.",
                      senses=[dict(sense_key="1", ja_term="犬", aliases=[], units=[], tone="unknown",
                                   ambiguous=False, translation_confidence=.5,
                                   semantic_confidence=.5, flags=[])])
        normalized = materialize_transport_result(result, {"id": "i0001", "pass": "translate"})
        with self.assertRaisesRegex(PipelineError, "SCHEMA_FAILURE unresolved"):
            validate_result(normalized, source)

    def test_complete_meaning_is_one_immutable_source_segment(self):
        meaning = "A complete interpretation clause. " * 16
        request = self.prepare([record(meaning=meaning)])
        segments = request["items"][0]["source_segments"]
        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0]["text"], normalize(meaning))
        self.assertGreater(len(segments[0]["text"]), 320)

    def test_legacy_chunked_entry_is_requeued_once_for_scope_migration(self):
        request = self.prepare([record(meaning="First clause. " + "Second clause. " * 30)])
        self.finish(request)
        legacy = copy.deepcopy(request)
        legacy["prompt_version"] = "faithful-ja-6"
        item = legacy["items"][0]
        text = item["source_segments"][0]["text"]
        item["source_segments"] = [
            {"source_unit_id": "legacy-1", "text": text[:180]},
            {"source_unit_id": "legacy-2", "text": text[180:]},
        ]
        with self.factory.db:
            self.factory.db.execute("UPDATE batches SET request=? WHERE id=?", (dumps(legacy), request["batch_id"]))
        stats = self.factory.requeue_legacy_segmented("Rebuild outputs whose old 320-character chunks crossed meaning boundaries.")
        self.assertEqual(stats["requeued"], 1)
        self.assertEqual(stats["states"]["pending"], 1)
        row = self.factory.db.execute("SELECT result,pass_count,attempts FROM entries").fetchone()
        self.assertIsNone(row["result"])
        self.assertEqual(row["pass_count"], 0)
        self.assertEqual(row["attempts"], 1)
        second = self.factory.requeue_legacy_segmented("Check the migration is idempotent after requeue.")
        self.assertEqual(second["requeued"], 0)

    def test_legacy_chunked_entry_at_attempt_cap_is_quarantined(self):
        request = self.prepare([record(meaning="A source meaning long enough to model." + " Later clause." * 28)])
        self.finish(request)
        legacy = copy.deepcopy(request)
        legacy["prompt_version"] = "faithful-ja-6"
        item = legacy["items"][0]
        text = item["source_segments"][0]["text"]
        item["source_segments"] = [
            {"source_unit_id": "legacy-1", "text": text[:160]},
            {"source_unit_id": "legacy-2", "text": text[160:]},
        ]
        entry_id = legacy["bindings"][0]["entry_id"]
        with self.factory.db:
            self.factory.db.execute("UPDATE batches SET request=? WHERE id=?", (dumps(legacy), request["batch_id"]))
            self.factory.db.execute("UPDATE entries SET attempts=3 WHERE id=?", (entry_id,))
        stats = self.factory.requeue_legacy_segmented("Do not exceed retry limit while migrating old chunked translations.")
        row = self.factory.db.execute("SELECT status,result,flags FROM entries WHERE id=?", (entry_id,)).fetchone()
        self.assertEqual(stats["skipped_attempt_limit"], 1)
        self.assertEqual(row["status"], "unresolved")
        self.assertIsNone(row["result"])
        self.assertIn("PIPELINE_ISSUE:source_segment_scope_attempt_limit", row["flags"])

    def test_rejected_batch_scope_requeue_preserves_saved_response_and_source(self):
        request = self.prepare([record()])
        broken = response(request)
        broken["results"][0]["senses"][0]["units"][0]["ja"] = ""
        atomic_json(self.factory.root / "responses" / f"{request['batch_id']}.json", broken)
        with self.assertRaisesRegex(PipelineError, "EMPTY_TRANSLATION"):
            self.factory.ingest(request["batch_id"], broken, "fixture")
        stats = self.factory.requeue_rejected_batch(
            request["batch_id"], "Observed merged source chunks; rebuilding from one complete meaning segment.")
        self.assertEqual(stats["requeued"], 1)
        self.assertEqual(stats["states"]["pending"], 1)
        self.assertTrue((self.factory.root / "responses" / f"{request['batch_id']}.json").exists())
        row = self.factory.db.execute("SELECT source,result,flags FROM entries").fetchone()
        self.assertIsNone(row["result"])
        self.assertEqual(json.loads(row["source"]), record()["source"])
        self.assertIn("PIPELINE_ISSUE:source_segment_scope", row["flags"])
        self.assertIsNotNone(self.factory.meta("halt"))

    def test_provenance_is_assigned_deterministically_and_unit_ids_are_not_model_fields(self):
        item = {"pass": "translate", "source_segments": [
            {"source_unit_id": "a", "text": "Complete source meaning."}]}
        result = response({"items": [{"id": "x"}]})["results"][0]
        validate_schema({"results": [result]}, response_schema())
        converted = materialize_transport_result(result, item)
        unit = converted["senses"][0]["units"][0]
        self.assertEqual(unit["source_quote"], "Complete source meaning.")
        result["senses"][0]["units"][0]["source_unit_id"] = "untrusted"
        with self.assertRaisesRegex(PipelineError, "SCHEMA_FAILURE"):
            validate_schema({"results": [result]}, response_schema())

    def test_multiple_semantic_units_keep_the_same_full_source_quote(self):
        item = {"pass": "translate", "source_segments": [
            {"source_unit_id": "a", "text": "Complete source meaning."}]}
        result = response({"items": [{"id": "x"}]})["results"][0]
        extra = copy.deepcopy(result["senses"][0]["units"][0])
        extra["ja"] = "追加の原文根拠付き解釈。"
        result["senses"][0]["units"].append(extra)
        converted = materialize_transport_result(result, item)
        self.assertEqual([u["source_quote"] for u in converted["senses"][0]["units"]],
                         ["Complete source meaning."] * 2)

    def test_review_context_omits_redundant_quotes_without_mutating_saved_result(self):
        quote = "A complete source meaning. " * 600
        current = {"status": "translated", "senses": [
            {"sense_key": str(index), "units": [{"kind": "interpretation", "ja": "訳文", "source_quote": quote}]}
            for index in range(65)]}
        context = review_context(current)
        self.assertNotIn("source_quote", context["senses"][0]["units"][0])
        self.assertEqual(current["senses"][0]["units"][0]["source_quote"], quote)
        self.assertLess(len(dumps(context)), len(dumps(current)) // 50)

    def test_internal_quote_word_change_still_rejected(self):
        request = self.prepare([record(meaning="First. Second.")])
        result = response(request)
        unit = result["results"][0]["senses"][0]["units"][0]
        unit["source_quote"] = "tampered source"
        with self.assertRaisesRegex(PipelineError, "SCHEMA_FAILURE"):
            self.factory.ingest(request["batch_id"], result, "fixture")

    def test_long_meanings_not_truncated(self):
        meaning = "A faithful friend. " * 600
        request = self.prepare([record(meaning=meaning)])
        self.assertEqual(normalize(" ".join(s["text"] for s in request["items"][0]["source_segments"])), meaning.strip())
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

    def test_source_grounded_profiles_bind_to_exact_source_and_selected_sense(self):
        source = record()
        profiles = [
            {"id": "dog-grounded", "term": "犬", "source": {"orig": "dog", "name": "fixture",
             "quote": "A faithful friend."}},
            {"id": "wrong-quote", "term": "犬", "source": {"orig": "dog", "name": "fixture",
             "quote": "a hostile enemy"}},
            {"id": "unproven", "term": "犬", "evidence": "not source-backed"},
        ]
        self.finish()
        root = self.root / "profile-root"
        profile_path = root / "data" / "ja" / "reading-semantics.json"
        profile_path.parent.mkdir(parents=True)
        atomic_json(profile_path, {"version": 1, "profiles": profiles})
        with patch.object(dictionary_pipeline, "ROOT", root):
            self.factory.build(self.root / "profile-build", 2)
        built = json.loads((self.root / "profile-build" / "reading-semantics.json").read_text(encoding="utf-8"))
        self.assertEqual([profile["id"] for profile in built["profiles"]], ["dog-grounded"])
        self.assertEqual(built["profiles"][0]["source"]["sense_id"], digest([source["entry_id"], "1"]))
        built_index = json.loads((self.root / "profile-build" / "terms.min.json").read_text(encoding="utf-8"))
        row = next(row for row in built_index["entries"] if row[0] == "犬")
        payload = json.loads((self.root / "profile-build" / f"meanings-{row[4]:02d}.min.json").read_text(encoding="utf-8"))
        self.assertEqual(payload[row[5]][0]["id"], built["profiles"][0]["source"]["sense_id"])
        ambiguous = self.factory.export_master()
        ambiguous[0]["translation"]["senses"].append(copy.deepcopy(ambiguous[0]["translation"]["senses"][0]))
        ambiguous[0]["translation"]["senses"][1]["sense_key"] = "2"
        self.assertEqual(source_grounded_profiles(ambiguous, [profiles[0]]), [])

    def test_source_verified_headword_alias_points_to_same_sense(self):
        source = record()
        self.finish()
        root = self.root / "alias-root"
        profile_path = root / "data" / "ja" / "reading-semantics.json"
        profile_path.parent.mkdir(parents=True)
        atomic_json(profile_path, {"version": 1, "profiles": [{
            "id": "dog-alias", "term": "いぬ", "evidence": "犬の根拠",
            "source": {"orig": "dog", "name": "fixture", "quote": "A faithful friend.", "master_only": True},
        }]})
        with patch.object(dictionary_pipeline, "ROOT", root):
            index = self.factory.build(self.root / "alias-build", 2)
        built = json.loads((self.root / "alias-build" / "reading-semantics.json").read_text(encoding="utf-8"))
        profile = built["profiles"][0]
        self.assertEqual(profile["source"]["runtime_term"], "犬")
        self.assertEqual(profile["source"]["sense_id"], digest([source["entry_id"], "1"]))
        entries = {row[0]: row for row in index["entries"]}
        self.assertIn("犬", entries)
        self.assertIn("いぬ", entries)
        for term in ("犬", "いぬ"):
            row = entries[term]
            shard = json.loads((self.root / "alias-build" / f"meanings-{row[4]:02d}.min.json").read_text(encoding="utf-8"))
            self.assertIn(profile["source"]["sense_id"], {sense["id"] for sense in shard[row[5]]})

    def test_curated_profile_can_use_exact_quote_without_promoting_unresolved_translation(self):
        source = record(term="eating", meaning="Food is a metaphor for fulfilment.", name="fixture")
        self.finish(self.prepare([source]))
        with self.factory.db:
            self.factory.db.execute("UPDATE entries SET status='unresolved' WHERE id=?", (source["entry_id"],))
        root = self.root / "curated-root"
        profile_path = root / "data" / "ja" / "reading-semantics.json"
        profile_path.parent.mkdir(parents=True)
        atomic_json(profile_path, {"version": 1, "profiles": [{
            "id": "eating-curated", "term": "食べる", "evidence": "食べ物は心の中の充足を象徴する。",
            "claims": [{"id": "fulfilment", "sentence": "食べ物は心の中の充足を表す夢と読めます。"}],
            "source": {"orig": "eating", "name": "fixture", "quote": "Food is a metaphor for fulfilment.",
                       "master_only": True, "curated_override": True},
        }]})
        with patch.object(dictionary_pipeline, "ROOT", root):
            index = self.factory.build(self.root / "curated-build", 2)
        built = json.loads((self.root / "curated-build" / "reading-semantics.json").read_text(encoding="utf-8"))
        profile = built["profiles"][0]
        self.assertTrue(profile["source"]["curated_only"])
        self.assertEqual(index["source_meaning_count"], 1)
        self.assertEqual(json.loads((self.root / "curated-build" / "build-report.json").read_text())['unresolved'], 1)
        self.assertEqual(json.loads((self.root / "curated-build" / "build-report.json").read_text())['curated_profiles'], 1)
        row = next(row for row in index["entries"] if row[0] == "食べる")
        shard = json.loads((self.root / "curated-build" / f"meanings-{row[4]:02d}.min.json").read_text(encoding="utf-8"))
        self.assertEqual(shard[row[5]][0]["m"], [profile["evidence"]])

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

    def test_halted_batch_allows_only_targeted_review_of_its_flagged_entry(self):
        flagged = record(term="three friends", meaning="3 friends.")
        clean = record(term="dog", meaning="A faithful friend.")
        self.factory.prepare([flagged, clean], phase="phase2")
        batch = self.factory.export_batch(limit=2)
        self.factory.ingest(batch["batch_id"], response(batch), "fixture")
        self.assertIsNotNone(self.factory.meta("halt"))
        review = self.factory.export_batch(pass_kind="review", entry_ids=[flagged["entry_id"]])
        self.assertEqual([binding["entry_id"] for binding in review["bindings"]], [flagged["entry_id"]])
        self.assertEqual(review["pass"], "review")

    def test_concurrent_batches_reserve_distinct_entries_and_preserve_first_halt(self):
        numeric = record(term="three friends", meaning="3 friends.")
        clean = record(term="dog", meaning="A faithful friend.")
        self.factory.prepare([numeric, clean], phase="phase2")
        other = Factory(self.root / "work")
        try:
            first = self.factory.export_batch(limit=1)
            second = other.export_batch(limit=1, allow_concurrent=True)
            first_entry = first["bindings"][0]["entry_id"]
            second_entry = second["bindings"][0]["entry_id"]
            self.assertNotEqual(first_entry, second_entry)
            self.assertEqual({first_entry, second_entry}, {numeric["entry_id"], clean["entry_id"]})
            numeric_batch, clean_batch = ((first, second) if first_entry == numeric["entry_id"]
                                          else (second, first))
            self.factory.ingest(numeric_batch["batch_id"], response(numeric_batch), "fixture")
            self.assertEqual(self.factory.meta("halt")["batch_id"], numeric_batch["batch_id"])
            other.ingest(clean_batch["batch_id"], response(clean_batch), "fixture")
            self.assertEqual(self.factory.meta("halt")["batch_id"], numeric_batch["batch_id"])
            self.assertEqual(self.factory.statistics()["states"]["processing"], 0)
        finally:
            other.close()

    def test_review_keep_cannot_paraphrase(self):
        request = self.prepare()
        result = response(request)
        result["results"][0]["senses"][0]["flags"] = ["SOURCE_QUALITY_ISSUE"]
        self.factory.ingest(request["batch_id"], result, "fixture")
        review = self.factory.export_batch(pass_kind="review")
        kept = copy.deepcopy(review["items"][0]["current"])
        kept["senses"] = []
        kept.update(id=review["items"][0]["id"], action="keep", reason="The source is archaic; the translation is faithful.")
        changed = response(review)["results"][0]
        changed.update(action="keep", reason="Keep test must not return replacement senses.")
        with self.assertRaisesRegex(PipelineError, "keep requires empty senses"):
            self.factory.ingest(review["batch_id"], {"results": [changed]}, "fixture")
        self.factory.ingest(review["batch_id"], {"results": [kept]}, "fixture")
        self.factory.acknowledge_halt("Rejected synthetic paraphrase; unchanged review response validated.")
        self.assertEqual(self.factory.statistics()["states"]["complete"], 1)
        self.factory.build(self.root / "staging", 2)

    def test_review_keep_preserves_current_confidence_and_translation(self):
        request = self.prepare()
        result = response(request)
        result["results"][0]["senses"][0]["translation_confidence"] = .7
        self.factory.ingest(request["batch_id"], result, "fixture")
        self.factory.acknowledge_halt("Synthetic low-confidence case is being sent to the explicit second-pass review lane.")
        review = self.factory.export_batch(pass_kind="review")
        kept = copy.deepcopy(review["items"][0]["current"])
        original_sense = copy.deepcopy(kept["senses"][0])
        kept["senses"] = []
        kept.update(id=review["items"][0]["id"], action="keep", reason="Rechecked against the complete source; the Japanese is accurate.")
        self.factory.ingest(review["batch_id"], {"results": [kept]}, "fixture")
        master = self.factory.export_master()[0]
        self.assertEqual(review_context(master["translation"])["senses"][0], original_sense)
        self.assertEqual(master["qa"]["status"], "unresolved")

    def test_review_empty_replace_with_explicit_keep_reason_preserves_current_translation(self):
        request = self.prepare([record(), record("cat", "A separate companion."),
                                record("bridge", "A third companion."),
                                record("fish", "A fourth companion.")])
        first = response(request)
        for result in first["results"]:
            result["senses"][0]["flags"] = ["SOURCE_QUALITY_ISSUE:source_noise"]
        self.factory.ingest(request["batch_id"], first, "fixture")
        review = self.factory.export_batch(pass_kind="review")
        originals = {item["id"]: copy.deepcopy(item["current"]["senses"]) for item in review["items"]}
        mistaken = response(review)
        reasons = ["内容を変更せず既存の翻訳を採用します。",
                   "「アーシュレのスープを食べる」は意味を保っているため、訳文を修正せずそのまま維持します。",
                   "原文の列挙と帰属先を保っている現訳は正確。",
                   "現訳は原文の意味を保っています。対象も合っています。"]
        for result, reason in zip(mistaken["results"], reasons):
            result.update(action="replace", senses=[], reason=reason)
        explicit_variant = copy.deepcopy(mistaken["results"][1])
        explicit_variant["reason"] = "「書物たち」ではなく原文どおり「書物」とし、現訳は妥当です。訂正箇所がないため保持します。"
        normalized = materialize_transport_result(explicit_variant, {**review["items"][1], "pass": "review"})
        self.assertEqual(normalized["senses"][0]["units"][0]["ja"],
                         originals[review["items"][1]["id"]][0]["units"][0]["ja"])
        for reason in ("現在の訳は意味を保っているため、そのまま採用。",
                       "夢の解釈も忠実に訳出されている。",
                       "夢解釈が正確に訳されている。",
                       "現訳は正確。",
                       "正しく訳されている。",
                       "正しく翻訳されている。"):
            variant = copy.deepcopy(explicit_variant)
            variant["reason"] = reason
            normalized = materialize_transport_result(variant, {**review["items"][1], "pass": "review"})
            self.assertEqual(normalized["senses"][0]["units"][0]["ja"],
                             originals[review["items"][1]["id"]][0]["units"][0]["ja"])
        no_error_variant = copy.deepcopy(explicit_variant)
        no_error_variant["reason"] = "訳に誤りはないため保持します。"
        normalized = materialize_transport_result(no_error_variant, {**review["items"][1], "pass": "review"})
        self.assertEqual(normalized["senses"][0]["units"][0]["ja"],
                         originals[review["items"][1]["id"]][0]["units"][0]["ja"])
        negative_variant = copy.deepcopy(explicit_variant)
        negative_variant["reason"] = "現訳は正確ではない。"
        with self.assertRaisesRegex(PipelineError, "EMPTY_TRANSLATION"):
            materialize_transport_result(negative_variant, {**review["items"][1], "pass": "review"})
        self.factory.ingest(review["batch_id"], mistaken, "fixture")
        stored = {row["id"]: json.loads(row["result"]) for row in
                  self.factory.db.execute("SELECT id,result FROM entries")}
        for binding in review["bindings"]:
            self.assertEqual(review_context({"senses": stored[binding["entry_id"]]["senses"]})["senses"],
                             originals[binding["id"]])
        saved = json.loads((self.factory.root / "responses" / f"{review['batch_id']}.json").read_text(encoding="utf-8"))
        self.assertTrue(all(result["action"] == "replace" and not result["senses"]
                            for result in saved["results"]))

    def test_review_quarantine_is_not_counted_as_suspicious_production_output(self):
        request = self.prepare()
        first = response(request)
        first["results"][0]["senses"][0]["translation_confidence"] = .7
        self.factory.ingest(request["batch_id"], first, "fixture")
        self.factory.acknowledge_halt("Initial low-confidence translation is routed to review.")
        review = self.factory.export_batch(pass_kind="review")
        result = response(review)
        result["results"][0]["senses"][0]["translation_confidence"] = .7
        self.factory.ingest(review["batch_id"], result, "fixture")
        self.assertEqual(self.factory.statistics()["states"]["unresolved"], 1)
        self.assertIsNone(self.factory.meta("halt"))

    def test_numeric_mismatch_can_get_one_targeted_repair_after_second_pass(self):
        source = record(meaning="120 guests.")
        request = self.prepare([source])
        first = response(request, ja="多くの来客を表す。")
        self.factory.ingest(request["batch_id"], first, "fixture")
        self.factory.acknowledge_halt("A detected numeric mismatch is routed through bounded review.")
        second = self.factory.export_batch(pass_kind="review")
        self.factory.ingest(second["batch_id"], response(second, ja="多くの来客を表す。"), "fixture")
        self.assertEqual(self.factory.statistics()["states"]["unresolved"], 1)
        self.factory.acknowledge_halt("The second pass retained a numeric mismatch; allow one targeted correction only.")
        repair = self.factory.export_batch(pass_kind="review")
        self.assertEqual(len(repair["items"]), 1)
        self.factory.ingest(repair["batch_id"], response(repair, ja="120人の来客を表す。"), "fixture")
        self.assertEqual(self.factory.statistics()["states"]["complete"], 1)

    def test_source_language_leakage_allows_explicit_original_spelling_note(self):
        source = record(term="basının", meaning="A head bleeding.", language="tr")
        item = {"id": "x", "source_segments": [{"source_unit_id": "x1", "text": "A head bleeding."}]}
        result = materialize_transport_result(response({"items": [item]},
            ja="夢で頭（原文表記「basının」）から出血するのを見る。")['results'][0], item)
        self.assertNotIn("TRANSLATION_ISSUE:source_language_leakage", deterministic_qa(result, source["source"]))
        other = record(term="test2", meaning="Another source.", language="tr")
        item = {"id": "y", "source_segments": [{"source_unit_id": "y1", "text": "Another source."}]}
        leaked = materialize_transport_result(response({"items": [item]}, ja="Rüyada görmek。")['results'][0], item)
        self.assertIn("TRANSLATION_ISSUE:source_language_leakage", deterministic_qa(leaked, other["source"]))
        reference = record(term="reference", meaning="Related titles: Şiddet ve Darlık.", language="tr")
        item = {"id": "ref", "source_segments": [{"source_unit_id": "ref1", "text": reference["source"]["meaning"]}]}
        cross_reference = materialize_transport_result(response(
            {"items": [item]}, ja="項目の説明。「Şiddet ve Darlık（暴力と困窮）」「Daralmak Darlık（困窮する）」も参照。")['results'][0], item)
        flags = deterministic_qa(cross_reference, reference["source"])
        self.assertNotIn("TRANSLATION_ISSUE:source_language_leakage", flags)

    def test_source_language_leakage_allows_quoted_source_term_in_source_note_only(self):
        source = record(term="başarısız olmak", meaning="An unfinished source entry.", language="tr")
        item = {"id": "note", "source_segments": [{"source_unit_id": "note1", "text": "An unfinished source entry."}]}
        note = response({"items": [item]}, ja="末尾の「Başarısız」は文として未完である。")['results'][0]
        note["senses"][0]["units"][0]["kind"] = "source_note"
        result = materialize_transport_result(note, item)
        self.assertNotIn("TRANSLATION_ISSUE:source_language_leakage", deterministic_qa(result, source["source"]))
        leaked = response({"items": [item]}, ja="末尾は未完。Başarısız は失敗を意味する。")['results'][0]
        leaked["senses"][0]["units"][0]["kind"] = "source_note"
        result = materialize_transport_result(leaked, item)
        self.assertIn("TRANSLATION_ISSUE:source_language_leakage", deterministic_qa(result, source["source"]))

    def test_source_language_leakage_ignores_turkish_dotless_i_list_marker(self):
        source = record(term="Süleyman", meaning="A list of interpretations.", language="tr")
        item = {"id": "list", "source_segments": [{"source_unit_id": "list1", "text": "A list of interpretations."}]}
        result = response({"items": [item]}, ja="a) 慈悲。ı）イスラームにおける力。")['results'][0]
        result = materialize_transport_result(result, item)
        self.assertNotIn("TRANSLATION_ISSUE:source_language_leakage", deterministic_qa(result, source["source"]))

    def test_source_language_leakage_allows_exact_quoted_ambiguous_headword(self):
        source = record(term="arka üst dış", meaning="An unclear dream symbol.", language="tr")
        item = {"id": "x", "source_segments": [{"source_unit_id": "x1", "text": source["source"]["meaning"]}]}
        result = materialize_transport_result(response({"items": [item]},
            ja="「arka üst dış」は意味が不明確な見出し語である。")['results'][0], item)
        result["senses"][0]["flags"] = ["SEMANTIC_AMBIGUITY:declared", "TRANSLATION_ISSUE:low_confidence"]
        flags = deterministic_qa(result, source["source"])
        self.assertNotIn("TRANSLATION_ISSUE:source_language_leakage", flags)
        self.assertIn("SEMANTIC_AMBIGUITY:declared", flags)

    def test_source_language_leakage_allows_quoted_truncated_source_tail(self):
        source = record(term="başkasının siyah giydiğini", meaning="A fragmented interpretation.", language="tr")
        item = {"id": "x", "source_segments": [{"source_unit_id": "x1", "text": source["source"]["meaning"]}]}
        notes = [
            "原文の末尾は「başarısızlıkların（失敗の）」で途切れている。",
            "原文は最後の「Tanıdık（親しい知人）」で途切れている。",
            "配偶者（原文では「eş」で途切れている）、よい出来事に遭遇する。",
            "原文はここで「eş（配偶者）」とだけ述べ、その先の関係が途切れている。",
        ]
        for note in notes:
            result = materialize_transport_result(response({"items": [item]},
                ja="夢の意味を表す。" + note)['results'][0], item)
            flags = deterministic_qa(result, source["source"])
            self.assertNotIn("TRANSLATION_ISSUE:source_language_leakage", flags)

    def test_source_language_leakage_ignores_quoted_source_tail_in_interpretation(self):
        source = record(term="askerlik yaptığını", meaning="A truncated Turkish source.", language="tr")
        request = self.prepare([source])
        result = response(request, ja=(
            "夢の意味を表す。原文末尾の「hem ağız tadının（そして心の満足が）」は文が途中で切れている。"))
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:source_language_leakage",
                         deterministic_qa(materialized, source["source"]))

    def test_source_language_leakage_allows_quoted_unclear_source_phrase_with_note(self):
        source = record(term="bilezik", meaning="A corrupted Turkish fragment.", language="tr")["source"]
        request = self.prepare([record(term="bilezik", meaning="A corrupted Turkish fragment.", language="tr")])
        result = response(request, ja="「çok bir duruma düşeceği」（原文の表現は意味不明瞭）とされる。")
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:source_language_leakage", deterministic_qa(materialized, source))

    def test_source_language_leakage_allows_quoted_source_phrase_with_literal_gloss(self):
        source = record(term="altına düşmek", meaning="A Turkish idiom.", language="tr")["source"]
        request = self.prepare([record(term="altına düşmek", meaning="A Turkish idiom.", language="tr")])
        result = response(request, ja="「altına düşmek」（直訳では「その下に落ちる」）を表す。")
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:source_language_leakage", deterministic_qa(materialized, source))

    def test_source_language_leakage_allows_explicit_explanatory_truncation_notes(self):
        source = record(term="dantel yaka", meaning="A fragmented interpretation.", language="tr")["source"]
        item = {"id": "x", "source_segments": [{"source_unit_id": "x1", "text": source["meaning"]}]}
        notes = [
            "原文の「dişinden（歯から）」に続く「nazara geleceğine ve güzelliği」は文が崩れており、意味を確定できない。",
            "〔原文は「tabiri caizse dişinden」と途中で切れており、意味を確定できない〕",
            "原文末尾の「rüya sahibinin üzgün」は後続部分が欠けており、文意を確定できない。",
            "原文では「yıpranmadan（疲弊せずに）」に続く部分が欠落している。",
            "〔原文は「yaşamının çalkantı」で文が途切れており、述語は不明〕",
            "〔原文は「güzelliği」と文が途切れており、述語や意味を確定できない〕",
            "末尾の「küslüğün」に続く意味は原文が途切れているため不明。",
            "原文は「artık çok daha hu」で途切れている。",
            "原文末尾の「geçimden」以降は文が欠けており、関係は不明。",
        ]
        for note in notes:
            result = materialize_transport_result(response({"items": [item]},
                ja="原文は不完全である。" + note)['results'][0], item)
            flags = deterministic_qa(result, source)
            self.assertNotIn("TRANSLATION_ISSUE:source_language_leakage", flags)

    def test_turkish_namaz_is_not_confused_with_japanese_catfish(self):
        source = record(term="birilerini namaz kılarken", meaning="Rüyada namaz kılan insan görmek.", language="tr")
        item = {"id": "namaz", "source_segments": [{"source_unit_id": "n1", "text": source["source"]["meaning"]}]}
        wrong = materialize_transport_result(response({"items": [item]}, ja="人々が礼拝（ナマズ）をしている。")['results'][0], item)
        correct = materialize_transport_result(response({"items": [item]}, ja="人々が礼拝をしている。")['results'][0], item)
        self.assertIn("TRANSLATION_ISSUE:religious_term", deterministic_qa(wrong, source["source"]))
        self.assertNotIn("TRANSLATION_ISSUE:religious_term", deterministic_qa(correct, source["source"]))

    def test_qa_accepts_declared_underscore_flag_constants(self):
        source = record()
        request = self.prepare([source])
        result = response(request)
        result["results"][0]["senses"][0]["flags"] = [
            "SEMANTIC_AMBIGUITY_UNRESOLVED", "SOURCE_QUALITY_ISSUE_INCOMPLETE_CLAUSE"]
        self.factory.ingest(request["batch_id"], result, "fixture")
        self.assertNotIn("PIPELINE_ISSUE:unrecognized_flag", self.factory.export_master()[0]["qa"]["flags"])

    def test_turkish_spelled_out_number_matches_japanese_digit(self):
        source = record(meaning="Her şeyin etiketi iki katına çıkıp.", language="tr")
        request = self.prepare([source])
        self.factory.ingest(request["batch_id"], response(request, ja="すべての価格が2倍になる。"), "fixture")
        flags = self.factory.export_master()[0]["qa"]["flags"]
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch", flags)

    def test_sequential_circled_list_markers_are_not_semantic_numbers(self):
        source = record(language="tr", meaning=(
            "Bir kimsenin rüyada Hadid Sûresini okuması güzel ahlâka delâlet eder. "
            "a) Din yoluna sülük, b) Ahlâk güzelliği, c) İman kuvveti, d) İbadet."))
        request = self.prepare([source])
        result = response(request, ja=(
            "ハディード章を読む夢は立派な道徳を表す。①信仰の道、②道徳の美しさ、"
            "③信仰の強さ、④礼拝。"))
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch",
                         deterministic_qa(materialized, source["source"]))

    def test_turkish_forty_years_of_hatir_idiom_matches_japanese_long_years(self):
        source = record(meaning="Kırk yıllık hatırı bir kelime ile sileceğine.", language="tr")
        request = self.prepare([source])
        self.factory.ingest(request["batch_id"], response(request, ja="長年の恩を一言で消す。"), "fixture")
        flags = self.factory.export_master()[0]["qa"]["flags"]
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch", flags)

    def test_turkish_bir_sure_daha_is_not_a_numeric_one(self):
        source = record(language="tr", meaning="Yaşanan memnuniyetsizliğin bir süre daha devam edeceğine.")
        request = self.prepare([source])
        self.factory.ingest(request["batch_id"], response(request, ja="現在の不満がもうしばらく続く。"), "fixture")
        flags = self.factory.export_master()[0]["qa"]["flags"]
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch", flags)

    def test_turkish_bir_muddet_daha_is_not_a_numeric_one(self):
        source = record(language="tr", meaning=(
            "Sıkıntılarınızın bir müddet daha devam edeceğine, uzun yıllardan beri "
            "sürdürdüğü bir dostluğun bitmesine sebep olacak bir sorun yaşayacağına."))
        request = self.prepare([source])
        self.factory.ingest(request["batch_id"], response(
            request, ja="悩みはもうしばらく続き、長年の友情が終わる原因となる問題を経験する。"), "fixture")
        flags = self.factory.export_master()[0]["qa"]["flags"]
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch", flags)

    def test_turkish_bir_doneme_is_indefinite_period(self):
        source = record(language="tr", meaning="Daha sıkıntılı bir döneme girileceğine.")
        request = self.prepare([source])
        self.factory.ingest(request["batch_id"], response(request, ja="さらに苦しい時期に入る。"), "fixture")
        flags = self.factory.export_master()[0]["qa"]["flags"]
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch", flags)

    def test_turkish_first_time_after_long_period_matches_hajimete(self):
        source = record(language="tr", meaning="Uzun bir dönemden sonra ilk kez rahat edileceğine.")
        request = self.prepare([source])
        self.factory.ingest(request["batch_id"], response(request, ja="長い期間を経て初めて安堵する。"), "fixture")
        flags = self.factory.export_master()[0]["qa"]["flags"]
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch", flags)

    def test_turkish_katlanarak_art_is_not_an_explicit_double(self):
        source = record(language="tr", meaning=(
            "Aile bireylerinden birinde hastalık başlayacağına, maddi durumunun "
            "ilerleyen yıllarda katlanarak artacağına."))
        request = self.prepare([source])
        self.factory.ingest(request["batch_id"], response(request, ja="家族の一人に病気が現れ、今後の財産が倍増する。"), "fixture")
        flags = self.factory.export_master()[0]["qa"]["flags"]
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch", flags)

    def test_turkish_bir_sebeple_is_reason_not_numeric_one(self):
        source = record(language="tr", meaning="Bir sebeple daha çok sabır göstermesi gerektiğine.")
        request = self.prepare([source])
        self.factory.ingest(request["batch_id"], response(request, ja="そのため、より多くの忍耐を示す必要がある。"), "fixture")
        flags = self.factory.export_master()[0]["qa"]["flags"]
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch", flags)

    def test_turkish_bir_turlu_idiom_is_not_numeric_one(self):
        source = record(language="tr", meaning="Bir türlü ilerleme kaydedilemeyeceğine.")
        request = self.prepare([source])
        self.factory.ingest(request["batch_id"], response(request, ja="なかなか進歩できない。"), "fixture")
        flags = self.factory.export_master()[0]["qa"]["flags"]
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch", flags)

    def test_turkish_bir_sure_is_an_indefinite_period(self):
        source = record(language="tr", meaning="Kısa bir süre içinde ayrılacağına.")
        request = self.prepare([source])
        self.factory.ingest(request["batch_id"], response(request, ja="短期間のうちに離れる。"), "fixture")
        flags = self.factory.export_master()[0]["qa"]["flags"]
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch", flags)

    def test_turkish_bir_dedigini_iki_etmek_idiom_accepts_natural_paraphrase(self):
        source = record(language="tr", meaning="Kişinin bir dediğini iki etmeyeceğine.")
        request = self.prepare([source])
        self.factory.ingest(request["batch_id"], response(
            request, ja="言われたことには何でも応じる。"), "fixture")
        flags = self.factory.export_master()[0]["qa"]["flags"]
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch", flags)

    def test_turkish_ikisi_tek_basina_and_japanese_negative_quantifier(self):
        source = record(language="tr", meaning=(
            "İkisi de rahat edeceğine, tek başına karar veremeyeceğine."))
        request = self.prepare([source])
        self.factory.ingest(request["batch_id"], response(
            request, ja="二人とも安らぎ、一人では決められず、何一つ不足を感じない。"), "fixture")
        flags = self.factory.export_master()[0]["qa"]["flags"]
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch", flags)

    def test_turkish_bir_nefes_al_is_not_a_numeric_one(self):
        source = record(language="tr", meaning="Rahat bir nefes alınacağına.")
        request = self.prepare([source])
        self.factory.ingest(request["batch_id"], response(request, ja="ほっと一息つける。"), "fixture")
        flags = self.factory.export_master()[0]["qa"]["flags"]
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch", flags)

    def test_turkish_bir_zamandan_beri_is_not_a_numeric_one(self):
        source = record(language="tr", meaning="Çok uzun bir zamandan beri yapılan iş.")
        request = self.prepare([source])
        self.factory.ingest(request["batch_id"], response(request, ja="長い間続けてきた仕事。"), "fixture")
        flags = self.factory.export_master()[0]["qa"]["flags"]
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch", flags)

    def test_turkish_two_witnesses_but_one_arrives_checks_both_numbers(self):
        source = record(language="tr", meaning=(
            "iki şahidin gerekli olduğu halde ancak bir şahidin geldiğini görmek."))
        request = self.prepare([source])
        self.factory.ingest(request["batch_id"], response(request, ja="二人の証人が必要なのに一人しか来ない夢。"), "fixture")
        flags = self.factory.export_master()[0]["qa"]["flags"]
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch", flags)

    def test_turkish_couple_in_headword_is_not_a_body_count(self):
        source = record(language="tr", term="bir çiftin kavga ettiğini", meaning=(
            "Rüyada bir çiftin kavga ettiğini görmek çok hayırlı kişiler ile "
            "irtibata geçip işlerini büyüteceğine."))
        request = self.prepare([source])
        self.factory.ingest(request["batch_id"], response(request, ja="善良な人々と知り合い、仕事を拡大する。"), "fixture")
        flags = self.factory.export_master()[0]["qa"]["flags"]
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch", flags)

    def test_turkish_one_in_threefold_return_matches_threefold_japanese(self):
        source = record(language="tr", meaning="bir koyduğu işten üç alacağına.")
        request = self.prepare([source])
        self.factory.ingest(request["batch_id"], response(request, ja="投じたものの三倍を得る。"), "fixture")
        flags = self.factory.export_master()[0]["qa"]["flags"]
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch", flags)

    def test_turkish_indefinite_article_in_avalanche_metaphor_is_not_counted(self):
        source = record(language="tr", meaning="Engellerin adeta bir çığ gibi büyüyeceğine.")
        request = self.prepare([source])
        self.factory.ingest(request["batch_id"], response(request, ja="障害が雪崩のように大きくなる。"), "fixture")
        flags = self.factory.export_master()[0]["qa"]["flags"]
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch", flags)

    def test_quoted_footnote_marker_is_not_semantic_number(self):
        self.assertFalse(numeric_signature("…geleceksiniz…”[122]", "tr"))

    def test_turkish_one_sikkeli_altin_and_one_altin_are_counted(self):
        self.assertEqual(numeric_signature("Bir sikkeli altın ve bir altını zayi ettiğini", "tr"),
                         numeric_signature("金貨一枚と金貨を一枚失う", "ja"))

    def test_turkish_birine_to_friend_or_relative_is_not_numeric_count(self):
        source = record(language="tr", meaning=(
            "Dost ya da akrabalardan birine verilen sözlerin ödenemeyeceğine."))
        request = self.prepare([source])
        self.factory.ingest(request["batch_id"], response(request, ja="友人や親族にした約束を果たせない。"), "fixture")
        flags = self.factory.export_master()[0]["qa"]["flags"]
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch", flags)

    def test_turkish_forty_years_numeric_still_checked_without_idiom_equivalent(self):
        source = record(meaning="Kırk yıllık hatırı bir kelime ile sileceğine.", language="tr")
        request = self.prepare([source])
        self.factory.ingest(request["batch_id"], response(request, ja="恩を一言で消す。"), "fixture")
        flags = self.factory.export_master()[0]["qa"]["flags"]
        self.assertIn("TRANSLATION_ISSUE:numeric_mismatch", flags)

    def test_turkish_bir_baska_rivayet_is_not_a_numeric_one(self):
        source = record(meaning="Bir başka rivayete göre, rüya sahibi zenginleşir.", language="tr")
        request = self.prepare([source])
        self.factory.ingest(request["batch_id"], response(request, ja="別の伝承によれば、夢を見た人は富を得る。"), "fixture")
        flags = self.factory.export_master()[0]["qa"]["flags"]
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch", flags)

    def test_turkish_real_one_remains_checked_with_another_tradition(self):
        source = record(meaning="Bir başka rivayete göre, iki çocuk sahibi olur.", language="tr")
        request = self.prepare([source])
        self.factory.ingest(request["batch_id"], response(request, ja="別の伝承では子どもを持つ。"), "fixture")
        flags = self.factory.export_master()[0]["qa"]["flags"]
        self.assertIn("TRANSLATION_ISSUE:numeric_mismatch", flags)

    def test_turkish_alone_and_one_of_relatives_are_not_false_numeric_counts(self):
        source = record(language="tr", meaning=(
            "Yalnız kalmaya ihtiyaç duyacağına, akrabalarınızdan birinin sizi kıskanacağına."))
        request = self.prepare([source])
        result = response(request, ja="一人になる必要を感じ、親族の一人があなたを妬む。")
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch",
                         deterministic_qa(materialized, source["source"]))

    def test_turkish_bir_kimsenin_is_indefinite_subject_not_a_count(self):
        source = record(language="tr", meaning="Bir kimsenin rüyada karaciğer görmesi yiğitlik ile tabir olunur.")
        request = self.prepare([source])
        result = response(request, ja="夢の中で肝臓を見ることは、勇敢さを表す。")
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch",
                         deterministic_qa(materialized, source["source"]))

    def test_turkish_birine_bakmak_matches_japanese_looking_at_a_person(self):
        source = record(language="tr", meaning="Rüyada camdan birine bakmak çok hayırlıdır.")
        request = self.prepare([source])
        result = response(request, ja="夢の中で窓越しに人を見るのは吉兆である。")
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch",
                         deterministic_qa(materialized, source["source"]))

    def test_turkish_bir_anda_doubled_income_matches_japanese_once(self):
        source = record(language="tr", meaning=(
            "Gelirinin bir anda ikiye katlanacağına, büyük adımlar atacağına."))
        request = self.prepare([source])
        result = response(request, ja="収入が一度に倍になり、大きな一歩を踏み出す。")
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch",
                         deterministic_qa(materialized, source["source"]))

    def test_english_roman_scripture_chapter_matches_japanese_citation(self):
        source = record(language="en", meaning="U. ``And Solomon awoke; and, behold, it was a dream.''-- First Kings, III., 15.")
        request = self.prepare([source])
        self.factory.ingest(request["batch_id"], response(request, ja="U。「ソロモンは目を覚ました。すると、それは夢であった。」――列王記上 第3章15節。"), "fixture")
        flags = self.factory.export_master()[0]["qa"]["flags"]
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch", flags)

    def test_common_japanese_acronym_ufo_is_valid_headword(self):
        source = record(language="en", term="ufo", meaning="A UFO appears in a dream.")
        request = self.prepare([source])
        self.factory.ingest(request["batch_id"], response(request, ja="夢にUFOが現れる。", term="UFO"), "fixture")
        flags = self.factory.export_master()[0]["qa"]["flags"]
        self.assertNotIn("TRANSLATION_ISSUE:headword_language", flags)

    def test_turkish_bir_nefes_matches_japanese_hittoiki(self):
        source = record(meaning="Rahat bir nefes alacağına.", language="tr")
        request = self.prepare([source])
        self.factory.ingest(request["batch_id"], response(request, ja="ほっと一息つける。"), "fixture")
        flags = self.factory.export_master()[0]["qa"]["flags"]
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch", flags)

    def test_turkish_bir_canlilik_is_not_counted_as_one_life(self):
        self.assertFalse(numeric_signature("ekonomik bir canlılık olduğunu görmek", "tr"))
        self.assertEqual(numeric_signature("bir can kurtarmak", "tr"), {"1": 1})

    def test_japanese_em_dash_list_labels_are_not_content_numbers(self):
        self.assertEqual(
            numeric_signature("三つの解釈がある。1―人々、2―秘密、3―貧困。", "ja"),
            {"3": 1})

    def test_turkish_indefinite_project_and_friend_are_not_numeric_counts(self):
        self.assertFalse(numeric_signature(
            "çok güzel bir projenin ardından sevilen bir arkadaşla bir başarının sonucu", "tr"))
        self.assertEqual(numeric_signature("tek bir projeyi tamamlamak", "tr"), {"1": 1})

    def test_japanese_ordinal_third_eye_preserves_source_number(self):
        self.assertEqual(numeric_signature("第三の目", "ja"), {"3": 1})

    def test_japanese_book_counter_preserves_source_number(self):
        self.assertEqual(numeric_signature("冒険小説二冊", "ja"), {"2": 1})

    def test_japanese_both_matters_preserves_source_number(self):
        self.assertEqual(numeric_signature("どちらの事柄についても", "ja"), {"2": 1})

    def test_japanese_both_hands_preserve_source_number(self):
        self.assertEqual(numeric_signature("両手が縛られている", "ja"), {"2": 1})

    def test_japanese_both_eyes_preserve_source_number(self):
        self.assertEqual(numeric_signature("両眼が白くなり、両目が見える", "ja"), {"2": 2})

    def test_japanese_both_legs_preserve_source_number(self):
        self.assertEqual(numeric_signature("両脚がない", "ja"), {"2": 1})

    def test_japanese_four_great_canonical_texts_preserve_number(self):
        self.assertEqual(numeric_signature("四大聖典とは別に", "ja"), {"4": 1})

    def test_turkish_second_job_translated_as_side_job(self):
        source = record(language="tr", meaning=(
            "Rüyada açma alıp yemek, bir yoksulu düşünme konusundaki vaadinizi yerine getirmeniz gerektiğine işarettir. "
            "Eve ekmek getirmek için ikinci bir işe gireceğinize delalet eder."))
        request = self.prepare([source])
        self.factory.ingest(request["batch_id"], response(
            request, ja="夢の中でアチュマを買って食べるのは、貧しい人を気にかける約束を果たすしるしである。家計を支えるために副業を始める。"), "fixture")
        flags = self.factory.export_master()[0]["qa"]["flags"]
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch", flags)

    def test_turkish_metaphorical_step_matches_japanese_ippou(self):
        source = record(language="tr", meaning="Kötü duruma sebep olacak bir adım atılacağına.")
        request = self.prepare([source])
        self.factory.ingest(request["batch_id"], response(request, ja="悪い事態を招く一歩を踏み出す。"), "fixture")
        flags = self.factory.export_master()[0]["qa"]["flags"]
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch", flags)

    def test_turkish_short_time_phrase_matches_japanese_short_period(self):
        self.assertFalse(numeric_signature("kısa bir zaman içinde", "tr"))
        self.assertFalse(numeric_signature("短期間で", "ja"))

    def test_turkish_first_period_matches_japanese_beginning(self):
        self.assertEqual(numeric_signature("ilk başlarda", "tr"), numeric_signature("初めは", "ja"))

    def test_turkish_indefinite_event_is_not_numeric_one(self):
        self.assertFalse(numeric_signature("güzel bir olayın ardından", "tr"))
        self.assertFalse(numeric_signature("bir olayın daha sonra sıkıntı yaratması", "tr"))
        self.assertEqual(numeric_signature("tek bir olay gördü", "tr"), {"1": 1})

    def test_english_standalone_number_heading_is_not_dream_content(self):
        self.assertFalse(numeric_signature("jealous people will injure you.\n14.\n\nDreams of lions", "en"))
        self.assertFalse(numeric_signature("jealous people will injure you. 14.\n\n Dreams of lions", "en"))
        self.assertFalse(numeric_signature("jealous people will injure you. 14. Dreams of lions", "en"))

    def test_japanese_second_child_preserves_source_number(self):
        self.assertEqual(numeric_signature("第二子を授かる", "ja"), {"2": 1})

    def test_japanese_numbered_heaven_layers_are_ordinals(self):
        self.assertEqual(numeric_signature("第一層、第二層、第三層", "ja"),
                         {"1": 1, "2": 1, "3": 1})

    def test_japanese_negative_never_again_is_not_a_numeric_count(self):
        self.assertEqual(numeric_signature("二度と立ち直れなくなる", "ja"), {})

    def test_japanese_footnote_number_is_not_a_dream_count(self):
        self.assertEqual(numeric_signature("注209。肉屋を参照。", "ja"), {})

    def test_footnote_number_does_not_trip_translation_numeric_qa(self):
        source = record(language="en", meaning="A reference note.[209]")
        request = self.prepare([source])
        result = response(request, ja="注209。肉屋を参照。")
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch",
                         deterministic_qa(materialized, source["source"]))

    def test_turkish_religion_and_world_pair_is_not_a_numeric_count(self):
        self.assertEqual(numeric_signature("din ve dünya için", "tr"), {})

    def test_turkish_indefinite_marriage_is_not_one_marriage(self):
        self.assertEqual(numeric_signature("bir evliliğin gerçekleşmesi", "tr"), {})

    def test_japanese_relative_days_preserve_english_day_counts(self):
        self.assertEqual(numeric_signature("今日の仕事を明日に回す", "ja"), {"1": 2})

    def test_turkish_duplicated_source_word_is_not_reported_as_translation_issue(self):
        source = record(term="birini birini dövmek",
                        meaning="Rüyada birini birini dövmek.", language="tr")
        request = self.prepare([source])
        result = response(request, ja="夢の中で誰かを殴る。")
        result["results"][0]["senses"][0]["flags"] = [
            "TRANSLATION_ISSUE: 重複した「birini birini」は文脈上「誰かを」として扱った。"]
        self.factory.ingest(request["batch_id"], result, "fixture")
        flags = self.factory.export_master()[0]["qa"]["flags"]
        self.assertIn("SOURCE_QUALITY_ISSUE:duplicated_source_word", flags)
        self.assertFalse(any(flag.startswith("TRANSLATION_ISSUE:") for flag in flags))

    def test_numeric_signature_respects_turkish_context_and_japanese_kanji(self):
        self.assertEqual(numeric_signature("Sure 078", "tr"), numeric_signature("第78章", "ja"))
        self.assertFalse(numeric_signature("1. If you dream of fruit. 1. Do you steal it. 1. If you eat it.", "en"))
        self.assertFalse(numeric_signature("1 If you dream, profit. 20. If you dream of leaves. 2 If you fall, gain.", "en"))
        self.assertFalse(numeric_signature("Glass is evil. 67. If maids dream of drawing drink, trouble follows.", "en"))
        self.assertFalse(numeric_signature("a week state of mind", "en"))
        self.assertEqual(set(numeric_signature(
            "Each of the five senses; perk up one of your five senses.", "en")), {"1", "5"})
        self.assertEqual(set(numeric_signature("9RAMAZAN: Kur'an bin aydan daha makbul.", "tr")), {"9", "1000"})
        turkish_currency = "beş dinardan ikisinin kaybolduğunu; yüz ya da bin dinar görmek"
        japanese_currency = "5ディナールのうち2枚がなくなり、100または1,000ディナールを見る"
        self.assertEqual(set(numeric_signature(turkish_currency, "tr")),
                         set(numeric_signature(japanese_currency, "ja")))
        self.assertEqual(set(numeric_signature(
            "onun iki ayağında veya bir ayağında olursa, onda bir hastalığın çıkması", "tr")), {"1", "2"})
        self.assertEqual(set(numeric_signature("両足または片足にある", "ja")), {"1", "2"})
        numbered_scrape = ("If you fall. 1 If you dream of a bicycle. 1 Maybe you lack confidence. "
                           "1 The dream indicates fear. 1 If you fall from a ladder. "
                           "1 If you do not know where you fell. 1 If someone else falls. "
                           "20. If you dream of falling leaves. 2 If you fall without injury. "
                           "2 If you fall into water. 1Falling leaves show grief. 1Do you dream of falling?")
        self.assertFalse(numeric_signature(numbered_scrape, "en"))
        self.assertEqual(set(numeric_signature("There is 1 apple.", "en")), {"1"})
        self.assertEqual(numeric_signature("twins or triplets", "en"), numeric_signature("双子または三つ子", "ja"))
        self.assertEqual(numeric_signature("timsahlardan biri", "tr"), numeric_signature("ワニの一匹", "ja"))
        self.assertFalse(numeric_signature("terbiye eden birine delâlet eder", "tr"))
        self.assertEqual(numeric_signature("yirmi beş tabir", "tr"),
                         numeric_signature("二十五の解釈がある。1.騒乱、2.武器、3.腐敗。", "ja"))
        self.assertEqual(numeric_signature("beş sayısı", "tr"), numeric_signature("五の数", "ja"))
        self.assertEqual(set(numeric_signature("On 20 May, 3 people arrived.", "en")), {"20", "3"})
        self.assertFalse(numeric_signature("No loss. 64.\n\nHOG’S-BRISTLES. This dream means danger.", "en"))
        self.assertFalse(numeric_signature("No loss. 64. HOG’S-BRISTLES. This dream means danger.", "en"))
        self.assertEqual(set(numeric_signature("64 hogs were seen.", "en")), {"64"})
        self.assertEqual(numeric_signature("10 steps ahead", "en"), numeric_signature("十手先", "ja"))
        self.assertEqual(numeric_signature("3 steps forward", "en"), numeric_signature("三歩進む", "ja"))
        self.assertEqual(numeric_signature("the number eleven", "en"), numeric_signature("数字の十一", "ja"))
        self.assertEqual(numeric_signature("dokuz türlü tâbir", "tr"), numeric_signature("九種類の意味", "ja"))
        self.assertFalse(numeric_signature("bir türlü anlamıyor", "tr"))
        self.assertEqual(numeric_signature("bir yahut iki ya da üç defa", "tr"),
                         numeric_signature("一度、二度、三度", "ja"))
        self.assertEqual(numeric_signature("bir veya iki ya da üç damla", "tr"),
                         numeric_signature("一滴、二滴、三滴", "ja"))
        self.assertEqual(numeric_signature("iki kapı ve iki halka", "tr"),
                         numeric_signature("扉が二つあり、環が二つある", "ja"))
        self.assertFalse(numeric_signature("yeni fikirlerle altına çöken küçük işler", "tr"))
        self.assertEqual(numeric_signature("altı parmak", "tr"), numeric_signature("六本指", "ja"))
        self.assertFalse(numeric_signature("altı yırtık ayakkabı ve koltuk altı kokusu", "tr"))
        self.assertEqual(numeric_signature("on iki şekilde", "tr"), numeric_signature("十二通り", "ja"))
        self.assertFalse(numeric_signature("yaşama dört elle sarılacağına", "tr"))
        self.assertFalse(numeric_signature("Yardım bekleyenler dört gözle bekliyor.", "tr"))
        self.assertEqual(set(numeric_signature("四方から支える", "ja")), {"4"})
        self.assertEqual(set(numeric_signature("双方が合意する", "ja")), {"2"})
        self.assertFalse(numeric_signature("üç beş kuruşunu boşa harcamak", "tr"))
        self.assertEqual(numeric_signature("iki farklı anlam. İlk olarak sonuç.", "tr"),
                         numeric_signature("二つの意味。まず一つ目の結果。", "ja"))
        self.assertEqual(numeric_signature("Olumlu rüyalardan biri, evlenecek bir çiftin.", "tr"),
                         numeric_signature("よい夢の一つ、結婚する二人。", "ja"))
        self.assertEqual(numeric_signature("aile efradından birinin başına", "tr"),
                         numeric_signature("家族の一人に起きる", "ja"))
        self.assertFalse(numeric_signature("経験も十分でない", "ja"))
        self.assertFalse(numeric_signature("成果を十分以上に得る", "ja"))
        self.assertEqual(set(numeric_signature("10分以上かかる", "ja")), {"10"})
        self.assertFalse(numeric_signature("document.getElementById('ak_js_1')", "tr"))
        self.assertEqual(set(numeric_signature("See [203] coins", "en")), {"203"})

    def test_turkish_being_among_a_group_does_not_force_numeric_one(self):
        source = record(meaning="Kurtuluşa erenlerden olmaya işaret eder.", language="tr")
        request = self.prepare([source])
        self.factory.ingest(request["batch_id"], response(request, ja="救われる者の一人となるしるしである。"), "fixture")
        flags = self.factory.export_master()[0]["qa"]["flags"]
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch", flags)

    def test_turkish_generic_someone_does_not_consume_another_explicit_one(self):
        source = record(meaning="Rüyada ev halkından birinin öldüğünü ve birine dinar verdiğini görmek.",
                        language="tr")
        request = self.prepare([source])
        self.factory.ingest(request["batch_id"], response(
            request, ja="家族の一人が亡くなり、誰かにディナールを渡す夢。"), "fixture")
        flags = self.factory.export_master()[0]["qa"]["flags"]
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch", flags)
        self.assertEqual(numeric_signature("24 hours a day", "en"), numeric_signature("一日24時間", "ja"))
        self.assertEqual(numeric_signature("married within a year", "en"),
                         numeric_signature("一年以内に結婚する", "ja"))
        self.assertEqual(numeric_signature("the symbolic number five", "en"),
                         numeric_signature("象徴的な数字の5", "ja"))
        self.assertEqual(numeric_signature("an old prediction in rhyme: 47", "en"),
                         numeric_signature("古い韻文の予言", "ja"))
        self.assertEqual(numeric_signature("Job vii, 14", "en"),
                         numeric_signature("ヨブ記7章14節", "ja"))
        self.assertFalse(numeric_signature("Bir kimse herhangi bir husustan sorumludur.", "tr"))
        rose_source = "Rüyada gül görmek iki çeşittir. Biri ağacı üzerinde, diğeri toplanmış olarak. Kırk gün sonra."
        rose_ja = "バラには二種類ある。一つは木についたもの、もう一つは摘まれたもの。四十日後。"
        self.assertEqual(numeric_signature(rose_source, "tr"), numeric_signature(rose_ja, "ja"))
        self.assertEqual(numeric_signature("her şeyin etiketi iki katına", "tr"), numeric_signature("2倍", "ja"))
        self.assertEqual(numeric_signature("iki kat artacağına", "tr"), numeric_signature("倍増する", "ja"))
        self.assertEqual(set(numeric_signature("2倍増する", "ja")), {"2"})

    def test_turkish_partitive_one_matches_japanese_choice_expression(self):
        source = record(meaning="Ebeveynlerinden biri ile ilgili bir sağlık sorunu.", language="tr")
        request = self.prepare([source])
        result = response(request, ja="両親のどちらかに健康上の問題がある。")
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch",
                         deterministic_qa(materialized, source["source"]))
        source = record(meaning="Dost ya da akrabalarından biri ile hayırlı bir işe gireceğine.", language="tr")["source"]
        materialized = dict(status="translated", senses=[dict(ja_term="友人または親族の誰か", flags=[], ambiguous=False,
            translation_confidence=.95, semantic_confidence=.95,
            units=[dict(ja="友人または親族の誰かと幸運な仕事を始める。",
                        source_quote=source["meaning"], kind="interpretation")])])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch",
                         deterministic_qa(materialized, source))

    def test_turkish_bir_dedigini_iki_etmemek_idiom_matches_japanese_paraphrase(self):
        source = record(meaning="Bir dediğini iki etmeyeceğine.", language="tr")
        request = self.prepare([source])
        result = response(request, ja="望みを一つも断らないことを示す。")
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch",
                         deterministic_qa(materialized, source["source"]))
        result = response(request, ja="配偶者の望みを何でもかなえる。")
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch",
                         deterministic_qa(materialized, source["source"]))

    def test_numeric_qa_includes_translated_headword(self):
        source = record(term="annemi iki tane", meaning="Rüyada annemi iki tane görmek.", language="tr")
        request = self.prepare([source])
        result = response(request, ja="その夢を見る。")
        result["results"][0]["senses"][0]["ja_term"] = "母親が二人見える"
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch",
                         deterministic_qa(materialized, source["source"]))
        self.assertEqual(numeric_signature("dört veçhe", "tr"), numeric_signature("四つの意味", "ja"))
        self.assertEqual(numeric_signature("iki vech ile tâbir olunur", "tr"), numeric_signature("二通りに解釈される", "ja"))
        self.assertEqual(numeric_signature("iki âlim erkeğe sahip olur", "tr"), numeric_signature("二人の学者を得る", "ja"))
        self.assertEqual(numeric_signature("dört elle sarılmak", "tr"), numeric_signature("懸命に取り組む", "ja"))
        self.assertEqual(numeric_signature("bir sene ve bir sene", "tr"),
                         numeric_signature("1年間、1年", "ja"))
        self.assertFalse(numeric_signature("haneden bir ayrılığın gerçekleşeceğine", "tr"))
        self.assertEqual(numeric_signature("bir ay boyunca", "tr"), numeric_signature("1か月", "ja"))
        self.assertEqual(numeric_signature("bir kere yaptığı hatayı bir daha yapmayacağına", "tr"),
                         numeric_signature("一度犯した過ちを二度と繰り返さない", "ja"))
        self.assertEqual(numeric_signature("hataları düşünerek bir kez daha yaşamamak için", "tr"),
                         numeric_signature("過ちを考え、二度と繰り返さないため", "ja"))
        self.assertEqual(numeric_signature("bir kere başarılı olursa", "tr"),
                         numeric_signature("いったん成功すれば", "ja"))
        self.assertEqual(numeric_signature("bir gün yerinden düşecek", "tr"),
                         numeric_signature("いつか落ちる", "ja"))
        self.assertEqual(numeric_signature("bir gün çok kızdı", "tr"),
                         numeric_signature("ある日ひどく怒った", "ja"))
        self.assertEqual(numeric_signature("üçüncü kişi", "tr"), numeric_signature("第三者", "ja"))
        self.assertEqual(numeric_signature("bir cana kıydım", "tr"), numeric_signature("一人を殺した", "ja"))
        self.assertEqual(numeric_signature("bir adım", "tr"), numeric_signature("一歩", "ja"))
        self.assertFalse(numeric_signature("bir avuç küpe görmek", "tr"))
        self.assertFalse(numeric_signature("elinde bulunan üç kuruşun bu kişi tarafından çalınacağı", "tr"))
        self.assertFalse(numeric_signature("samimiyetsiz ve iki yüzlü birine", "tr"))
        self.assertFalse(numeric_signature("bir deprem sonrası enkaz", "tr"))
        self.assertFalse(numeric_signature("he will look out for number two", "en"))
        self.assertFalse(numeric_signature("何一つ不足しない", "ja"))
        self.assertEqual(numeric_signature("ilk günkü gibi", "tr"),
                         numeric_signature("最初の日のように", "ja"))
        self.assertEqual(numeric_signature("birinci yarış", "tr"),
                         numeric_signature("競技の一位", "ja"))
        self.assertEqual(numeric_signature("bir koyduğu işten üç alacağına", "tr"),
                         numeric_signature("一つ投じて三つを得る", "ja"))
        self.assertEqual(set(numeric_signature("iki karga, biri diğerini öldürdü", "tr")),
                         set(numeric_signature("二羽のカラス、一羽がもう一羽を殺した", "ja")))
        self.assertEqual(numeric_signature("birbiri ardına yaşanan sorunlar", "tr"),
                         numeric_signature("次々と起こる問題", "ja"))
        self.assertEqual(numeric_signature("iki elbiseyi üst üste giymek dört şekilde tâbir olunur", "tr"),
                         numeric_signature("二枚の衣服を重ね、四通りに解釈される", "ja"))
        self.assertEqual(numeric_signature("iki parçaya ayrılmak ve iki memleket insanı", "tr"),
                         numeric_signature("二つに割れ、二つの国の人々", "ja"))
        self.assertEqual(numeric_signature("inançsız şehirlerinden bir şehrin fethedilmesi", "tr"),
                         numeric_signature("都市の一つが征服される", "ja"))
        self.assertEqual(numeric_signature("dörtte biri ve dörtte birden az", "tr"),
                         numeric_signature("四分の一と四分の一未満", "ja"))
        self.assertFalse(numeric_signature("十分に考えてから行動する", "ja"))
        self.assertEqual(set(numeric_signature("十分間待つ", "ja")), {"10"})
        self.assertEqual(numeric_signature("bir yıl sonra", "tr"), numeric_signature("1年後", "ja"))
        self.assertFalse(numeric_signature("bir yılanın izini görmek", "tr"))
        self.assertFalse(numeric_signature("bir günah işlemek", "tr"))
        self.assertFalse(numeric_signature("bir ayı öldürmek", "tr"))
        self.assertEqual(set(numeric_signature("bir ay boyunca çalışmak", "tr")), {"1"})
        self.assertEqual(set(numeric_signature("sahabilerinden birini görmek", "tr")), {"1"})
        self.assertEqual(set(numeric_signature("teorilerinden biri", "tr")), {"1"})
        self.assertEqual(numeric_signature("mübarek yerlerden birine gitmek", "tr"),
                         numeric_signature("聖地の一つへ行く", "ja"))
        self.assertFalse(numeric_signature("birini aramak", "tr"))
        self.assertEqual(set(numeric_signature("bir, iki veya yarım ekmek", "tr")), {"1", "2"})
        self.assertFalse(numeric_signature("bir ekmek ve bir insan", "tr"))
        self.assertFalse(numeric_signature("herkesin bir katkısının bulunduğu bir işe", "tr"))
        self.assertFalse(numeric_signature("_Sheriff_.[203] [203] See Bailiff and Police.", "en"))
        self.assertFalse(numeric_signature("[119] For a woman to dream of manslaughter. [119] See Murder.", "en"))
        self.assertFalse(numeric_signature("[119] 女性が夢で殺人罪を見る。[119] 「殺人」の項を参照。", "ja"))
        self.assertEqual(set(numeric_signature("The dream shows [203] coins.", "en")), {"203"})

    def test_numeric_qa_ignores_translated_labels_for_source_lettered_list(self):
        source = record(meaning="Dört şekilde tâbir olunur: a) Göz; b) Din; c) Mal; d) Yüz.",
                        language="tr")["source"]
        result = dict(status="translated", senses=[dict(ja_term="夢", flags=[], ambiguous=False,
            translation_confidence=.95, semantic_confidence=.95,
            units=[dict(ja="四通りに解釈される。①目。②信仰。③財産。④顔。",
                        source_quote=source["meaning"], kind="interpretation")])])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch", deterministic_qa(result, source))

    def test_numeric_and_religious_qa_aggregate_split_units_and_ignore_list_marker(self):
        source = record(meaning="1 If you find 3 coins, God rewards you.", language="en")
        request = self.prepare([source])
        result = response(request)
        result["results"][0]["senses"][0]["units"] = [
            dict(ja="3枚の硬貨を見つけたなら、", kind="interpretation"),
            dict(ja="神が報いることを示す。", kind="interpretation"),
        ]
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        flags = deterministic_qa(materialized, source["source"])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch", flags)

    def test_numeric_qa_treats_turkish_aloneness_as_japanese_one_person_phrase(self):
        source = record(meaning="Hamamda yalnız kaldığını görmek kedere işarettir.", language="tr")
        request = self.prepare([source])
        result = response(request)
        result["results"][0]["senses"][0]["units"][0]["ja"] = "浴場で一人でいる夢は悲しみを表す。"
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        flags = deterministic_qa(materialized, source["source"])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch", flags)

    def test_numeric_qa_treats_turkish_tekrar_as_non_numeric_again(self):
        source = record(meaning="Bu rüyayı tekrar görmek hayra işarettir.", language="tr")
        request = self.prepare([source])
        result = response(request)
        result["results"][0]["senses"][0]["units"][0]["ja"] = "この夢をもう一度見るのは吉兆である。"
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch",
                         deterministic_qa(materialized, source["source"]))

    def test_numeric_qa_does_not_count_turkish_bir_sirada(self):
        source = record(term="birinden kaçarak koşmak",
                        meaning="Rüyada birinden kaçarak koşmak fakat bir sırada büyük zorluklarla yüzleşmek hayra işarettir.",
                        language="tr")
        request = self.prepare([source])
        result = response(request)
        result["results"][0]["senses"][0]["units"][0]["ja"] = "夢の中で誰かから逃げて走り、ある時に大きな困難に直面するのは吉兆である。"
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch",
                         deterministic_qa(materialized, source["source"]))

    def test_numeric_qa_matches_turkish_both_ears_to_japanese_ryoji(self):
        source = record(term="KULAK ÇINLAMASI",
                        meaning="Eğer iki kulağı birden çınlarsa, insanlar arasında ondan söz edilir.",
                        language="tr")
        request = self.prepare([source])
        result = response(request)
        result["results"][0]["senses"][0]["units"][0]["ja"] = "両耳が同時に鳴ると、人々の間で話題になる。"
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch",
                         deterministic_qa(materialized, source["source"]))

    def test_numeric_qa_matches_turkish_four_angels_to_japanese_compound(self):
        source = record(term="dört büyük melek",
                        meaning="Rüyada dört büyük meleklerden biri olan Cebrail'i görmek hayra işarettir.",
                        language="tr")
        request = self.prepare([source])
        result = response(request)
        result["results"][0]["senses"][0]["units"][0]["ja"] = "四大天使の一人であるジブリールを見るのは吉兆を表す。"
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch",
                         deterministic_qa(materialized, source["source"]))

    def test_numeric_qa_matches_turkish_whip_strike_counts(self):
        source = record(term="dövmek",
                        meaning="Kendisine kırk değnek vurulsa içki içtiğine; seksen değnek vurulsa iftiraya işaret eder.",
                        language="tr")
        request = self.prepare([source])
        result = response(request)
        result["results"][0]["senses"][0]["units"][0]["ja"] = "40回むちで打たれるのは飲酒を、80回打たれるのは中傷を示す。"
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch",
                         deterministic_qa(materialized, source["source"]))

    def test_numeric_qa_matches_turkish_double_to_japanese_bai_ni_suru(self):
        source = record(meaning="Uzun vadede kazancı iki katına çıkaracak.", language="tr")
        request = self.prepare([source])
        result = response(request)
        result["results"][0]["senses"][0]["units"][0]["ja"] = "長期的には利益を倍にする。"
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch",
                         deterministic_qa(materialized, source["source"]))

    def test_numeric_qa_matches_turkish_bir_iki_to_japanese_one_or_two(self):
        source = record(meaning="Sevdiği bir iki kişi dışında kimsenin yardım etmeyeceğine.",
                        language="tr")
        request = self.prepare([source])
        result = response(request)
        result["results"][0]["senses"][0]["units"][0]["ja"] = "好きな一人か二人を除いて、誰も助けてくれない。"
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch",
                         deterministic_qa(materialized, source["source"]))

    def test_numeric_qa_ignores_turkish_bir_koseye_birakmak_idiom(self):
        source = record(meaning="Sıkıntılarını bir köşeye bırakarak daha doğru karar verir.", language="tr")
        request = self.prepare([source])
        result = response(request)
        result["results"][0]["senses"][0]["units"][0]["ja"] = "悩みを脇に置き、より正しい判断をする。"
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch",
                         deterministic_qa(materialized, source["source"]))

    def test_numeric_qa_matches_turkish_four_corners_to_japanese_square(self):
        source = record(meaning="Dört köşeli yastık görmek şerefe işarettir.", language="tr")
        request = self.prepare([source])
        result = response(request)
        result["results"][0]["senses"][0]["units"][0]["ja"] = "四角い枕を見る夢は名誉を表す。"
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch",
                         deterministic_qa(materialized, source["source"]))

    def test_numeric_qa_ignores_documented_citation_markers(self):
        source = record(meaning="A historical interpretation.")
        request = self.prepare([source])
        result = response(request, ja="[119] 女性が事件を恐れることを示す。 [119] 「事件」の項を参照。")
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch",
                         deterministic_qa(materialized, source["source"]))
        result = response(request, ja="10日後に起こる。")
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertIn("TRANSLATION_ISSUE:numeric_mismatch",
                      deterministic_qa(materialized, source["source"]))

    def test_turkish_biriyle_evlilik_is_not_a_literal_numeric_one(self):
        source = record(term="sardunya", meaning="Genç kızlar için çevreden biriyle evliliktir.", language="tr")
        request = self.prepare([source])
        result = response(request, ja="若い女性にとって周囲の人との結婚を意味する。")
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch",
                         deterministic_qa(materialized, source["source"]))

    def test_turkish_not_refusing_any_request_idiom_accepts_kikiireru_translation(self):
        source = record(language="tr", meaning="Eşinin bir dediğini iki etmeyeceğine.")
        request = self.prepare([source])
        result = response(request, ja="配偶者の言うことを何でも聞き入れることを示す。")
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch",
                         deterministic_qa(materialized, source["source"]))

    def test_english_five_senses_and_month_name_match_natural_japanese(self):
        source = record(language="en", meaning=(
            "Each of the five senses has much to offer. Perk up one of your five senses. "
            "To dream of the month of March signifies disappointment."))
        request = self.prepare([source])
        result = response(request, ja="五感にはそれぞれの働きがある。五感のうち一つを活性化する。3月の夢は失望を表す。")
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch",
                         deterministic_qa(materialized, source["source"]))

    def test_english_june_month_name_matches_japanese_numeric_month(self):
        source = record(language="en", meaning=(
            "The month of June brings success. The month of June in winter means love. "
            "The weather is unusual for the month of June."))
        request = self.prepare([source])
        result = response(request, ja="6月は成功をもたらす。冬の6月は恋愛を意味する。6月らしくない天候。")
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch",
                         deterministic_qa(materialized, source["source"]))

    def test_turkish_named_months_match_japanese_numeric_months(self):
        source = record(language="tr", meaning=(
            "Mayıs ayında, Haziran'da, Temmuz ayı içinde, Ağustos ayında ve Aralık ayında zelzele görmek."))
        request = self.prepare([source])
        result = response(request, ja="5月、6月、7月、8月、12月に地震を見る。")
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch",
                         deterministic_qa(materialized, source["source"]))

    def test_turkish_aralik_meaning_interval_is_not_december(self):
        source = record(language="tr", meaning="Aralık olan kapılar kadınlara işarettir.")
        request = self.prepare([source])
        result = response(request, ja="半開きの扉は女性を示す。")
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch",
                         deterministic_qa(materialized, source["source"]))

    def test_turkish_first_time_and_partitive_are_not_numeric_mismatches(self):
        source = record(language="tr", meaning=(
            "Uzun zaman sonra ilk kez kötü haber alacağına; bir kısmının kurtulduğunu."))
        request = self.prepare([source])
        result = response(request, ja="長い時を経て初めて悪い知らせを受ける。一部が助かった。")
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch",
                         deterministic_qa(materialized, source["source"]))

    def test_turkish_numbered_lists_match_japanese_comma_labels(self):
        source = record(language="tr", meaning=(
            "Rüyada dirhem görmenin on iki şekilde yorumlanır: 1-Doğru söz "
            "2-Hacet gidermek 3-Velayet 4-Biriktirilmiş mal 5-Dost 6-Evlat "
            "7-Arkadaş 8-Bol nimet 9-Güvende olmak 10-Cariye 11-Makam "
            "12-Derece. Kırık dirhem görmenin dört tabiri vardır: "
            "1-Çirkin söz 2-Savaş 3-Zindan 4-Tartışma. Elinde bir dirhem "
            "olduğunu görmek çocuk sahibi olmaya işarettir. Eşinizle dirhem "
            "paylaştığınızı görmek, eşinizle aranızda sonuçsuz bir konuşmaya delalet eder."))
        request = self.prepare([source])
        result = response(request, ja=(
            "ディルハムには12通りの解釈がある。1、正しい言葉。2、願い。3、統治権。"
            "4、財産。5、友人。6、子ども。7、仲間。8、恵み。9、安全。"
            "10、女奴隷。11、地位。12、位階。割れたディルハムには4通りある。"
            "1、醜い言葉。2、戦争。3、牢獄。4、口論。手のひらにディルハムが"
            "1枚あるのを見ると、子どもを授かるしるしである。配偶者と"
            "ディルハムを分け合うと、二人の間で結論の出ない話し合いがある。"))
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch",
                         deterministic_qa(materialized, source["source"]))

    def test_turkish_numbered_list_detection_preserves_verse_citation(self):
        meaning = ("Üç yorum: 1-İşlerin kötüye gitmesi 2-Üzüntü 3-Maddi zarar. "
                   "(Sure Tur, ayet 21) mealindeki ayet.")
        self.assertEqual(numeric_signature(meaning, "tr").get("21"), 1)

    def test_turkish_one_of_the_parties_is_japanese_one_side(self):
        source = record(language="tr", meaning=(
            "Yapılmak üzere olan bir anlaşmanın taraflardan birinin itiraz etmesiyle "
            "askıya alınacağına, önemli bir iş için sonucunu üç vakitte alacağına."))
        request = self.prepare([source])
        result = response(request, ja=(
            "予定されていた合意が当事者の一方の異議によって延期されること、"
            "重要な仕事の結果を三つの期間内に得ること。"))
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch",
                         deterministic_qa(materialized, source["source"]))

    def test_turkish_two_or_one_foot_translation_matches_japanese_counts(self):
        source = record(language="tr", meaning=(
            "Eğer ayrılan eklemler, onun iki ayağında veya bir ayağında olursa. "
            "Ancak rüya sahibinin bir hayvanı varsa, onda bir hastalığın çıkmasıyla baytara muhtaç olur."))
        request = self.prepare([source])
        result = response(request, ja="外れた関節が両足または片足にある場合。家畜が病気になるおそれがある。")
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch",
                         deterministic_qa(materialized, source["source"]))

    def test_turkish_nearby_person_partitive_matches_natural_japanese(self):
        source = record(language="tr", meaning="Yakın çevrenizden biri hakkında kötü haber alabileceğinize.")
        request = self.prepare([source])
        result = response(request, ja="身近な人について悪い知らせを受ける可能性を示す。")
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch",
                         deterministic_qa(materialized, source["source"]))

    def test_turkish_first_day_matches_japanese_hatsuka_expression(self):
        source = record(language="tr", meaning=(
            "Aile saadetinin hep ilk günkü gibi taze kalacağına."))
        request = self.prepare([source])
        result = response(request, ja="家族の幸福がいつまでも初日のように新鮮であることを示す。")
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch",
                         deterministic_qa(materialized, source["source"]))

    def test_turkish_dort_bir_koldan_matches_japanese_shiho(self):
        source = record(language="tr", meaning="Dört bir koldan saracak kimseler çoğalır.")
        request = self.prepare([source])
        result = response(request, ja="四方から支えてくれる人々が増える。")
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch",
                         deterministic_qa(materialized, source["source"]))

    def test_turkish_dort_bir_koldan_matches_japanese_all_directions_idiom(self):
        source = record(language="tr", meaning="Dört bir koldan saracak kimseler çoğalır.")
        request = self.prepare([source])
        result = response(request, ja="あらゆる方面から支えてくれる人々が増える。")
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch",
                         deterministic_qa(materialized, source["source"]))

    def test_turkish_iki_tarafinda_and_unquantified_step_match_natural_japanese(self):
        source = record(language="tr", meaning=(
            "Atılacak adım sayesinde iki tarafında çok mutlu olacağına."))
        request = self.prepare([source])
        result = response(request, ja="一歩踏み出すことで双方がとても幸せになる。")
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch",
                         deterministic_qa(materialized, source["source"]))

    def test_turkish_iki_tarafini_matches_japanese_ryogawa(self):
        source = record(language="tr", meaning=(
            "Caddenin iki tarafını yeşil ağaçlarla görmek yardım alacağını gösterir."))
        request = self.prepare([source])
        result = response(request, ja="大通りの両側に緑の木々が見える夢は、助けを受けることを示す。")
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch",
                         deterministic_qa(materialized, source["source"]))

    def test_turkish_iki_fikirlilik_matches_japanese_futagokoro(self):
        source = record(language="tr", meaning="İki fikirlilik edip güven kaybına yol açar.")
        request = self.prepare([source])
        result = response(request, ja="二心を抱くことで信頼を失う。")
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch",
                         deterministic_qa(materialized, source["source"]))

    def test_turkish_her_iki_cihanda_matches_japanese_ryoho(self):
        source = record(language="tr", meaning=(
            "Her iki cihanda da cenneti bulmak için mücadele edeceğine."))
        request = self.prepare([source])
        result = response(request, ja="現世と来世の両方で楽園を見いだすために努力する。")
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch",
                         deterministic_qa(materialized, source["source"]))

    def test_turkish_halktan_biri_matches_japanese_ippan_no_hito(self):
        source = record(language="tr", meaning="Halktan biri ise kazancı artar.")
        request = self.prepare([source])
        result = response(request, ja="一般の人なら収入が増える。")
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch",
                         deterministic_qa(materialized, source["source"]))

    def test_turkish_ilk_zamanlarda_matches_japanese_hajime_no_uchi(self):
        source = record(language="tr", meaning="İlk zamanlarda parasal sıkıntılar yaşanacağına.")
        request = self.prepare([source])
        result = response(request, ja="初めのうちは金銭的な困難に見舞われる。")
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch",
                         deterministic_qa(materialized, source["source"]))

    def test_turkish_partitive_birinde_matches_japanese_izureka(self):
        source = record(language="tr", meaning=(
            "Devlet kurumlarından birinde işe başlanacağına."))
        request = self.prepare([source])
        result = response(request, ja="国の機関のいずれかで働き始める。")
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch",
                         deterministic_qa(materialized, source["source"]))

    def test_turkish_ogreten_birine_is_a_generic_recipient_not_a_partitive(self):
        source = record(language="tr", meaning=(
            "İnsanlara sanat ve maharet öğreten birine delalet eder."))
        request = self.prepare([source])
        result = response(request, ja="人々に技術や熟練の技を教える人を表す。")
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch",
                         deterministic_qa(materialized, source["source"]))

    def test_turkish_ikiye_katlamak_matches_japanese_baizo(self):
        source = record(language="tr", meaning=(
            "Zor geçen bir yılın ardından gelirini ikiye katlayarak ailesine hayat sunar."))
        request = self.prepare([source])
        result = response(request, ja="苦しい一年の後、収入を倍増させて家族に生活を提供する。")
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch",
                         deterministic_qa(materialized, source["source"]))

    def test_turkish_partitive_one_matches_juubun_chuui(self):
        source = record(language="tr", meaning=(
            "Tanıdığı kişilerden birine yardım edeceğine ve dikkatli olacağına."))
        request = self.prepare([source])
        result = response(request, ja="知人の一人を助け、十分注意する。")
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch",
                         deterministic_qa(materialized, source["source"]))

    def test_turkish_second_time_not_repeating_matches_ja_nidoto(self):
        source = record(language="tr", meaning="Aynı hatayı ikinci kez tekrarlamayacağına.")
        request = self.prepare([source])
        result = response(request, ja="同じ過ちを二度と繰り返さないことを示す。")
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch",
                         deterministic_qa(materialized, source["source"]))

    def test_turkish_ilk_once_as_japanese_mazu_is_not_numeric_mismatch(self):
        source = record(language="tr", meaning=(
            "İş hayatına atılmak yerine ilk önce açığını kapatma yoluna gideceğine."))
        request = self.prepare([source])
        result = response(request, ja="仕事に就くのではなく、まず不足を補う道を選ぶことを示す。")
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch",
                         deterministic_qa(materialized, source["source"]))

    def test_turkish_ilk_kez_matches_japanese_hisashiburi(self):
        source = record(language="tr", meaning="Uzun zaman sonra ilk kez kötü haber alacağına.")
        request = self.prepare([source])
        result = response(request, ja="久しぶりに悪い知らせを受ける。")
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch",
                         deterministic_qa(materialized, source["source"]))

    def test_turkish_ilk_kez_matches_japanese_hajimete(self):
        source = record(language="tr", meaning="Uzun zaman sonra ilk kez kötü haber alacağına.")
        request = self.prepare([source])
        result = response(request, ja="長い時を経て初めて悪い知らせを受ける。")
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch",
                         deterministic_qa(materialized, source["source"]))

    def test_turkish_baska_onemli_bir_husus_is_not_a_numeric_one(self):
        source = record(language="tr", meaning=(
            "Teklif edilen iki husustan birini seçmek gerekir. Başka önemli bir husus ise dikkat etmektir."))
        request = self.prepare([source])
        result = response(request, ja="提示された二つの事柄から一つを選ぶ。もう一つ大切な点は、注意が必要なことです。")
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch",
                         deterministic_qa(materialized, source["source"]))

    def test_turkish_another_small_door_preserves_the_count(self):
        source = record(language="tr", meaning="Kapının ortasında küçük bir kapının daha bulunduğunu.")
        request = self.prepare([source])
        result = response(request, ja="扉の真ん中にもう一つ小さな扉がある。")
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch",
                         deterministic_qa(materialized, source["source"]))

    def test_turkish_another_balcony_and_second_marriage_preserve_both_counts(self):
        source = record(language="tr", meaning=(
            "Bir kimsenin rüyada bir balkon daha yaptığını görmek ikinci bir evliliğe işarettir."))
        request = self.prepare([source])
        result = response(request, ja="もう一つバルコニーを作るのを見るのは、二度目の結婚のしるしである。")
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch",
                         deterministic_qa(materialized, source["source"]))

    def test_turkish_bir_adim_atmak_matches_japanese_action(self):
        source = record(language="tr", meaning=(
            "Bir takım sorunlarla karşılaşacağına, anne babanın sevineceği bir adım atılacağına."))
        request = self.prepare([source])
        result = response(request, ja="いくつかの問題に直面すること、両親が喜ぶような行動を取ることを表す。")
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch",
                         deterministic_qa(materialized, source["source"]))

    def test_turkish_ilk_basta_matches_japanese_hajime(self):
        sources = [record(language="tr", meaning=f"{phrase} nazik davranacağına.")
                   for phrase in ("İlk başta", "İlk etapta")]
        request = self.prepare(sources)
        result = response(request, ja="当初は親切に振る舞う。")
        for source, translated, item in zip(sources, result["results"], request["items"]):
            materialized = materialize_transport_result(translated, item)
            self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch",
                             deterministic_qa(materialized, source["source"]))

    def test_turkish_cheek_sides_match_japanese_one_side_and_both_cheeks(self):
        source = record(language="tr", meaning=(
            "Yüzün yan kısımlarından birini görmek ve rüyada görülen iki yanak işarettir."))
        request = self.prepare([source])
        result = response(request, ja="顔の側面の一方と、夢で見た両頬を表す。")
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch",
                         deterministic_qa(materialized, source["source"]))

    def test_turkish_problem_count_idiom_one_to_five_preserves_both_numbers(self):
        source = record(language="tr", meaning="Sorunlarının birken beş olacağına.")
        request = self.prepare([source])
        result = response(request, ja="問題が一つから五つに増える。")
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch",
                         deterministic_qa(materialized, source["source"]))

    def test_turkish_bir_sebeple_and_bir_dedigini_iki_et_idioms(self):
        source = record(language="tr", meaning=(
            "Bir sebeple uzun süre toparlanamayacağına, bir dediğini iki etmeyeceğine."))
        request = self.prepare([source])
        result = response(request, ja="ある理由から長い間立ち直れず、頼みを断らない。")
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch",
                         deterministic_qa(materialized, source["source"]))

    def test_turkish_indefinite_bir_insanin_is_not_a_numeric_count(self):
        source = record(language="tr", meaning="Bir insanın boğulduğunu görmek.")
        request = self.prepare([source])
        result = response(request, ja="人が溺れるのを見る。")
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch",
                         deterministic_qa(materialized, source["source"]))

    def test_turkish_indefinite_birini_matches_generic_japanese_person(self):
        source = record(language="tr", meaning="Balkondan birini atmak.")
        request = self.prepare([source])
        result = response(request, ja="バルコニーから人を突き落とす。")
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch",
                         deterministic_qa(materialized, source["source"]))

    def test_turkish_iki_yuzlu_matches_japanese_futagokoro_idiom(self):
        source = record(language="tr", meaning="İkiyüzlü bir kişiyle karşılaşacağına.")
        request = self.prepare([source])
        result = response(request, ja="二心のある人に出会う。")
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch",
                         deterministic_qa(materialized, source["source"]))

    def test_turkish_dort_elle_matches_japanese_kokoro_kara(self):
        source = record(language="tr", meaning="Ona dört elle sarılacağına.")
        request = self.prepare([source])
        result = response(request, ja="その人に心から寄り添う。")
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch",
                         deterministic_qa(materialized, source["source"]))

    def test_turkish_birden_matches_japanese_totsuzen(self):
        source = record(language="tr", meaning="Birden yaşadığı değişikliğe şaşıracağına.")
        request = self.prepare([source])
        result = response(request, ja="突然の変化に驚く。")
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch",
                         deterministic_qa(materialized, source["source"]))

    def test_turkish_bir_bir_and_iki_kat_keep_both_numeric_meanings(self):
        source = record(language="tr", meaning=(
            "Bütün dileklerin bir bir gerçeğe dönüşeceğine, kazancının iki kat artacağına."))
        request = self.prepare([source])
        result = response(request, ja="願いが一つずつ実現し、収入が二倍になる。")
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch",
                         deterministic_qa(materialized, source["source"]))

    def test_turkish_birisiyle_matches_japanese_counterpart(self):
        source = record(language="tr", meaning="Rüyada birisiyle barıştığını görmek.")
        request = self.prepare([source])
        result = response(request, ja="仲たがいしていた相手と和解する。")
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch",
                         deterministic_qa(materialized, source["source"]))

    def test_turkish_bir_adim_atmak_matches_japanese_chakushu(self):
        source = record(language="tr", meaning="Kazançlı bir işle ilgili olarak bir adım atacağına.")
        request = self.prepare([source])
        result = response(request, ja="利益のある仕事に着手する。")
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch",
                         deterministic_qa(materialized, source["source"]))

    def test_turkish_aranizdaki_matches_japanese_between_you_idiom(self):
        source = record(language="tr", meaning="Aranızdaki anlaşmazlıkların artacağına.")
        request = self.prepare([source])
        result = response(request, ja="二人の間の不一致が増す。")
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch",
                         deterministic_qa(materialized, source["source"]))

    def test_turkish_pair_of_boots_matches_japanese_one_pair_unit(self):
        source = record(language="tr", term="bir çift bot", meaning="Bir çift bot görmek.")
        request = self.prepare([source])
        result = response(request, ja="一足のブーツを見る。", term="ブーツ")
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch",
                         deterministic_qa(materialized, source["source"]))

    def test_english_twin_flame_is_not_numeric_two(self):
        source = record(language="en", meaning="You may meet your twin flame.")
        request = self.prepare([source])
        result = response(request, ja="ツインフレームと出会うかもしれない。")
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch",
                         deterministic_qa(materialized, source["source"]))

    def test_repeated_twin_flame_phrase_is_not_counted_as_numeric_two(self):
        source = record(language="en", term="Twin Flame", meaning=(
            "Your twin flame appears. Twin Flame dreams reveal your possible future mate."))
        request = self.prepare([source])
        result = response(request, ja="ツインフレームが現れる。ツインフレームの夢は将来の伴侶を示す。")
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch",
                         deterministic_qa(materialized, source["source"]))

    def test_english_indefinite_stage_matches_japanese_counter_word(self):
        source = record(language="en", meaning=(
            "You will successfully complete a stage in your life."))
        request = self.prepare([source])
        result = response(request, ja="人生の一つの段階を無事に終える。")
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch",
                         deterministic_qa(materialized, source["source"]))

    def test_english_two_or_more_matches_numeric_japanese(self):
        source = record(language="en", meaning="If you see two or more aces.")
        request = self.prepare([source])
        result = response(request, ja="エースを2枚以上見る。")
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch",
                         deterministic_qa(materialized, source["source"]))

    def test_english_firstborn_matches_japanese_ordinal(self):
        source = record(language="en", meaning="The first born will be twins or triplets.")
        request = self.prepare([source])
        result = response(request, ja="第一子は双子か三つ子になる。")
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch",
                         deterministic_qa(materialized, source["source"]))

    def test_english_numbered_list_markers_allow_missing_space_after_period(self):
        meaning = "1 If snow falls. 1 If it melts.1 If it freezes."
        self.assertNotIn("1", numeric_signature(meaning, "en"))
        self.assertIn("1", numeric_signature("If 1 snowflake falls.", "en"))

    def test_turkish_fourteen_cattle_thresholds_match_explicit_japanese_counts(self):
        source = record(language="tr", meaning=(
            "Sığırın on dörtten fazla olması harbe; ondan az olması düşmanlığa işarettir."))
        request = self.prepare([source])
        result = response(request, ja="牛が14頭を超えると戦争、14頭未満なら敵意を示す。")
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch",
                         deterministic_qa(materialized, source["source"]))

    def test_turkish_giden_birinin_geri_donmek_is_indefinite_person(self):
        source = record(language="tr", meaning="Giden birinin geri döneceğine.")
        request = self.prepare([source])
        result = response(request, ja="去っていった人が戻ってくる。")
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch",
                         deterministic_qa(materialized, source["source"]))

    def test_turkish_bir_daha_negation_and_bir_adim_are_distinct(self):
        source = record(language="tr", meaning=(
            "Bir daha cesaret edip büyük adımlar atamayacağına, bir adımdan ötürü tartışacağına."))
        request = self.prepare([source])
        result = response(request, ja="二度と勇気を出して大きな一歩を踏み出せず、最後の一歩が原因で争う。")
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch",
                         deterministic_qa(materialized, source["source"]))

    def test_japanese_ittai_counts_as_two_for_turkish_pair(self):
        source = record(language="tr", meaning="Rüyada bir çift mavi göz görmek ve sevilen kişilerden birinin haberini almak.")
        request = self.prepare([source])
        result = response(request, ja="夢で一対の青い目を見る。愛する人の一人から知らせを受ける。")
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch",
                         deterministic_qa(materialized, source["source"]))

    def test_turkish_bir_yenisinin_matches_japanese_mou_hitotsu(self):
        source = record(language="tr", meaning="Sorunlarına bir yenisinin daha ekleneceğine.")
        request = self.prepare([source])
        result = response(request, ja="悩みにもう一つ問題が加わる。")
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch",
                         deterministic_qa(materialized, source["source"]))

    def test_turkish_ilk_firsatta_matches_japanese_soon(self):
        source = record(language="tr", meaning="İlk fırsatta hayatı yeniden düzene koyacağına.")
        request = self.prepare([source])
        result = response(request, ja="近いうちに暮らしを再び整える。")
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch",
                         deterministic_qa(materialized, source["source"]))

    def test_turkish_hem_hem_de_matches_japanese_ryoho(self):
        source = record(language="tr", meaning="Hem iç huzuruna hem de maddi huzura işaret eder.")
        request = self.prepare([source])
        result = response(request, ja="内面の平穏と物質的な安定の両方を表す。")
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch",
                         deterministic_qa(materialized, source["source"]))

    def test_turkish_and_japanese_numbered_interpretation_lists_are_not_content_numbers(self):
        source = record(language="tr", meaning=(
            "Rüyada kan aldırmanın dört tabiri vardır: 1-İşlerin açılması "
            "2-Yolculuk 3-Husumet 4-Ortaklık."))
        request = self.prepare([source])
        result = response(request, ja=(
            "夢の中で瀉血をすることには四つの解釈がある。"
            "1. 物事がうまく運ぶ。2. 旅。3. 敵意。4. 共同事業。"))
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch",
                         deterministic_qa(materialized, source["source"]))

    def test_turkish_partitive_exception_does_not_erase_another_number_one(self):
        source = record(language="tr", meaning=(
            "Tanıdığı kişilerden birine yardım edeceğine ve bir gün dinleneceğine."))
        request = self.prepare([source])
        result = response(request, ja="知人の誰かを助け、1日休む。")
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch",
                         deterministic_qa(materialized, source["source"]))

    def test_turkish_seeing_the_moon_is_not_numeric_one_month(self):
        self.assertNotIn("1", numeric_signature(
            "Bir kimsenin rüyasında bedir halinde bir ay görmesi.", "tr"))
        self.assertIn("1", numeric_signature("Bir ay sonra dönecek.", "tr"))
        self.assertNotIn("1", numeric_signature("Rüyada bir ayakkabı almak.", "tr"))
        self.assertIn("1", numeric_signature("Rüyada bir ayak görmek.", "tr"))

    def test_turkish_ordinal_and_reduplicative_leaf_phrase_match_japanese(self):
        self.assertIn("1", numeric_signature("yaşayacağı ilk maddi sıkıntıda", "tr"))
        self.assertIn("1", numeric_signature("最初の経済的困難", "ja"))
        self.assertIn("1", numeric_signature("Ağacın yaprak yaprak açıldığını görmek", "tr"))
        self.assertIn("1", numeric_signature("葉が一枚ずつ開く", "ja"))

    def test_turkish_biri_olmak_is_indefinite_not_numeric_one(self):
        source = record(language="tr", meaning=(
            "Kıskanılan ve her işi rast giden biri olacağına."))
        request = self.prepare([source])
        result = response(request, ja="羨ましがられ、何をしてもうまくいく人になる。")
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch",
                         deterministic_qa(materialized, source["source"]))

    def test_turkish_mutual_bond_can_match_two_people_in_japanese(self):
        source = record(language="tr", meaning="Onunla birlikte aralarındaki bağ kuvvetlenecek.")
        request = self.prepare([source])
        result = response(request, ja="その友人とともに、二人の絆が強まる。")
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch",
                         deterministic_qa(materialized, source["source"]))

    def test_turkish_their_bond_can_be_rendered_as_mutual_bond(self):
        source = record(language="tr", meaning="Aralarındaki bağın daha da kuvvetleneceğine.")
        request = self.prepare([source])
        result = response(request, ja="互いの絆がさらに強まる。")
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch",
                         deterministic_qa(materialized, source["source"]))

    def test_turkish_minute_and_both_pair_are_numeric_equivalents(self):
        source = record(language="tr", meaning=(
            "Bir dakikanın bile azap vereceği ve hem rütbe hem de kazanç elde edeceği."))
        request = self.prepare([source])
        result = response(request, ja="一分さえ苦痛となり、地位と収入の両方を得る。")
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch",
                         deterministic_qa(materialized, source["source"]))

    def test_turkish_indefinite_friend_does_not_require_japanese_number(self):
        source = record(language="tr", meaning=(
            "Teklif götürdüğü bir arkadaşının kabul etmesi sayesinde büyük başarılar kazanacağı."))
        request = self.prepare([source])
        result = response(request, ja="提案を持ちかけた友人が承諾したことで、大きな成功を収める。")
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch",
                         deterministic_qa(materialized, source["source"]))

    def test_turkish_two_animals_can_match_gender_pair_in_japanese(self):
        source = record(language="tr", meaning=(
            "Aynı cinsten erkek ve dişi iki hayvanın çiftleşerek ürediğini görmek."))
        request = self.prepare([source])
        result = response(request, ja="同じ種類の雄と雌の動物が交尾して繁殖する。")
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch",
                         deterministic_qa(materialized, source["source"]))

    def test_turkish_second_time_matches_japanese_once_again(self):
        source = record(language="tr", meaning=(
            "İkinci defa yine oraya gider. Şehre bir kez daha gider."))
        request = self.prepare([source])
        result = response(request, ja="もう一度そこへ行く。街へもう一度行く。")
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch",
                         deterministic_qa(materialized, source["source"]))

    def test_turkish_not_one_but_at_least_twofold_preserves_numeric_meaning(self):
        source = record(language="tr", meaning=(
            "Kazancın bir değil en az iki kat arttırılacağı."))
        request = self.prepare([source])
        result = response(request, ja="収入が一倍ではなく少なくとも二倍に増える。")
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch",
                         deterministic_qa(materialized, source["source"]))

    def test_turkish_one_by_one_preserves_numeric_meaning(self):
        source = record(language="tr", meaning=(
            "İki taraf sevinecek, işleri birer birer yoluna girecek."))
        request = self.prepare([source])
        result = response(request, ja="双方が喜び、仕事が一つずつ軌道に乗る。")
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch",
                         deterministic_qa(materialized, source["source"]))

    def test_turkish_indefinite_period_is_not_a_numeric_one(self):
        source = record(language="tr", meaning=(
            "Uzun bir süre boyunca kendisine gelemeyeceğine, hiç ummadığı bir anda çok daha kötü bir durumda kalacağına."))
        request = self.prepare([source])
        result = response(request, ja="長い間立ち直れず、まったく予期しない時にさらに悪い状況に置かれる。")
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch",
                         deterministic_qa(materialized, source["source"]))

    def test_turkish_indefinite_situation_is_not_one_situation(self):
        source = record(language="tr", meaning="Talihsiz bir durumun daha da büyüyeceğine.")
        request = self.prepare([source])
        result = response(request, ja="起こる不運な出来事がさらに深刻になる。")
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch",
                         deterministic_qa(materialized, source["source"]))

    def test_turkish_material_spiritual_pair_preserves_numeric_meaning(self):
        source = record(language="tr", meaning="Maddi ve manevi zararlarla karşılaşacak.")
        request = self.prepare([source])
        result = response(request, ja="物質面と精神面の両方で損害に直面する。")
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch",
                         deterministic_qa(materialized, source["source"]))

    def test_turkish_religious_worldly_pair_is_not_a_count_requirement(self):
        source = record(language="tr", meaning="Din ve dünya işlerinden müşkül olan bir iş çözülür.")
        request = self.prepare([source])
        result = response(request, ja="宗教上または世俗上の難事が解決する。")
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch",
                         deterministic_qa(materialized, source["source"]))

    def test_turkish_army_number_scale_matches_japanese_hundreds_and_thousands(self):
        meaning = ("Sayıları yüz olan asker, her yüz senede bir; rüyada yüz, üç ya da beş bin "
                   "veya yirmi bin asker görmek. Rüyada bin askeri görmek.")
        source = record(language="tr", term="ordu", meaning=meaning)
        request = self.prepare([source])
        result = response(request, ja=("兵士の数が百の軍隊、百年ごと、夢で百人、三千人、五千人、二万人を見ること。"
                                       "夢で千人の兵士を見ること。"))
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch",
                         deterministic_qa(materialized, source["source"]))

    def test_turkish_indefinite_someone_is_not_a_numeric_one(self):
        source = record(language="tr", meaning="Balkondan birinin atladığını görmek.")
        request = self.prepare([source])
        result = response(request, ja="ベランダから人が飛び降りるのを見る。")
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch",
                         deterministic_qa(materialized, source["source"]))

    def test_turkish_birine_matches_japanese_someone_headword(self):
        source = record(language="tr", term="birine çorap", meaning="Rüyada birine çorap görmek.")
        request = self.prepare([source])
        result = response(request, ja="誰かの靴下を見る夢。", term="誰かの靴下を見る")
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch",
                         deterministic_qa(materialized, source["source"]))

    def test_turkish_family_elder_or_parents_is_an_alternative_not_count(self):
        source = record(language="tr", meaning=(
            "Aile büyüklerinden birinin ya da anne babanın desteğiyle her şey düzelir."))
        request = self.prepare([source])
        result = response(request, ja="年長の家族か両親の支えですべてが好転する。")
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch",
                         deterministic_qa(materialized, source["source"]))

    def test_turkish_nur_topu_baby_idiom_is_not_one_baby_count(self):
        source = record(language="tr", meaning="Nur topu gibi bir bebeğin geleceğine.")
        request = self.prepare([source])
        result = response(request, ja="玉のようにかわいらしい赤ちゃんが生まれる。")
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch",
                         deterministic_qa(materialized, source["source"]))

    def test_turkish_indefinite_income_is_not_one_counted_income(self):
        source = record(language="tr", meaning="Daha iyi bir kazanç ve rahat bir gelecek sunacak.")
        request = self.prepare([source])
        result = response(request, ja="よりよい収入とゆとりある将来をもたらす。")
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:numeric_mismatch",
                         deterministic_qa(materialized, source["source"]))

    def test_suspicious_japanese_does_not_match_valid_compound_prefix(self):
        source = record()
        request = self.prepare([source])
        result = response(request, ja="夢では善と善行にすがることを示す。")
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:suspicious_japanese",
                         deterministic_qa(materialized, source["source"]))

    def test_noun_label_meaning_text_is_not_a_sentence_headword(self):
        source = record(language="tr", term="cennet yazısı", meaning="Rüyada cennet yazısı görmek hayra işarettir.")
        request = self.prepare([source])
        result = response(request, ja="夢で天国の文字を見るのは吉兆である。", term="天国という文字")
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertNotIn("TRANSLATION_ISSUE:suspicious_headword",
                         deterministic_qa(materialized, source["source"]))
        result = response(request, ja="問題が問題として残る。")
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertIn("TRANSLATION_ISSUE:suspicious_japanese",
                      deterministic_qa(materialized, source["source"]))

    def test_classical_chinese_gloss_with_nonidiomatic_shirus_is_routed_to_review(self):
        source = record(term="桶無水者主大凶", meaning="桶無水者主大凶")
        source["source"]["language"] = "zh-Hant"
        source["entry_id"] = digest(source["source"])
        request = self.prepare([source])
        result = response(request, ja="大凶を主る。")
        materialized = materialize_transport_result(result["results"][0], request["items"][0])
        self.assertIn("TRANSLATION_ISSUE:suspicious_japanese",
                      deterministic_qa(materialized, source["source"]))

    def test_low_confidence_routes_to_review_without_batch_quality_halt(self):
        request = self.prepare()
        result = response(request)
        result["results"][0]["senses"][0]["semantic_confidence"] = .63
        self.factory.ingest(request["batch_id"], result, "fixture")
        self.assertEqual(self.factory.statistics()["states"]["needs_review"], 1)
        quality = json.loads(self.factory.db.execute("SELECT detail FROM events WHERE kind='batch_quality'").fetchone()[0])
        self.assertEqual(quality["rates"]["suspicious_translation"], 0)
        self.assertIsNone(self.factory.meta("halt"))

    def test_explicit_source_fragment_uncertainty_is_not_a_translation_halt(self):
        request = self.prepare([record(language="tr")])
        result = materialize_transport_result(response(request)["results"][0], request["items"][0])
        result["senses"][0]["flags"] = [
            "TRANSLATION_ISSUE: source contains a garbled fragment near the end"]
        self.factory.check_batch_quality(request, {result["id"]: result})
        self.assertIsNone(self.factory.meta("halt"))

    def test_review_quarantine_does_not_trip_language_halt(self):
        request = self.prepare([record(language="tr")])
        item = request["items"][0]
        reviewed = copy.deepcopy(request)
        reviewed["pass"] = "review"
        item["pass"] = "review"
        result = materialize_transport_result(response(request, ja="Rüyada görmek.")["results"][0], item)
        result["senses"][0]["translation_confidence"] = .8
        self.factory.check_batch_quality(reviewed, {result["id"]: result})
        self.assertIsNone(self.factory.meta("halt"))

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
        phrases = ["The first interpretation " + "friend " * 60 + "ends here.",
                   "The second interpretation " + "friend " * 60 + "ends here.",
                   "The third interpretation " + "friend " * 60 + "ends here.",
                   "The fourth interpretation " + "friend " * 60 + "ends here.",
                   "The fifth interpretation " + "friend " * 60 + "ends here."]
        request = self.prepare([record(original_term, phrase) for phrase in phrases])
        result = response(request)
        for i, translated in enumerate(result["results"]):
            translated["senses"][0]["units"][0]["ja"] = [
                "第一の意味を示す。", "第二の意味を示す。", "第三の意味を示す。",
                "第四の意味を示す。", "第五の意味を示す。"][i]
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

    def test_qa_quarantines_review_after_attempt_limit_without_dropping_translation(self):
        self.finish()
        row = self.factory.db.execute("SELECT id,result FROM entries").fetchone()
        result = json.loads(row["result"])
        result["senses"][0]["translation_confidence"] = .8
        result["senses"][0]["semantic_confidence"] = .8
        row_result = dumps(result)
        with self.factory.db:
            self.factory.db.execute("UPDATE entries SET attempts=3,result=? WHERE id=?", (row_result, row["id"]))
        stats = self.factory.qa_all()
        self.assertEqual(stats, self.factory.qa_all())
        saved = self.factory.db.execute("SELECT status,result,flags FROM entries WHERE id=?", (row["id"],)).fetchone()
        self.assertEqual(stats["states"]["unresolved"], 1)
        self.assertEqual(saved["status"], "unresolved")
        self.assertEqual(saved["result"], row_result)
        self.assertIn("PIPELINE_ISSUE:review_attempt_limit", json.loads(saved["flags"]))

    def test_qa_preserves_human_audit_block_and_translation_evidence(self):
        self.finish()
        row = self.factory.db.execute("SELECT id,result FROM entries").fetchone()
        audit_flag = "TRANSLATION_ISSUE:human_audit:direction_reversal"
        with self.factory.db:
            self.factory.db.execute("UPDATE entries SET status='unresolved',flags=? WHERE id=?",
                                    (dumps([audit_flag]), row["id"]))
        first = self.factory.qa_all()
        saved = self.factory.db.execute("SELECT status,result,flags FROM entries WHERE id=?", (row["id"],)).fetchone()
        self.assertEqual(first["states"]["unresolved"], 1)
        self.assertEqual(saved["result"], row["result"])
        self.assertIn(audit_flag, json.loads(saved["flags"]))
        self.assertEqual(first, self.factory.qa_all())

    def test_qa_all_keeps_legacy_prompt_review_until_second_pass(self):
        self.finish()
        row = self.factory.db.execute("SELECT id,flags FROM entries").fetchone()
        flags = json.loads(row["flags"])
        flags.append("PIPELINE_ISSUE:legacy_prompt_review")
        with self.factory.db:
            self.factory.db.execute("UPDATE entries SET status='needs_review',flags=? WHERE id=?",
                                    (dumps(flags), row["id"]))

        first = self.factory.qa_all()
        self.assertEqual(first["states"]["needs_review"], 1)
        self.assertIn("PIPELINE_ISSUE:legacy_prompt_review",
                      json.loads(self.factory.db.execute("SELECT flags FROM entries").fetchone()[0]))

        with self.factory.db:
            self.factory.db.execute("UPDATE entries SET pass_count=2 WHERE id=?", (row["id"],))
        second = self.factory.qa_all()
        self.assertEqual(second["states"]["complete"], 1)
        self.assertNotIn("PIPELINE_ISSUE:legacy_prompt_review",
                         json.loads(self.factory.db.execute("SELECT flags FROM entries").fetchone()[0]))

    def test_qa_all_quarantines_only_corrupt_source_coverage(self):
        self.finish()
        row = self.factory.db.execute("SELECT id,result FROM entries").fetchone()
        result = json.loads(row["result"])
        result["senses"][0]["units"][0]["source_quote"] = "A faithful"
        with self.factory.db:
            self.factory.db.execute("UPDATE entries SET result=? WHERE id=?", (dumps(result), row["id"]))
        stats = self.factory.qa_all()
        self.assertEqual(stats["states"]["needs_review"], 1)
        self.assertEqual(self.factory.db.execute("SELECT count(*) FROM cache").fetchone()[0], 0)
        master = self.factory.export_master()[0]
        self.assertIn("PIPELINE_ISSUE:source_span_coverage", master["qa"]["flags"])

    def test_coverage_salvage_commits_only_individually_valid_results(self):
        request = self.prepare([record(), record("cat", "A separate companion.")])
        result = response(request)
        result["results"][0]["senses"][0]["units"] = []
        atomic_json(self.factory.root / "responses" / f"{request['batch_id']}.json", result)
        self.factory.meta("halt", {"reason": "SOURCE_COVERAGE missing span", "batch_id": request["batch_id"]})
        stats = self.factory.salvage_coverage_response(request["batch_id"], "fixture")
        self.assertEqual((stats["salvaged"], stats["requeued"]), (1, 1))
        self.assertEqual(stats["states"]["complete"], 1)
        self.assertEqual(stats["states"]["pending"], 1)
        self.assertEqual(self.factory.db.execute("SELECT state FROM batches").fetchone()[0], "failed")
        retry_response = response(request)
        atomic_json(self.factory.root / "responses" / f"{request['batch_id']}.json", retry_response)
        retried = self.factory.salvage_coverage_response(request["batch_id"], "fixture")
        self.assertEqual((retried["salvaged"], retried["requeued"]), (1, 0))
        self.assertEqual(retried["states"]["complete"], 2)

    def test_review_coverage_salvage_requeues_only_bad_item_to_review(self):
        request = self.prepare()
        first = response(request)
        first["results"][0]["senses"][0]["flags"] = ["SOURCE_QUALITY_ISSUE:source_noise"]
        self.factory.ingest(request["batch_id"], first, "fixture")
        review = self.factory.export_batch(pass_kind="review")
        broken = response(review)
        broken["results"][0]["senses"][0]["units"] = []
        atomic_json(self.factory.root / "responses" / f"{review['batch_id']}.json", broken)
        with self.assertRaisesRegex(PipelineError, "SOURCE_COVERAGE"):
            self.factory.ingest(review["batch_id"], broken, "fixture")
        stats = self.factory.salvage_coverage_response(review["batch_id"], "fixture")
        self.assertEqual((stats["salvaged"], stats["requeued"]), (0, 1))
        self.assertEqual(stats["states"]["needs_review"], 1)

    def test_empty_translation_salvage_preserves_valid_ids_and_requeues_only_invalid(self):
        request = self.prepare([record(), record("cat", "A separate companion.")])
        broken = response(request)
        broken["results"][1]["senses"][0]["units"][0]["ja"] = ""
        atomic_json(self.factory.root / "responses" / f"{request['batch_id']}.json", broken)
        with self.assertRaisesRegex(PipelineError, "EMPTY_TRANSLATION"):
            self.factory.ingest(request["batch_id"], broken, "fixture")
        stats = self.factory.salvage_response(request["batch_id"], "fixture")
        self.assertEqual((stats["salvaged"], stats["requeued"]), (1, 1))
        self.assertEqual(stats["states"]["complete"], 1)
        self.assertEqual(stats["states"]["pending"], 1)
        self.assertEqual(self.factory.meta("halt")["violations"]["empty_translation"], .5)
        work = self.factory.root
        self.factory.close()
        self.factory = Factory(work)
        self.assertEqual(self.factory.meta("halt")["violations"]["empty_translation"], .5)
        flags = self.factory.db.execute("SELECT flags FROM entries WHERE id=?", (record("cat", "A separate companion.")["entry_id"],)).fetchone()[0]
        self.assertIn("PIPELINE_ISSUE:invalid_entry_output", flags)
        self.factory.acknowledge_halt("Investigated empty unit; kept exact-ID valid output and requeued only the faulty entry.")
        self.assertIsNone(self.factory.meta("halt"))

    def test_empty_sense_list_is_not_misclassified_as_source_coverage(self):
        request = self.prepare()
        first = response(request)
        first["results"][0]["senses"][0]["flags"] = ["SOURCE_QUALITY_ISSUE:source_noise"]
        self.factory.ingest(request["batch_id"], first, "fixture")
        review = self.factory.export_batch(pass_kind="review")
        broken = response(review)
        broken["results"][0]["senses"] = []
        atomic_json(self.factory.root / "responses" / f"{review['batch_id']}.json", broken)
        with self.assertRaisesRegex(PipelineError, "EMPTY_TRANSLATION no senses"):
            self.factory.ingest(review["batch_id"], broken, "fixture")
        stats = self.factory.salvage_response(review["batch_id"], "fixture")
        self.assertEqual((stats["salvaged"], stats["requeued"]), (0, 1))
        self.assertEqual(stats["states"]["needs_review"], 1)
        self.assertEqual(self.factory.meta("halt")["batch_id"], review["batch_id"])
        self.assertEqual(self.factory.db.execute("SELECT state FROM batches WHERE id=?",
                         (review["batch_id"],)).fetchone()[0], "failed")

    def test_parallel_dispatch_stops_scheduling_after_quality_halt(self):
        request = {"batch_id": "fixture-batch", "items": [{}]}
        with patch.object(dictionary_codex_runner, "Factory") as factory_class, \
                patch.object(dictionary_codex_runner, "invoke", return_value={"states": {}}), \
                redirect_stdout(io.StringIO()):
            factory = factory_class.return_value
            factory.export_batch.return_value = request
            factory.meta.return_value = {"batch_id": "fixture-batch"}
            self.assertIsNone(dictionary_codex_runner.dispatch_one("work", "gpt-6-luna", 1, 100,
                                                                   "translate", None))
            factory.meta.return_value = None
            self.assertEqual(dictionary_codex_runner.dispatch_one("work", "gpt-6-luna", 1, 100,
                                                                  "translate", None), "fixture-batch")


if __name__ == "__main__":
    unittest.main()
