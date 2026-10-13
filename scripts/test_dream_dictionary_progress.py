import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dream_dictionary_progress import read_progress


class ProgressTests(unittest.TestCase):
    def test_translation_and_review_each_advance_progress(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "checkpoint.sqlite"
            with sqlite3.connect(database) as db:
                db.execute("CREATE TABLE entries(status TEXT NOT NULL, pass_count INTEGER NOT NULL)")
                db.execute("INSERT INTO entries VALUES('pending', 0)")
            db.close()
            before = read_progress(database)["completed"]
            with sqlite3.connect(database) as db:
                db.execute("UPDATE entries SET status='needs_review', pass_count=1")
            db.close()
            translated = read_progress(database)["completed"]
            with sqlite3.connect(database) as db:
                db.execute("UPDATE entries SET status='unresolved', pass_count=2")
            db.close()
            reviewed = read_progress(database)["completed"]
            self.assertLess(before, translated)
            self.assertLess(translated, reviewed)


if __name__ == "__main__":
    unittest.main()
