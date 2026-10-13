#!/usr/bin/env python3
"""Extend data/dream_terms.json with additional public sources.

This script performs an additive merge: it loads the existing merged
dictionary and appends new sources without re-fetching the original
15 sources (some of which require network access unavailable in every
environment).

Currently implemented additional sources:
  - Rüya Tabiri GitHub Pages dataset (all unique Turkish dream pages
    listed in the repository sitemap, GPL-3.0, fetched from raw pages).

Usage:
  python3 scripts/extend_dream_terms.py [--fetch-only] [--limit N]
"""

import argparse
import gzip
import html as html_lib
import json
import os
import re
import threading
import time
import random
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = ROOT / "data" / "raw"
DICT_PATH = ROOT / "data" / "dream_terms.json.gz"
PAGES_CACHE = RAW_DIR / "ruya_tabiri_pages.jsonl"
PARSER_VERSION = 2

UA = "DreamInterpretationDatasetBuilder/0.2"
RUYA_REPO_RAW = "https://raw.githubusercontent.com/ruya-tabiri/ruya-tabiri.github.io/main/"
RUYA_SOURCE = {
    "name": "Rüya Tabiri GitHub Pages Dataset",
    "url": "https://github.com/ruya-tabiri/ruya-tabiri.github.io",
    "license_note": "Repository licensed under GPL-3.0; attribution retained, verify before commercial redistribution.",
}

_lock = threading.Lock()


def http_get(url, attempt=0):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            return res.read().decode("utf-8", "replace")
    except Exception:
        if attempt >= 4:
            raise
        time.sleep(min(30, 2**attempt + random.random()))
        return http_get(url, attempt + 1)


def fetch_sitemap_slugs():
    slugs = []
    for i in range(1, 30):
        try:
            xml = http_get(f"{RUYA_REPO_RAW}sitemaps/sitemap{i}.xml")
        except Exception:
            break
        found = re.findall(r"<loc>[^<]*/([^/<]+\.html)</loc>", xml)
        if not found:
            break
        slugs.extend(found)
    return sorted(set(slugs))


TAG_RE = re.compile(r"<[^>]+>")


def parse_ruya_page(slug, page_html):
    h1 = re.search(r"<h1[^>]*>(.*?)</h1>", page_html, re.S)
    if not h1:
        return None
    title = html_lib.unescape(TAG_RE.sub(" ", h1.group(1)))
    title = re.sub(r"\s+", " ", title).strip()
    if not title:
        return None

    paragraphs = []
    for raw in re.findall(r"<p[^>]*>(.*?)</p>", page_html, re.S):
        text = html_lib.unescape(TAG_RE.sub(" ", raw))
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            paragraphs.append(text)
    if not paragraphs:
        return None

    term = re.sub(r"^r[uü]yada\s+", "", title, flags=re.I).strip()
    term = re.sub(r"\s+g[oö]rmek$", "", term, flags=re.I).strip()
    if not term:
        return None

    return {
        "slug": slug,
        "term": term,
        "alias": title,
        "meaning": "\n".join(paragraphs),
        "source_url": RUYA_REPO_RAW + slug,
        "parser_version": PARSER_VERSION,
    }


def load_cached_pages():
    rows = {}
    if PAGES_CACHE.exists():
        with PAGES_CACHE.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                    if row.get("parser_version") == PARSER_VERSION and row.get("source_url"):
                        rows[row["slug"]] = row
                except (json.JSONDecodeError, KeyError):
                    continue
    return rows


def fetch_ruya_pages(limit=None, jobs=6):
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    slugs = fetch_sitemap_slugs()
    print(f"sitemap slugs: {len(slugs)}")
    cached = load_cached_pages()
    todo = [s for s in slugs if s not in cached]
    if limit:
        todo = todo[:limit]
    print(f"cached: {len(cached)}, fetching: {len(todo)}")
    if not todo:
        return cached

    done = 0
    failed = 0
    started = time.time()
    with PAGES_CACHE.open("a", encoding="utf-8") as out:

        def worker(slug):
            nonlocal done, failed
            try:
                page = http_get(RUYA_REPO_RAW + slug)
                row = parse_ruya_page(slug, page)
                if row is None:
                    row = {"slug": slug, "term": "", "alias": "", "meaning": ""}
                with _lock:
                    out.write(json.dumps(row, ensure_ascii=False) + "\n")
                    out.flush()
                    cached[slug] = row
                    done += 1
                    if done % 500 == 0:
                        rate = done / max(1, time.time() - started)
                        eta = (len(todo) - done) / max(rate, 0.1) / 60
                        print(f"  {done}/{len(todo)} ({rate:.0f}/s, eta {eta:.0f}m)")
            except Exception as err:
                with _lock:
                    failed += 1
                    if failed <= 5:
                        print(f"  fail {slug}: {err}")

        with ThreadPoolExecutor(max_workers=jobs) as pool:
            list(pool.map(worker, todo))
    print(f"fetch finished: done={done} failed={failed}")
    return cached


def merge_into_dictionary(pages, input_path=None, output_path=None, replace_source=False):
    input_path = Path(input_path or DICT_PATH).resolve()
    output_path = Path(output_path or input_path).resolve()
    if replace_source and output_path == input_path:
        raise ValueError("Source replacement must write to a separate staging file")
    opener = gzip.open if input_path.suffix == ".gz" else open
    with opener(input_path, "rt", encoding="utf-8") as fh:
        data = json.load(fh)
    entries = data["entries"]
    if replace_source:
        kept = []
        for entry in entries:
            old_meanings = [m for m in entry["meanings"]
                            if m.get("source_name") == RUYA_SOURCE["name"]]
            entry["meanings"] = [m for m in entry["meanings"] if m not in old_meanings]
            entry["sources"] = [s for s in entry["sources"] if s["name"] != RUYA_SOURCE["name"]]
            if entry["meanings"] or not old_meanings:
                kept.append(entry)
        entries = kept
    by_key = {(e["language"], e["term_normalized"]): e for e in entries}
    added_terms = 0
    added_meanings = 0

    for row in pages.values():
        term = re.sub(r"\s+", " ", row.get("term") or "").strip()
        meaning = (row.get("meaning") or "").strip()
        if not term or not meaning:
            continue
        key = ("tr", term.casefold())
        alias = (row.get("alias") or "").strip()
        entry = by_key.get(key)
        if entry is None:
            entry = {
                "term": term,
                "term_normalized": key[1],
                "language": "tr",
                "aliases": [alias] if alias else [],
                "meanings": [],
                "sources": [],
            }
            by_key[key] = entry
            added_terms += 1
        else:
            if alias and alias not in entry["aliases"]:
                entry["aliases"] = sorted(set(entry["aliases"]) | {alias}, key=str.casefold)

        mkey = re.sub(r"\s+", " ", meaning).strip().casefold()
        existing = next((m for m in entry["meanings"]
                         if re.sub(r"\s+", " ", m["text"]).strip().casefold() == mkey), None)
        attribution = {
            "name": RUYA_SOURCE["name"],
            "url": row.get("source_url") or RUYA_SOURCE["url"],
            "license_note": RUYA_SOURCE["license_note"],
        }
        if existing is None:
            entry["meanings"].append(
                {
                    "text": meaning,
                    "source_name": attribution["name"],
                    "source_url": attribution["url"],
                    "license_note": attribution["license_note"],
                    "sources": [attribution],
                }
            )
            added_meanings += 1
        else:
            direct_sources = existing.setdefault("sources", [{
                "name": existing.get("source_name", ""),
                "url": existing.get("source_url", ""),
                "license_note": existing.get("license_note", ""),
            }])
            if attribution not in direct_sources:
                direct_sources.append(attribution)
        if RUYA_SOURCE["name"] not in {s["name"] for s in entry["sources"]}:
            entry["sources"].append(dict(RUYA_SOURCE))

    entries = sorted(by_key.values(), key=lambda e: (e["language"], e["term_normalized"]))
    language_counts = {}
    source_names = set()
    for entry in entries:
        entry["meaning_count"] = len(entry["meanings"])
        entry["source_count"] = len(entry["sources"])
        language_counts[entry["language"]] = language_counts.get(entry["language"], 0) + 1
        for source in entry["sources"]:
            source_names.add(source["name"])

    data["generated_at"] = datetime.now(timezone.utc).isoformat()
    data["entry_count"] = len(entries)
    data["language_counts"] = dict(sorted(language_counts.items()))
    data["source_count"] = len(source_names)
    data["entries"] = entries
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(output_path.name + ".tmp")
    with gzip.open(temporary, "wt", encoding="utf-8", compresslevel=9) as fh:
        json.dump(data, fh, ensure_ascii=False, separators=(",", ":"))
    os.replace(temporary, output_path)
    print(f"dictionary: {len(entries)} entries (+{added_terms} new terms, +{added_meanings} meanings)")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fetch-only", action="store_true")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--jobs", type=int, default=6)
    parser.add_argument("--input", type=Path, default=DICT_PATH,
                        help="Merged dictionary to extend (JSON or gzip JSON)")
    parser.add_argument("--output", type=Path, help="Write the merged dictionary to this path")
    parser.add_argument("--replace-source", action="store_true",
                        help="Replace this source's old meanings; requires a separate --output")
    args = parser.parse_args()

    if args.replace_source and (args.fetch_only or not args.output or args.input.resolve() == args.output.resolve()):
        parser.error("--replace-source requires a separate --input/--output and cannot be used with --fetch-only")

    pages = fetch_ruya_pages(limit=args.limit, jobs=args.jobs)
    if not args.fetch_only:
        merge_into_dictionary(pages, input_path=args.input, output_path=args.output,
                              replace_source=args.replace_source)


if __name__ == "__main__":
    main()
