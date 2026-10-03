"""Replay explicitly reviewed sample translations through the real factory, never production.

The reference file is authored in the Codex conversation. This measures infrastructure
and documented linguistic review, not independent model accuracy or live transport.
"""
import argparse
from collections import Counter
from pathlib import Path

from dictionary_pipeline import Factory, ROOT, atomic_json, cache_key, digest, dumps, normalize, read_json


def run(work, sample_path, translations_path, out):
    sample = read_json(sample_path)["records"]
    reference = read_json(translations_path)
    if reference["sample_hash"] != digest(sample):
        raise RuntimeError("Reference belongs to a different sample")
    by_source = {}
    for ref in reference["translations"]:
        record = sample[ref["sample_index"]]
        if not record["source"]["meaning"]:
            continue
        key = cache_key(record["source"])
        senses = []
        if ref.get("ja_meaning"):
            senses = [{"sense_key": "1", "ja_term": ref["ja_term"], "aliases": [],
                "units": [{"source_quote": normalize(record["source"]["meaning"]), "ja": ref["ja_meaning"], "kind": "interpretation"}],
                "tone": ref["tone"], "ambiguous": False, "translation_confidence": .95,
                "semantic_confidence": .95, "flags": []}]
        by_source[key] = {"id": key, "status": "translated" if senses else "unresolved",
                          "action": "replace" if senses else "unresolved", "reason": ref.get("reason", ""), "senses": senses}
    expected = {cache_key(r["source"]) for r in sample if r["source"]["meaning"]}
    if set(by_source) != expected:
        raise RuntimeError("Missing or extra representative reference IDs")
    factory = Factory(work)
    try:
        factory.prepare(sample)
        while request := factory.export_batch():
            factory.ingest(request["batch_id"], {"results": [by_source[i["id"]] for i in request["items"]]},
                           reference["author_model"])
        before = factory.statistics()
        factory.close()
        factory = Factory(work)
        factory.prepare(sample)
        if factory.export_batch() is not None:
            raise RuntimeError("Completed sample was resubmitted on resume")
        factory.export_master()
        index = factory.build(out, 4)
        report = {"sample_records": len(sample), "unique_records": before["entries"],
            "real_source_records": 50, "synthetic_records": 2, "metrics": before,
            "source_language_counts": dict(Counter(r["source"]["language"] for r in sample)),
            "runtime_entries": index["entry_count"], "sample_hash": digest(sample),
            "live_transport_tested": False, "independent_semantic_audit": False,
            "notes": reference["review_notes"], "resume_no_resubmission": True}
        atomic_json(Path(work) / "evaluation.json", report)
        print(dumps(report))
    finally:
        factory.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", type=Path, default=ROOT / ".codex/sample-work")
    parser.add_argument("--sample", type=Path, default=ROOT / ".codex/artifacts/representative-input.json")
    parser.add_argument("--references", type=Path, default=ROOT / ".codex/artifacts/representative-translations.json")
    parser.add_argument("--out", type=Path, default=ROOT / ".codex/staging-sample")
    args = parser.parse_args()
    run(args.work, args.sample, args.references, args.out)
