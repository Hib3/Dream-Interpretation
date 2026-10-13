import json
import sqlite3
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / ".codex/dictionary-work/checkpoint.sqlite"
STATE = ROOT / ".codex/dream-dictionary-state.md"


def main():
    if not DB.is_file() or not STATE.is_file():
        print("completion evidence missing")
        return 1
    with sqlite3.connect(DB) as db:
        counts = dict(db.execute("SELECT status, COUNT(*) FROM entries GROUP BY status"))
    total = sum(counts.values())
    unfinished = {key: counts.get(key, 0) for key in
                  ("pending", "processing", "failed", "needs_review") if counts.get(key, 0)}
    state = STATE.read_text("utf-8")
    required_state = ("DREAM DICTIONARY REBUILD COMPLETE", "FINAL STATUS", "READY")
    index = ROOT / "data/ja/terms.min.json"
    shards = list((ROOT / "data/ja").glob("meanings-*.min.json"))
    issues = []
    if total != 86073:
        issues.append(f"source count {total}/86073")
    if unfinished:
        issues.append(f"unfinished statuses {json.dumps(unfinished, ensure_ascii=False)}")
    if counts.get("complete", 0) + counts.get("unresolved", 0) != total:
        issues.append("terminal status count mismatch")
    if not all(marker in state for marker in required_state):
        issues.append("state file has no READY completion report")
    if not index.is_file() or not shards:
        issues.append("production Japanese index/shards missing")
    if issues:
        print("INCOMPLETE: " + "; ".join(issues))
        return 1
    print(f"READY: {total} entries; complete={counts.get('complete', 0)}; "
          f"unresolved={counts.get('unresolved', 0)}; shards={len(shards)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
