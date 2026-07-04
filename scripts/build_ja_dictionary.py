#!/usr/bin/env python3
"""Build the Japanese-first dream dictionary from translations.

Reads data/dream_terms.json plus the translation cache produced by
translate_dictionary.py, merges entries that map to the same Japanese
term, assigns a fortune tone (吉/凶/中), and emits:

  data/ja/terms.min.json     - compact term index loaded at startup
  data/ja/meanings-NN.json   - meaning shards fetched on demand

Usage:
  python3 scripts/build_ja_dictionary.py --cache /path/translation_cache.jsonl
"""

import argparse
import hashlib
import json
import math
import os
import re
import sys
import unicodedata

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from translate_dictionary import (  # noqa: E402
    MEANINGS_PER_ENTRY,
    MEANING_LIMIT,
    SEP,
    TR_CUT_PATTERNS,
    clean_text,
    load_cache,
    truncate_sentences,
)

SHARD_COUNT = 32
MAX_MEANINGS = 3
MAX_MEANING_CANDIDATES = 8  # 統合時は多めに集め、表示語との関連順に並べてから絞る
MEANING_JA_LIMIT = 220
TERM_JA_LIMIT = 60

POSITIVE = [
    "吉", "幸運", "幸せ", "成功", "繁栄", "喜び", "順調", "達成", "利益", "豊か",
    "昇進", "健康", "平和", "安心", "祝福", "満足", "発展", "勝利", "愛情", "良い",
    "チャンス", "希望", "恵まれ", "上昇", "向上", "実り", "報われ", "尊敬", "信頼",
]
NEGATIVE = [
    "凶", "不吉", "警告", "注意", "不安", "失敗", "病気", "トラブル", "危険", "損失",
    "悪い", "困難", "裏切り", "別れ", "苦し", "災い", "障害", "悩み", "ストレス",
    "対立", "喪失", "後悔", "孤独", "疲れ", "焦り", "誤解", "妨げ", "悪化",
]

KEYWORD_RE = re.compile(r"[一-鿿々]+|[ァ-・ー]+|[a-z0-9]+")
STOP_KEYWORDS = {"夢", "見", "意味", "兆", "暗示", "象徴", "解釈", "占"}


def normalize_ja(text):
    text = unicodedata.normalize("NFKC", str(text or "")).lower().strip()
    return re.sub(r"\s+", " ", text)


TRAILING_PARTICLES = "のにでをへとがはも"


def clean_term_ja(term):
    """Clean translation artifacts from a Japanese term surface."""
    term = normalize_ja(term)
    # 「空/空」「逃げる / 逃げる」のような同語反復を畳む
    m = re.match(r"^(.+?)\s*/\s*(.+)$", term)
    if m and m.group(1).strip() == m.group(2).strip():
        term = m.group(1).strip()
    # 「あなたは泳いでいます」のような人称代名詞の前置きを外す
    term = re.sub(r"^(あなた|わたし|私|彼女|彼)(は|が|の)", "", term).strip() or term
    # 「泳ぐ、あなたは泳ぎます」「ビル、そうだね」は最短の断片を採用する
    if "、" in term:
        parts = [p.strip() for p in term.split("、") if p.strip()]
        if parts:
            term = min(parts, key=len)
    # 「水b」のような辞書の索引記号の名残を除去
    term = re.sub(r"(?<=[一-鿿ぁ-んァ-ヶー])[a-z]$", "", term)
    # 「空の」「海で」など、活用由来の末尾助詞を1文字だけ落とす
    if len(term) >= 2 and term[-1] in TRAILING_PARTICLES and re.match(r"[一-鿿ァ-ヶ]", term[-2]):
        term = term[:-1]
    return term.strip()


def merge_key(term_ja):
    key = normalize_ja(term_ja)
    key = re.sub(r"[「」『』()\[\]。、.,!?！?・…\"']", "", key)
    key = re.sub(r"\s+", "", key)
    return key


def tone_of(texts, orig_terms):
    joined = "".join(texts)
    score = sum(joined.count(w) for w in POSITIVE) - sum(joined.count(w) for w in NEGATIVE)
    # Classical Chinese entries carry explicit 吉/凶 markers in the source term.
    for orig in orig_terms:
        if "吉" in orig:
            score += 2
        if "凶" in orig:
            score -= 2
    if score >= 1:
        return 1
    if score <= -1:
        return -1
    return 0


def meaning_source_text(entry, meaning):
    text = clean_text(meaning.get("text"))
    if entry["language"] == "tr":
        for pattern in TR_CUT_PATTERNS:
            text = pattern.sub("", text).strip()
    return truncate_sentences(text, MEANING_LIMIT)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="data/dream_terms.json")
    parser.add_argument("--cache", required=True)
    parser.add_argument("--outdir", default="data/ja")
    args = parser.parse_args()

    cache = load_cache(args.cache)
    with open(args.input, encoding="utf-8") as fh:
        data = json.load(fh)

    def translated(lang, text):
        return cache.get(lang + SEP + clean_text(text))

    merged = {}
    skipped = {"no_term": 0, "no_meaning": 0}

    for entry in data["entries"]:
        lang = entry["language"]
        term_ja = clean_term_ja(clean_text(translated(lang, entry["term"])))
        if not term_ja:
            skipped["no_term"] += 1
            continue
        term_ja = term_ja[:TERM_JA_LIMIT].strip()
        key = merge_key(term_ja)
        if not key:
            skipped["no_term"] += 1
            continue

        meanings_ja = []
        for meaning in entry.get("meanings", [])[:MEANINGS_PER_ENTRY]:
            source_text = meaning_source_text(entry, meaning)
            if not source_text or source_text == clean_text(entry["term"]):
                continue
            text_ja = clean_text(translated(lang, source_text))
            if text_ja:
                meanings_ja.append(
                    {
                        "t": text_ja[:MEANING_JA_LIMIT].strip(),
                        "s": meaning.get("source_name") or "",
                    }
                )
        # Proverb-style entries (my/zh) repeat the term as the meaning:
        # reuse the translated sentence when nothing else is available.
        if not meanings_ja and len(term_ja) >= 12:
            meanings_ja.append({"t": term_ja, "s": entry["sources"][0]["name"] if entry.get("sources") else ""})
        if not meanings_ja:
            skipped["no_meaning"] += 1
            continue

        slot = merged.setdefault(
            key,
            {"term": term_ja, "langs": [], "orig": [], "meanings": [], "sources": []},
        )
        # Prefer the shortest surface form as the display term.
        if len(term_ja) < len(slot["term"]):
            slot["term"] = term_ja
        if lang not in slot["langs"]:
            slot["langs"].append(lang)
        orig = clean_text(entry["term"])[:48]
        if orig not in slot["orig"] and len(slot["orig"]) < 3:
            slot["orig"].append(orig)
        for m in meanings_ja:
            if len(slot["meanings"]) >= MAX_MEANING_CANDIDATES:
                break
            head = m["t"][:36]
            if not any(existing["t"][:36] == head for existing in slot["meanings"]):
                slot["meanings"].append(m)
        for src in entry.get("sources", [])[:2]:
            name = src.get("name")
            if name and name not in slot["sources"] and len(slot["sources"]) < 3:
                slot["sources"].append(name)

    items = sorted(merged.items())
    shard_size = math.ceil(len(items) / SHARD_COUNT)

    os.makedirs(args.outdir, exist_ok=True)
    term_rows = []
    shards = [[] for _ in range(SHARD_COUNT)]
    for i, (key, slot) in enumerate(items):
        shard_id = min(i // shard_size, SHARD_COUNT - 1)
        idx = len(shards[shard_id])
        # 同じ訳語に統合された同綴り異義の意味は、表示語の漢字を含むものを先頭へ
        term_runs = [r for r in KEYWORD_RE.findall(slot["term"]) if r not in STOP_KEYWORDS]
        slot["meanings"].sort(
            key=lambda m: 0 if any(run in m["t"] for run in term_runs) else 1
        )
        slot["meanings"] = slot["meanings"][:MAX_MEANINGS]
        tone = tone_of([m["t"] for m in slot["meanings"]], slot["orig"])
        # Row: [displayTerm, tone, languages, originalTerms, shard, index]
        term_rows.append([slot["term"], tone, ",".join(slot["langs"]), slot["orig"][0], shard_id, idx])
        shards[shard_id].append(
            {"m": [m["t"] for m in slot["meanings"]], "src": slot["sources"][:2]}
        )

    lang_counts = {}
    for _, slot in items:
        for lang in slot["langs"]:
            lang_counts[lang] = lang_counts.get(lang, 0) + 1

    index = {
        "v": 1,
        "entry_count": len(term_rows),
        "source_entry_count": data["entry_count"],
        "language_counts": lang_counts,
        "source_count": data.get("source_count"),
        "shard_count": SHARD_COUNT,
        "entries": term_rows,
    }
    index_path = os.path.join(args.outdir, "terms.min.json")
    with open(index_path, "w", encoding="utf-8") as fh:
        json.dump(index, fh, ensure_ascii=False, separators=(",", ":"))

    total_shard_bytes = 0
    for shard_id, rows in enumerate(shards):
        path = os.path.join(args.outdir, f"meanings-{shard_id:02d}.min.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(rows, fh, ensure_ascii=False, separators=(",", ":"))
        total_shard_bytes += os.path.getsize(path)

    print(f"entries merged: {len(items)} (from {data['entry_count']}), skipped: {skipped}")
    print(f"terms.min.json: {os.path.getsize(index_path):,} bytes")
    print(f"meaning shards: {SHARD_COUNT} files, {total_shard_bytes:,} bytes total")


if __name__ == "__main__":
    main()
