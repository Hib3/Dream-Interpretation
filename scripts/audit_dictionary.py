"""Read-only audit of the legacy source and Japanese runtime dictionary."""
import argparse
from collections import Counter, defaultdict
import gzip
import hashlib
import json
from pathlib import Path
import re
import unicodedata

from build_ja_dictionary import meaning_source_text

ROOT = Path(__file__).resolve().parents[1]


def normalized(value):
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", str(value))).strip()


def audit(root=ROOT):
    with gzip.open(root / "data/dream_terms.json.gz", "rt", encoding="utf-8") as stream:
        source = json.load(stream)
    index = json.loads((root / "data/ja/terms.min.json").read_text(encoding="utf-8"))
    shards = {i: json.loads((root / f"data/ja/meanings-{i:02d}.min.json").read_text(encoding="utf-8"))
              for i in range(index["shard_count"])}
    by_language = defaultdict(Counter)
    source_counts = Counter()
    source_signatures = Counter()
    represented = defaultdict(set)
    counts = Counter()
    examples = defaultdict(list)
    japanese_meanings = Counter()
    histogram = Counter()
    provenance_coverage = Counter()
    source_terms_by_prefix = defaultdict(set)
    for entry in source["entries"]:
        language = entry.get("language", "unknown")
        stat = by_language[language]
        stat["source_entries"] += 1
        meanings = entry.get("meanings", [])
        histogram[len(meanings)] += 1
        stat["source_meanings"] += len(meanings)
        stat["empty_entries"] += not meanings
        stat["meanings_excluded_by_first_two_limit"] += max(0, len(meanings) - 2)
        stat["overly_long_source_terms"] += len(entry.get("term", "")) > 60
        source_terms_by_prefix[normalized(entry["term"])[:48]].add((language, entry["term"]))
        for pos, meaning in enumerate(meanings):
            text = meaning.get("text", "")
            stat["empty_source_meanings"] += not normalized(text)
            source_counts[meaning.get("source_name", "MISSING")] += 1
            source_signatures[(language, normalized(entry["term"]), normalized(text),
                               meaning.get("source_name"), meaning.get("source_url"))] += 1
            stat["term_equals_meaning"] += normalized(text) == normalized(entry["term"])
            stat["source_long_meanings_gt280"] += len(text) > 280
            stat["source_numeric_meanings"] += bool(re.search(r"\d", text))
            stat["source_religious_terms"] += bool(re.search(r"allah|God|Buddha|佛|天帝", text, re.I))
            stat["source_cross_reference"] += bool(re.search(r"\bsee\b|bakınız|参照", text, re.I))
            stat["source_repetition_signal"] += bool(re.search(r"(.{12,80})(?:\s*\1){2,}", text))
            stat["missing_source_metadata"] += not (meaning.get("source_name") and meaning.get("source_url"))
            if pos < 2:
                prepared = meaning_source_text(entry, meaning)
                if prepared != normalized(text):
                    stat["source_meanings_shortened_by_legacy_preprocessing"] += 1
                    stat["source_characters_removed_by_legacy_preprocessing"] += max(0, len(normalized(text)) - len(prepared))
                    if len(examples["preprocessing_loss"]) < 6:
                        examples["preprocessing_loss"].append({"language": language, "term": entry["term"],
                            "source_length": len(text), "sent_length": len(prepared)})
        direct = {m.get("source_name") for m in meanings}
        aggregate = {s.get("name") for s in entry.get("sources", [])}
        stat["entries_with_unlinked_aggregate_sources"] += bool(aggregate - direct)
    original_to_terms = defaultdict(set)
    for row in index["entries"]:
        if not isinstance(row, list) or len(row) != 6:
            counts["malformed_index_rows"] += 1
            continue
        term, _, languages, _, shard, slot = row
        try:
            senses = shards[shard][slot]
        except (KeyError, IndexError, TypeError):
            counts["broken_shard_references"] += 1
            continue
        counts["empty_runtime_entries"] += not senses
        counts["same_ja_term_multiple_senses"] += len(senses) > 1
        counts["terms_at_four_sense_cap"] += len(senses) == 4
        counts["terms_at_60_char_cap"] += len(term) == 60
        counts["overly_long_ja_terms_gt30"] += len(term) > 30
        counts["sentence_like_ja_terms"] += bool(re.search(r"[。！？]|です$|ます$|でしょう|という|ことを意味", term))
        for language in languages.split(","):
            by_language[language]["generated_ja_entries"] += 1
        for sense in senses:
            if not isinstance(sense, dict) or not all(key in sense for key in ("o", "t", "m", "s")):
                counts["malformed_senses"] += 1
                continue
            counts["runtime_senses"] += 1
            counts["senses_in_four_sense_slots"] += len(senses) == 4
            counts["original_at_48_char_cap"] += len(sense["o"]) == 48
            counts["empty_runtime_senses"] += not sense["m"]
            original_to_terms[(languages, sense["o"])].add(term)
            for language in languages.split(","):
                represented[(language, sense["o"])].update(sense["s"])
            for name in sense["s"]:
                provenance_coverage[name] += 1
            for meaning in sense["m"]:
                counts["runtime_meanings"] += 1
                japanese_meanings[normalized(meaning)] += 1
                flags = []
                if not meaning.strip(): flags.append("empty_meaning")
                if "�" in meaning: flags.append("encoding_problem")
                if re.search(r"[\u1000-\u109f]", meaning): flags.append("burmese_leakage")
                if re.search(r"[ğşıİĞŞ]", meaning): flags.append("turkish_leakage")
                if re.search(r"\b[a-zA-Z]{3,}(?:\s+[a-zA-Z]{3,}){2,}\b", meaning): flags.append("latin_sentence_signal")
                if meaning.endswith("…"): flags.append("explicit_truncation_marker")
                if re.search(r"https?://|<[^>]+>", meaning): flags.append("url_html_leakage")
                if re.search(r"(.{3,30})(?:[、 ]*\1){2,}|問題が問題|自分自身を集め", meaning): flags.append("suspicious_japanese_signal")
                for flag in flags:
                    counts[flag] += 1
                    for language in languages.split(","):
                        by_language[language][flag] += 1
                    if len(examples[flag]) < 3:
                        examples[flag].append({"term": term, "text": meaning})
    for entry in source["entries"]:
        language = entry["language"]
        prefix = normalized(entry["term"])[:48]
        found = represented.get((language, prefix), set())
        by_language[language]["unrepresented_source_entries_by_legacy_original_key"] += not bool(found)
    counts["distinct_ja_meanings"] = len(japanese_meanings)
    counts["duplicate_ja_meaning_occurrences"] = sum(n - 1 for n in japanese_meanings.values())
    counts["exact_duplicate_source_records"] = sum(n - 1 for n in source_signatures.values())
    counts["same_source_key_multiple_ja_terms"] = sum(len(terms) > 1 for terms in original_to_terms.values())
    counts["source_prefix48_collisions"] = sum(len(terms) > 1 for terms in source_terms_by_prefix.values())
    inventory = {}
    for path in [root / "data/dream_terms.json.gz", root / "data/source_registry.json", *sorted((root / "data/raw").glob("*"))]:
        if path.is_file():
            inventory[str(path.relative_to(root)).replace("\\", "/")] = {
                "bytes": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    return {"source_entries": len(source["entries"]), "generated_ja_entries": len(index["entries"]),
        "source_meanings": sum(len(e.get("meanings", [])) for e in source["entries"]),
        "by_language": dict(by_language), "source_meanings_per_entry": dict(sorted(histogram.items())),
        "counts": dict(counts), "source_coverage": {name: {"source_meanings": count,
            "runtime_senses_with_source_name": provenance_coverage[name]} for name, count in source_counts.items()},
        "examples": dict(examples), "immutable_inventory": inventory,
        "limitations": ["Legacy cache not found in repository; translated pre-truncation texts unavailable.",
            "Coverage by original key is an estimate: original labels were truncated to 48 characters and source associations were aggregated.",
            "Duplicate Japanese meanings across independent sources are not necessarily errors.",
            "Language leakage and suspicious Japanese counts are heuristic signals, not semantic accuracy rates.",
            "Raw cache for the additional Turkish GitHub Pages source is absent; merged source remains available."]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / ".codex/artifacts/legacy-audit.json")
    args = parser.parse_args()
    report = audit()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in ["source_entries", "source_meanings", "generated_ja_entries", "by_language", "counts"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
