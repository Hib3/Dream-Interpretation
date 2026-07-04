#!/usr/bin/env python3
"""Translate dream dictionary terms and meanings to Japanese.

Uses the public Google Translate web endpoint (client=gtx) with
newline-batched requests, a resumable JSONL cache, retries and
line-count verification (falls back to per-item translation when a
batch response loses line alignment).

Usage:
  python3 scripts/translate_dictionary.py \
      --input data/dream_terms.json \
      --cache /path/to/translation_cache.jsonl \
      [--jobs 4] [--only terms|meanings]
"""

import argparse
import gzip
import json
import random
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

ENDPOINT = "https://translate.googleapis.com/translate_a/single?client=gtx&dt=t&tl=ja&sl={sl}"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"
LANG_MAP = {"en": "en", "tr": "tr", "zh-Hant": "zh-TW", "my": "my"}
SEP = "\x1f"

MAX_BATCH_CHARS = 3600
MAX_BATCH_ITEMS = 48
MEANING_LIMIT = 280
MEANINGS_PER_ENTRY = 2

# Turkish SEO cross-reference boilerplate that adds noise, cut before translating.
TR_CUT_PATTERNS = [
    re.compile(r"Ayrıca şu rüya tabir\w* ve yorumlara bakınız.*$", re.S | re.I),
    re.compile(r"Bu rüya tabiri şu kaynaklardan.*$", re.S | re.I),
]

_print_lock = threading.Lock()
_cache_lock = threading.Lock()


def open_dictionary(path):
    """dream_terms.json / .json.gz を透過的に開く。"""
    if str(path).endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8")
    return open(path, encoding="utf-8")


def clean_text(text):
    return re.sub(r"\s+", " ", str(text or "")).strip()


def truncate_sentences(text, limit):
    text = clean_text(text)
    if len(text) <= limit:
        return text
    parts = re.split(r"(?<=[.!?。！？])\s*", text)
    out = ""
    for part in parts:
        if not part:
            continue
        if out and len(out) + len(part) + 1 > limit:
            break
        out = f"{out} {part}".strip()
        if len(out) >= limit * 0.6:
            break
    if not out:
        out = text[: limit - 1].rsplit(" ", 1)[0]
    return out.strip()


def collect_items(data, only=None):
    """Yield (lang, text) pairs to translate, deduplicated."""
    seen = set()
    items = []

    def add(lang, text):
        text = clean_text(text)
        if not text or lang not in LANG_MAP:
            return
        key = lang + SEP + text
        if key in seen:
            return
        seen.add(key)
        items.append((lang, text))

    for entry in data["entries"]:
        lang = entry["language"]
        if only in (None, "terms"):
            add(lang, entry["term"])
        if only in (None, "meanings"):
            for meaning in entry.get("meanings", [])[:MEANINGS_PER_ENTRY]:
                text = clean_text(meaning.get("text"))
                if lang == "tr":
                    for pattern in TR_CUT_PATTERNS:
                        text = pattern.sub("", text).strip()
                text = truncate_sentences(text, MEANING_LIMIT)
                # Burmese/Chinese proverb entries repeat the term as meaning.
                if text and text != clean_text(entry["term"]):
                    add(lang, text)
    return items


def load_cache(path):
    cache = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                    cache[row["k"]] = row["v"]
                except (json.JSONDecodeError, KeyError):
                    continue
    except FileNotFoundError:
        pass
    return cache


class Translator:
    def __init__(self, cache_path, cache):
        self.cache_path = cache_path
        self.cache = cache
        self.fh = open(cache_path, "a", encoding="utf-8")
        self.done = 0
        self.failed = 0

    def save(self, lang, source, translated):
        key = lang + SEP + source
        with _cache_lock:
            self.cache[key] = translated
            self.fh.write(json.dumps({"k": key, "v": translated}, ensure_ascii=False) + "\n")
            self.fh.flush()

    def request(self, lang, text, attempt=0):
        url = ENDPOINT.format(sl=LANG_MAP[lang])
        payload = urllib.parse.urlencode({"q": text}).encode()
        req = urllib.request.Request(url, data=payload, headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            return "".join(seg[0] for seg in body[0] if seg and seg[0])
        except Exception as err:  # noqa: BLE001 - retry any transport error
            if attempt >= 5:
                raise
            wait = min(60, (2 ** attempt) + random.random() * 2)
            with _print_lock:
                print(f"  retry {attempt + 1} after {wait:.1f}s: {err}", file=sys.stderr)
            time.sleep(wait)
            return self.request(lang, text, attempt + 1)

    def translate_batch(self, lang, batch):
        joined = "\n".join(batch)
        try:
            result = self.request(lang, joined)
        except Exception:
            self.failed += len(batch)
            return
        lines = [l.strip() for l in result.split("\n")]
        if len(lines) == len(batch):
            for src, dst in zip(batch, lines):
                self.save(lang, src, dst)
            self.done += len(batch)
            return
        # Misaligned response: translate items individually.
        for src in batch:
            try:
                self.save(lang, src, self.request(lang, src).strip())
                self.done += 1
            except Exception:
                self.failed += 1


def make_batches(items, cache):
    batches = []
    by_lang = {}
    for lang, text in items:
        if lang + SEP + text in cache:
            continue
        by_lang.setdefault(lang, []).append(text)
    for lang, texts in by_lang.items():
        batch, size = [], 0
        for text in texts:
            if batch and (size + len(text) > MAX_BATCH_CHARS or len(batch) >= MAX_BATCH_ITEMS):
                batches.append((lang, batch))
                batch, size = [], 0
            batch.append(text)
            size += len(text) + 1
        if batch:
            batches.append((lang, batch))
    random.shuffle(batches)
    return batches


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="data/dream_terms.json.gz")
    parser.add_argument("--cache", required=True)
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--only", choices=["terms", "meanings"])
    args = parser.parse_args()

    with open_dictionary(args.input) as fh:
        data = json.load(fh)

    items = collect_items(data, args.only)
    cache = load_cache(args.cache)
    translator = Translator(args.cache, cache)
    batches = make_batches(items, cache)
    total_remaining = sum(len(b) for _, b in batches)
    print(f"items={len(items)} cached={len(items) - total_remaining} remaining={total_remaining} batches={len(batches)}")

    start = time.time()

    def worker(job):
        lang, batch = job
        translator.translate_batch(lang, batch)
        if random.random() < 0.02:
            with _print_lock:
                rate = translator.done / max(1, time.time() - start)
                print(f"  progress done={translator.done} failed={translator.failed} rate={rate:.0f}/s")

    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        list(pool.map(worker, batches))

    print(f"finished done={translator.done} failed={translator.failed} elapsed={time.time() - start:.0f}s")


if __name__ == "__main__":
    main()
