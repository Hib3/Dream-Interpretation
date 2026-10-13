import gzip
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import extend_dream_terms
from extend_dream_terms import RUYA_REPO_RAW, parse_ruya_page


class ParseRuyaPageTests(unittest.TestCase):
    def test_keeps_all_paragraphs_without_truncation_and_tracks_page(self):
        first = "Genel yorum: " + ("umut " * 100)
        second = "Dini yorum: " + ("bereket " * 100)
        short = "Kısa ek."
        html = (
            "<h1>Rüyada köpek görmek</h1>"
            f"<p>{first}</p><p>{second}</p><p>{short}</p>"
        )

        result = parse_ruya_page("ruyada-kopek-gormek.html", html)

        self.assertEqual(result["term"], "köpek")
        self.assertEqual(result["meaning"], "\n".join([first.strip(), second.strip(), short]))
        self.assertGreater(len(result["meaning"]), 600)
        self.assertEqual(
            result["source_url"],
            RUYA_REPO_RAW + "ruyada-kopek-gormek.html",
        )
        self.assertEqual(result["parser_version"], extend_dream_terms.PARSER_VERSION)

    def test_old_parsed_cache_rows_are_not_treated_as_current(self):
        old_row = {"slug": "old.html", "term": "old", "meaning": "truncated"}
        current_row = {
            "slug": "current.html", "term": "current", "meaning": "full",
            "source_url": RUYA_REPO_RAW + "current.html",
            "parser_version": extend_dream_terms.PARSER_VERSION,
        }
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "pages.jsonl"
            cache.write_text(
                "\n".join(json.dumps(row) for row in (old_row, current_row)),
                encoding="utf-8",
            )
            with patch.object(extend_dream_terms, "PAGES_CACHE", cache):
                rows = extend_dream_terms.load_cached_pages()

        self.assertEqual(list(rows), ["current.html"])

    def test_dictionary_merge_keeps_language_collision_separate_and_page_url(self):
        initial = {
            "entries": [{
                "term": "gift", "term_normalized": "gift", "language": "en",
                "aliases": [], "meanings": [{"text": "A present."}], "sources": [],
            }],
        }
        with tempfile.TemporaryDirectory() as directory:
            dictionary = Path(directory) / "dictionary.json.gz"
            with gzip.open(dictionary, "wt", encoding="utf-8") as stream:
                json.dump(initial, stream)
            with patch.object(extend_dream_terms, "DICT_PATH", dictionary):
                extend_dream_terms.merge_into_dictionary({"gift.html": {
                    "term": "gift", "alias": "Rüyada gift görmek",
                    "meaning": "Bir rüya yorumu.",
                    "source_url": RUYA_REPO_RAW + "gift.html",
                }})
            with gzip.open(dictionary, "rt", encoding="utf-8") as stream:
                result = json.load(stream)

        self.assertEqual({entry["language"] for entry in result["entries"]}, {"en", "tr"})
        turkish = next(entry for entry in result["entries"] if entry["language"] == "tr")
        self.assertEqual(turkish["meanings"][0]["source_url"], RUYA_REPO_RAW + "gift.html")

    def test_source_replacement_is_staged_and_keeps_shared_meaning_origins(self):
        initial = {
            "entries": [{
                "term": "cat", "term_normalized": "cat", "language": "tr",
                "aliases": [],
                "meanings": [
                    {"text": "Old truncated text.", "source_name": extend_dream_terms.RUYA_SOURCE["name"],
                     "source_url": extend_dream_terms.RUYA_SOURCE["url"], "license_note": "old"},
                    {"text": "A shared meaning.", "source_name": "Other",
                     "source_url": "https://example.test/other", "license_note": "test"},
                ],
                "sources": [dict(extend_dream_terms.RUYA_SOURCE), {
                    "name": "Other", "url": "https://example.test/other", "license_note": "test",
                }],
            }],
        }
        with tempfile.TemporaryDirectory() as directory:
            original = Path(directory) / "original.json.gz"
            staged = Path(directory) / "staged.json.gz"
            with gzip.open(original, "wt", encoding="utf-8") as stream:
                json.dump(initial, stream)
            with patch.object(extend_dream_terms, "DICT_PATH", original):
                extend_dream_terms.merge_into_dictionary({"cat.html": {
                    "term": "cat", "alias": "Rüyada cat görmek",
                    "meaning": "A shared meaning.",
                    "source_url": RUYA_REPO_RAW + "cat.html",
                }}, output_path=staged, replace_source=True)
            with gzip.open(original, "rt", encoding="utf-8") as stream:
                untouched = json.load(stream)
            with gzip.open(staged, "rt", encoding="utf-8") as stream:
                migrated = json.load(stream)

        self.assertEqual(untouched, initial)
        cat = migrated["entries"][0]
        self.assertEqual([meaning["text"] for meaning in cat["meanings"]], ["A shared meaning."])
        self.assertEqual(
            {source["name"] for source in cat["meanings"][0]["sources"]},
            {"Other", extend_dream_terms.RUYA_SOURCE["name"]},
        )
        self.assertTrue(any(source["name"] == extend_dream_terms.RUYA_SOURCE["name"]
                            for source in cat["sources"]))

    def test_source_replacement_refuses_to_overwrite_original(self):
        with tempfile.TemporaryDirectory() as directory:
            original = Path(directory) / "original.json.gz"
            with gzip.open(original, "wt", encoding="utf-8") as stream:
                json.dump({"entries": []}, stream)
            with patch.object(extend_dream_terms, "DICT_PATH", original):
                with self.assertRaisesRegex(ValueError, "separate staging"):
                    extend_dream_terms.merge_into_dictionary({}, replace_source=True)


if __name__ == "__main__":
    unittest.main()
