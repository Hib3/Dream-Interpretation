import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dictionary_pipeline import Factory, PROMPT_VERSION, TASK_SCHEMA_VERSION, source_records
from migrate_dictionary_checkpoint import migrate_checkpoint


def archive(path, changed=False):
    turkish_meaning = "Revised meaning." if changed else "Old meaning."
    turkish_url = "https://example.test/revised" if changed else "https://example.test/old"
    path.write_text(json.dumps({"entries": [
        {"term": "water", "term_normalized": "water", "language": "en", "aliases": [],
         "sources": [], "meanings": [{"text": "Unchanged meaning.", "source_name": "English",
           "source_url": "https://example.test/en", "license_note": "test"}]},
        {"term": "köpek", "term_normalized": "köpek", "language": "tr", "aliases": [],
         "sources": [], "meanings": [{"text": turkish_meaning, "source_name": "Turkish",
           "source_url": turkish_url, "license_note": "test"}]},
    ]}), encoding="utf-8")


class MigrateCheckpointTests(unittest.TestCase):
    def test_migration_clones_prior_and_requeues_only_changed_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original_archive = root / "original.json"
            revised_archive = root / "revised.json"
            old_work = root / "old-work"
            new_work = root / "new-work"
            archive(original_archive)
            archive(revised_archive, changed=True)

            old = Factory(old_work)
            old.prepare(source_records(original_archive), phase="phase2")
            water_id = old.db.execute(
                "SELECT id FROM entries WHERE json_extract(source,'$.term')='water'"
            ).fetchone()[0]
            old.db.execute(
                "UPDATE entries SET status='complete',result=?,task_key=? WHERE id=?",
                (json.dumps({"status": "translated", "senses": []}), "legacy-key", water_id),
            )
            request = {"prompt_version": PROMPT_VERSION, "task_schema_version": TASK_SCHEMA_VERSION,
                       "pass": "translate", "bindings": [{"entry_id": water_id}]}
            old.db.execute(
                "INSERT INTO batches(id,request,state) VALUES(?,?,?)",
                ("current-prompt-evidence", json.dumps(request), "complete"),
            )
            old.db.commit()
            old.close()

            result = migrate_checkpoint(revised_archive, old_work, new_work)

            self.assertEqual(result["source_entries"], 2)
            self.assertEqual(result["reused_source_entries"], 1)
            self.assertEqual(result["new_or_changed_entries"], 1)
            self.assertEqual(result["inactive_old_entries"], 1)
            self.assertEqual(result["rebased_cache_keys"], 1)
            self.assertEqual(result["states"]["complete"], 1)
            self.assertEqual(result["states"]["pending"], 1)
            self.assertEqual(sum(result["states"].values()), 2)
            with closing(sqlite3.connect(old_work / "checkpoint.sqlite")) as original:
                self.assertEqual(original.execute(
                    "SELECT COUNT(*) FROM entries WHERE active=1"
                ).fetchone()[0], 2)
            with closing(sqlite3.connect(new_work / "checkpoint.sqlite")) as migrated:
                self.assertEqual(migrated.execute(
                    "SELECT COUNT(*) FROM entries WHERE active=0"
                ).fetchone()[0], 1)

    def test_complete_result_without_current_prompt_evidence_returns_to_review(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.json"
            old_work = root / "old-work"
            new_work = root / "new-work"
            archive(source)
            old = Factory(old_work)
            old.prepare(source_records(source), phase="phase2")
            water_id = old.db.execute(
                "SELECT id FROM entries WHERE json_extract(source,'$.term')='water'"
            ).fetchone()[0]
            old.db.execute(
                "UPDATE entries SET status='complete',result=? WHERE id=?",
                (json.dumps({"status": "translated", "senses": []}), water_id),
            )
            old.db.commit()
            old.close()

            result = migrate_checkpoint(source, old_work, new_work)

            self.assertEqual(result["legacy_prompt_reviews"], 1)
            self.assertEqual(result["states"]["needs_review"], 1)
            self.assertEqual(result["states"]["pending"], 1)


if __name__ == "__main__":
    unittest.main()
