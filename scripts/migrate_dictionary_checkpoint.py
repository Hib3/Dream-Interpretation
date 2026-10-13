"""Clone a checkpoint and activate a revised source snapshot without touching the original."""
import argparse
import json
import sqlite3
from pathlib import Path

from dictionary_pipeline import (
    Factory, LANGUAGES, PipelineError, PROMPT_VERSION, ROOT, TASK_SCHEMA_VERSION,
    cache_key, digest, source_records, task_source,
)


def migrate_checkpoint(source_path, old_work, new_work):
    source_path = Path(source_path).resolve()
    old_db_path = Path(old_work).resolve() / "checkpoint.sqlite"
    new_work = Path(new_work).resolve()
    new_db_path = new_work / "checkpoint.sqlite"
    if not source_path.is_file() or not old_db_path.is_file():
        raise PipelineError("MIGRATION_INPUT_MISSING")
    if new_db_path == old_db_path or (new_work.exists() and any(new_work.iterdir())):
        raise PipelineError("MIGRATION_TARGET_MUST_BE_NEW_OR_EMPTY")

    old = sqlite3.connect(old_db_path.as_uri() + "?mode=ro", uri=True)
    old.row_factory = sqlite3.Row
    phase = json.loads(old.execute("SELECT value FROM meta WHERE key='phase'").fetchone()[0])
    if phase != "phase2":
        old.close()
        raise PipelineError("MIGRATION_REQUIRES_PHASE2_CHECKPOINT")
    old_active_ids = {row[0] for row in old.execute("SELECT id FROM entries WHERE active=1")}
    integrity = old.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        old.close()
        raise PipelineError("CHECKPOINT_INTEGRITY " + integrity)

    new_work.mkdir(parents=True, exist_ok=True)
    clone = sqlite3.connect(new_db_path)
    try:
        old.backup(clone)
    finally:
        clone.close()
        old.close()

    records = list(source_records(source_path))
    expected_keys = {}
    for record in records:
        source = record["source"]
        try:
            normalized = task_source(source)
            valid = source["language"] in LANGUAGES and all(normalized.values())
        except (PipelineError, KeyError, TypeError):
            valid = False
        expected_keys[record["entry_id"]] = cache_key(source) if valid else digest(record)

    factory = Factory(new_work)
    try:
        batch_versions = {}
        for row in factory.db.execute("SELECT request,state FROM batches ORDER BY rowid"):
            request = json.loads(row["request"])
            if row["state"] == "complete":
                version = (request.get("prompt_version"), request.get("task_schema_version"))
                for binding in request.get("bindings", []):
                    batch_versions[binding["entry_id"]] = version

        rebased = legacy_prompt_reviews = 0
        with factory.db:
            for row in factory.db.execute(
                "SELECT id,source,task_key,status,result,flags,pass_count FROM entries WHERE active=1"
            ).fetchall():
                if row["id"] not in expected_keys:
                    continue
                key = expected_keys[row["id"]]
                if row["task_key"] != key:
                    factory.db.execute("UPDATE entries SET task_key=? WHERE id=?", (key, row["id"]))
                    rebased += 1
                if row["status"] == "complete" and row["result"] and row["pass_count"] < 2 and batch_versions.get(row["id"]) != (
                    PROMPT_VERSION, TASK_SCHEMA_VERSION
                ):
                    flags = json.loads(row["flags"])
                    if "PIPELINE_ISSUE:legacy_prompt_review" not in flags:
                        flags.append("PIPELINE_ISSUE:legacy_prompt_review")
                    factory.db.execute("UPDATE entries SET status='needs_review',flags=? WHERE id=?",
                                       (json.dumps(flags, ensure_ascii=False), row["id"]))
                    legacy_prompt_reviews += 1
            factory.event("source_migration_key_rebase", {
                "rebased_keys": rebased,
                "legacy_prompt_reviews": legacy_prompt_reviews,
                "prompt_version": PROMPT_VERSION,
                "task_schema_version": TASK_SCHEMA_VERSION,
                "reason": "source snapshot migration; identical source IDs retain results, legacy prompt outputs go to review",
            })

        factory.prepare(records, phase="phase2")
        staged_integrity = factory.db.execute("PRAGMA integrity_check").fetchone()[0]
        if staged_integrity != "ok":
            raise PipelineError("STAGING_CHECKPOINT_INTEGRITY " + staged_integrity)
        new_active_ids = {row[0] for row in factory.db.execute("SELECT id FROM entries WHERE active=1")}
        reused = len(old_active_ids & new_active_ids)
        return {"source_entries": len(new_active_ids), "reused_source_entries": reused,
                "new_or_changed_entries": len(new_active_ids - old_active_ids),
                "inactive_old_entries": len(old_active_ids - new_active_ids),
                "rebased_cache_keys": rebased,
                "legacy_prompt_reviews": legacy_prompt_reviews,
                "states": factory.statistics()["states"],
                "checkpoint_integrity": staged_integrity}
    finally:
        factory.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--from-work", type=Path, default=ROOT / ".codex/dictionary-work")
    parser.add_argument("--to-work", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(migrate_checkpoint(args.source, args.from_work, args.to_work), ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except (PipelineError, OSError, sqlite3.Error) as error:
        raise SystemExit(str(error))
