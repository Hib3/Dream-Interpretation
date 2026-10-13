import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path


def read_progress(database):
    path = Path(database).resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)) as db:
        counts = dict(db.execute("SELECT status, COUNT(*) FROM entries GROUP BY status"))
        passes = db.execute("SELECT COALESCE(SUM(pass_count), 0) FROM entries").fetchone()[0]
    terminal = sum(counts.get(status, 0) for status in ("complete", "needs_review", "unresolved"))
    return {"completed": terminal + passes, "terminal_entries": terminal,
            "review_passes": passes, "counts": counts}


if __name__ == "__main__":
    print(json.dumps(read_progress(sys.argv[1])))
