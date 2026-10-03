"""Select reproducible real-source coverage, plus explicitly labelled malformed/duplicate fixtures."""
import argparse
from collections import Counter
import copy
from pathlib import Path
import re

from dictionary_pipeline import ROOT, atomic_json, digest, source_records


def select(path):
    records = list(source_records(path))
    chosen, seen = [], set()

    def add(candidates, category, count=1):
        candidates = sorted(candidates, key=lambda r: (len(r["source"]["meaning"] or ""), r["entry_id"]))
        added = 0
        for record in candidates:
            if record["entry_id"] in seen:
                continue
            item = copy.deepcopy(record)
            item["sample_category"] = category
            chosen.append(item)
            seen.add(item["entry_id"])
            added += 1
            if added == count:
                break
        if added != count:
            raise RuntimeError(f"Insufficient sample category {category}: {added}/{count}")

    for term in ["bank", "bat", "spring", "date", "fly", "bill, you reckon", "seal"]:
        add([r for r in records if r["source"]["language"] == "en" and r["source"]["term"].casefold() == term], "en_polysemy_" + term)
    en = [r for r in records if r["source"]["language"] == "en"]
    add([r for r in en if 900 < len(r["source"]["meaning"]) < 1400], "en_long")
    add([r for r in en if r["source"]["term"].casefold() in {"january", "february", "march", "april", "may", "june"}], "month", 2)
    add([r for r in en if re.search(r"\d", r["source"]["term"])], "number", 2)
    add([r for r in en if 60 < len(r["source"]["meaning"]) < 200 and "see " not in r["source"]["meaning"].lower()], "en_simple", 8)
    tr = [r for r in records if r["source"]["language"] == "tr"]
    add([r for r in tr if re.search(r"allah|namaz|kuran", r["source"]["meaning"], re.I) and len(r["source"]["meaning"]) > 80], "tr_religious", 2)
    add([r for r in tr if 1200 < len(r["source"]["meaning"]) < 1600], "tr_long")
    add([r for r in tr if r["source"]["term"].casefold() in {"yüz", "gül", "ay", "yaz", "dil"}], "tr_ambiguous", 2)
    add([r for r in tr if 70 < len(r["source"]["meaning"]) < 250], "tr_simple", 7)
    zh = [r for r in records if r["source"]["language"] == "zh-Hant"]
    add(zh, "zh_classical", 6)
    add([r for r in zh if len(r["source"]["meaning"]) == 7], "zh_seven_characters", 6)
    add([r for r in records if r["source"]["language"] == "my"], "my_term_equals_meaning", 6)
    duplicate = copy.deepcopy(chosen[0])
    duplicate["origin"] = {"entry_index": "synthetic_duplicate", "meaning_index": 0}
    duplicate["sample_category"] = "synthetic_duplicate"
    malformed = copy.deepcopy(chosen[0])
    malformed["source"]["meaning"] = None
    malformed["entry_id"] = digest(malformed["source"])
    malformed["origin"] = {"entry_index": "synthetic_malformed", "meaning_index": 0}
    malformed["sample_category"] = "synthetic_malformed"
    return chosen + [duplicate, malformed]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=ROOT / "data/dream_terms.json.gz")
    parser.add_argument("--out", type=Path, default=ROOT / ".codex/artifacts/representative-input.json")
    args = parser.parse_args()
    sample = select(args.input)
    atomic_json(args.out, {"selection_version": 1, "records": sample})
    print({"records": len(sample), "languages": dict(Counter(r["source"]["language"] for r in sample)), "path": str(args.out)})
