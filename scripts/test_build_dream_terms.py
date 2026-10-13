import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_dream_terms import merge_entries


def source(name):
    return {"name": name, "url": f"https://example.test/{name}", "license_note": "test"}


def entry(language, term, meaning, source_name):
    return {
        "language": language,
        "term": term,
        "meaning": meaning,
        "aliases": [],
        "source": source(source_name),
    }


class MergeEntriesTests(unittest.TestCase):
    def test_language_is_part_of_term_identity(self):
        result = merge_entries([
            entry("en", "gift", "A present.", "English"),
            entry("tr", "gift", "Poison.", "Turkish"),
        ])

        self.assertEqual([(item["language"], item["term"]) for item in result], [
            ("en", "gift"), ("tr", "gift")
        ])

    def test_one_character_terms_are_retained(self):
        result = merge_entries([entry("en", "A", "A letter.", "Source")])

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["term"], "A")

    def test_duplicate_meaning_keeps_all_direct_sources(self):
        result = merge_entries([
            entry("en", "dog", "A faithful companion.", "First"),
            entry("en", "dog", " A   faithful companion. ", "Second"),
        ])

        self.assertEqual(len(result), 1)
        self.assertEqual(len(result[0]["meanings"]), 1)
        self.assertEqual(
            [item["name"] for item in result[0]["meanings"][0]["sources"]],
            ["First", "Second"],
        )


if __name__ == "__main__":
    unittest.main()
