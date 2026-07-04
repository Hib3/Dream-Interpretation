import csv
import io
import json
import re
import sys
import time
import urllib.error
import urllib.request
import zipfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = ROOT / "data" / "raw"
OUT_PATH = ROOT / "data" / "dream_terms.json"
OUT_MIN_PATH = ROOT / "data" / "dream_terms.min.json"
GUTENBERG_URL = "https://www.gutenberg.org/cache/epub/926/pg926.txt"
HF_DATASET = "samvlad/dream-decoder-dataset"
HF_TERAGRON_DREAM_INTERPRETATION = "teragron/dream_interpretation"
HF_DREAMBOOK_GUANACO = "n3rd0/DreamBook_Guanaco_Format"
HF_ROWS_URL = "https://datasets-server.huggingface.co/rows"
HF_TERAGRON_ALL_JSON_URL = (
    "https://huggingface.co/datasets/teragron/dream_interpretation/resolve/main/"
    "_interpretations_contains_all.json"
)
HF_TOLGADEV_RUYA_CSV_URL = (
    "https://huggingface.co/datasets/tolgadev/ruyatabirleri/resolve/main/ruya.csv"
)
GITHUB_HEARTYEARNING_DREAM_SYMBOLS_URL = (
    "https://raw.githubusercontent.com/ljt-one/dream-symbols-dataset/main/"
    "dream-symbols-dataset.json"
)
GITHUB_AKMM_DREAM_DICTIONARY_URL = (
    "https://raw.githubusercontent.com/akmm-dev/dream-dictionary/main/"
    "DreamDictionary.json"
)
GITHUB_BLAZOR_MYANMAR_DREAM_DETAIL_URL = (
    "https://raw.githubusercontent.com/sannlynnhtun-coding/blazor-dream-dictionary/"
    "master/BlazorWasm.DreamDictionary/wwwroot/data/detail.json"
)
GITHUB_SOMNIUMSAGE_DREAM_DATASET_URL = (
    "https://raw.githubusercontent.com/makalin/SomniumSage/main/dream_dataset.csv"
)
KAGGLE_DREAM_DICTIONARY_URL = (
    "https://www.kaggle.com/api/v1/datasets/download/yuvrajsanghai/dream-dictionary"
)
KAGGLE_DICTIONARY_OF_DREAMS_URL = (
    "https://www.kaggle.com/api/v1/datasets/download/manswad/dictionary-of-dreams"
)
WIKISOURCE_ZHOUGONG_URL = (
    "https://zh.wikisource.org/w/index.php?title=%E5%91%A8%E5%85%AC%E8%A7%A3%E5%A4%A2&action=raw"
)
GUTENBERG_GOLDEN_WHEEL_URL = "https://www.gutenberg.org/cache/epub/60045/pg60045.txt"
GUTENBERG_WITCHES_DREAM_BOOK_URL = "https://www.gutenberg.org/cache/epub/53879/pg53879.txt"
GUTENBERG_FORTUNES_AND_DREAMS_URL = "https://www.gutenberg.org/cache/epub/54774/pg54774.txt"


def fetch_gutenberg_text() -> str:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    raw_path = RAW_DIR / "gutenberg_926_ten_thousand_dreams_interpreted.txt"
    if not raw_path.exists():
        req = urllib.request.Request(
            GUTENBERG_URL,
            headers={"User-Agent": "DreamInterpretationDatasetBuilder/0.1"},
        )
        with urllib.request.urlopen(req, timeout=30) as res:
            raw_path.write_bytes(res.read())
    return raw_path.read_text(encoding="utf-8-sig")


def fetch_url_text(url: str, raw_filename: str) -> str:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    raw_path = RAW_DIR / raw_filename
    if not raw_path.exists():
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "DreamInterpretationDatasetBuilder/0.1"},
        )
        with urllib.request.urlopen(req, timeout=30) as res:
            raw_path.write_bytes(res.read())
    return raw_path.read_text(encoding="utf-8-sig", errors="replace")


def fetch_hf_dream_decoder_rows() -> list[dict]:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    raw_path = RAW_DIR / "hf_samvlad_dream_decoder_dataset_rows.json"
    if raw_path.exists():
        return json.loads(raw_path.read_text(encoding="utf-8"))

    rows = []
    offset = 0
    length = 100
    total = None
    while total is None or offset < total:
        url = (
            f"{HF_ROWS_URL}?dataset={HF_DATASET}&config=default"
            f"&split=train&offset={offset}&length={length}"
        )
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "DreamInterpretationDatasetBuilder/0.1"},
        )
        with urllib.request.urlopen(req, timeout=30) as res:
            payload = json.load(res)
        total = int(payload["num_rows_total"])
        rows.extend(item["row"] for item in payload["rows"])
        offset += length

    raw_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    return rows


def fetch_hf_rows(dataset: str, raw_filename: str) -> list[dict]:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    raw_path = RAW_DIR / raw_filename
    if raw_path.exists():
        return json.loads(raw_path.read_text(encoding="utf-8"))

    rows = []
    offset = 0
    length = 100
    total = None
    while total is None or offset < total:
        url = (
            f"{HF_ROWS_URL}?dataset={dataset}&config=default"
            f"&split=train&offset={offset}&length={length}"
        )
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "DreamInterpretationDatasetBuilder/0.1"},
        )
        for attempt in range(5):
            try:
                with urllib.request.urlopen(req, timeout=30) as res:
                    payload = json.load(res)
                break
            except urllib.error.HTTPError as exc:
                if exc.code != 429 or attempt == 4:
                    raise
                time.sleep(5 * (attempt + 1))
        total = int(payload["num_rows_total"])
        rows.extend(item["row"] for item in payload["rows"])
        offset += length
        time.sleep(0.25)

    raw_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    return rows


def fetch_hf_rows_incremental(
    dataset: str,
    raw_filename: str,
    sleep_seconds: float = 2.0,
) -> list[dict]:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    raw_path = RAW_DIR / raw_filename
    meta_path = raw_path.with_suffix(".meta.json")
    if raw_path.exists() and meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        rows = json.loads(raw_path.read_text(encoding="utf-8"))
        if len(rows) >= int(meta["total"]):
            return rows
    else:
        rows = []
        meta = {"total": None}

    offset = len(rows)
    length = 100
    total = meta["total"]
    while total is None or offset < int(total):
        url = (
            f"{HF_ROWS_URL}?dataset={dataset}&config=default"
            f"&split=train&offset={offset}&length={length}"
        )
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "DreamInterpretationDatasetBuilder/0.1"},
        )
        for attempt in range(8):
            try:
                with urllib.request.urlopen(req, timeout=30) as res:
                    payload = json.load(res)
                break
            except urllib.error.HTTPError as exc:
                if exc.code != 429 or attempt == 7:
                    raise
                time.sleep(30 * (attempt + 1))
        total = int(payload["num_rows_total"])
        rows.extend(item["row"] for item in payload["rows"])
        raw_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        meta_path.write_text(json.dumps({"total": total}, indent=2), encoding="utf-8")
        offset = len(rows)
        time.sleep(sleep_seconds)

    return rows


def fetch_hf_teragron_rows() -> list[dict]:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    raw_path = RAW_DIR / "hf_teragron_dream_interpretation_rows.json"
    if not raw_path.exists():
        req = urllib.request.Request(
            HF_TERAGRON_ALL_JSON_URL,
            headers={"User-Agent": "DreamInterpretationDatasetBuilder/0.1"},
        )
        with urllib.request.urlopen(req, timeout=30) as res:
            raw_path.write_bytes(res.read())
    return json.loads(raw_path.read_text(encoding="utf-8"))


def fetch_hf_tolgadev_ruya_csv() -> str:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    raw_path = RAW_DIR / "hf_tolgadev_ruyatabirleri_ruya.csv"
    if not raw_path.exists():
        req = urllib.request.Request(
            HF_TOLGADEV_RUYA_CSV_URL,
            headers={"User-Agent": "DreamInterpretationDatasetBuilder/0.1"},
        )
        with urllib.request.urlopen(req, timeout=30) as res:
            raw_path.write_bytes(res.read())
    return raw_path.read_text(encoding="utf-8-sig")


def fetch_json_url(url: str, raw_filename: str) -> object:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    raw_path = RAW_DIR / raw_filename
    if not raw_path.exists():
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "DreamInterpretationDatasetBuilder/0.1"},
        )
        with urllib.request.urlopen(req, timeout=30) as res:
            raw_path.write_bytes(res.read())
    return json.loads(raw_path.read_text(encoding="utf-8-sig"))


def fetch_text_url(url: str, raw_filename: str) -> str:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    raw_path = RAW_DIR / raw_filename
    if not raw_path.exists():
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "DreamInterpretationDatasetBuilder/0.1"},
        )
        with urllib.request.urlopen(req, timeout=30) as res:
            raw_path.write_bytes(res.read())
    return raw_path.read_text(encoding="utf-8-sig", errors="replace")


def fetch_kaggle_dream_dictionary_csv() -> str:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    raw_path = RAW_DIR / "kaggle_yuvrajsanghai_dream_dictionary.zip"
    if not raw_path.exists():
        req = urllib.request.Request(
            KAGGLE_DREAM_DICTIONARY_URL,
            headers={"User-Agent": "DreamInterpretationDatasetBuilder/0.1"},
        )
        with urllib.request.urlopen(req, timeout=30) as res:
            raw_path.write_bytes(res.read())

    with zipfile.ZipFile(raw_path) as archive:
        with archive.open("cleaned_dream_interpretations.csv") as csv_file:
            return csv_file.read().decode("utf-8-sig")


def fetch_kaggle_dictionary_of_dreams_csv() -> str:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    raw_path = RAW_DIR / "kaggle_manswad_dictionary_of_dreams.zip"
    if not raw_path.exists():
        req = urllib.request.Request(
            KAGGLE_DICTIONARY_OF_DREAMS_URL,
            headers={"User-Agent": "DreamInterpretationDatasetBuilder/0.1"},
        )
        with urllib.request.urlopen(req, timeout=30) as res:
            raw_path.write_bytes(res.read())

    with zipfile.ZipFile(raw_path) as archive:
        with archive.open("dreams_interpretations.csv") as csv_file:
            return csv_file.read().decode("utf-8-sig")


def fetch_wikisource_zhougong_raw() -> str:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    raw_path = RAW_DIR / "wikisource_zhougong_jiemeng_raw.txt"
    if not raw_path.exists():
        req = urllib.request.Request(
            WIKISOURCE_ZHOUGONG_URL,
            headers={"User-Agent": "DreamInterpretationDatasetBuilder/0.1 (local research)"},
        )
        with urllib.request.urlopen(req, timeout=30) as res:
            raw_path.write_bytes(res.read())
    return raw_path.read_text(encoding="utf-8")


def project_gutenberg_body(text: str) -> str:
    start = "*** START OF THE PROJECT GUTENBERG EBOOK"
    end = "*** END OF THE PROJECT GUTENBERG EBOOK"
    if start in text:
        text = text.split(start, 1)[1]
    if end in text:
        text = text.split(end, 1)[0]
    marker = "_Abandon_."
    if marker in text:
        text = text[text.index(marker) :]
    return text


def normalize_text(value: str) -> str:
    value = value.replace("\r\n", "\n")
    value = re.sub(r"\n{3,}", "\n\n", value)
    value = re.sub(r"[ \t]+", " ", value)
    return value.strip()


def is_heading(line: str) -> bool:
    stripped = line.strip()
    if not stripped or len(stripped) > 80:
        return False
    if stripped.startswith("["):
        return False
    return bool(re.fullmatch(r"_[^_\n]{1,80}_\.", stripped))


def clean_term(heading: str) -> str:
    heading = re.sub(r"\[\d+\]", "", heading)
    return heading.strip().rstrip(".").strip("_").strip()


def parse_gutenberg_entries(text: str) -> list[dict]:
    body = project_gutenberg_body(text)
    lines = body.splitlines()
    entries = []
    current_term = None
    current_lines = []

    for line in lines:
        if is_heading(line):
            if current_term and current_lines:
                meaning = normalize_text("\n".join(current_lines))
                if meaning:
                    entries.append(make_gutenberg_entry(current_term, meaning))
            current_term = clean_term(line)
            current_lines = []
        elif current_term:
            current_lines.append(line)

    if current_term and current_lines:
        meaning = normalize_text("\n".join(current_lines))
        if meaning:
            entries.append(make_gutenberg_entry(current_term, meaning))

    return entries


def make_gutenberg_entry(term: str, meaning: str) -> dict:
    aliases = []
    if " and " in term.lower():
        aliases = [part.strip() for part in re.split(r"\band\b", term, flags=re.I)]
        aliases = [alias for alias in aliases if alias and alias.lower() != term.lower()]

    return {
        "term": term,
        "term_normalized": term.casefold(),
        "language": "en",
        "meaning": meaning,
        "aliases": aliases,
        "source": {
            "name": "Ten Thousand Dreams Interpreted; Or, What's in a Dream",
            "author": "Gustavus Hindman Miller",
            "url": GUTENBERG_URL,
            "license_note": "Project Gutenberg ebook; public-domain status depends on jurisdiction.",
        },
    }


def parse_hf_dream_decoder_entries(rows: list[dict]) -> list[dict]:
    by_symbol = {}
    for row in rows:
        symbols = row.get("symbols") or []
        interpretation = str(row.get("interpretation") or "").strip()
        if not symbols or not interpretation:
            continue
        first_sentence = interpretation.split(". ", 1)[0].strip()
        if first_sentence and not first_sentence.endswith("."):
            first_sentence += "."
        for symbol in symbols:
            term = str(symbol).strip()
            if term and term.casefold() not in by_symbol:
                by_symbol[term.casefold()] = {
                    "term": term.title(),
                    "term_normalized": term.casefold(),
                    "language": "en",
                    "meaning": first_sentence or interpretation,
                    "aliases": [],
                    "source": {
                        "name": "Dream Decoder Synthetic Dataset",
                        "author": "samvlad",
                        "url": "https://huggingface.co/datasets/samvlad/dream-decoder-dataset",
                        "license_note": "MIT License for dataset content.",
                    },
                }
    return list(by_symbol.values())


def parse_hf_teragron_dream_interpretation_entries(rows: list[dict]) -> list[dict]:
    entries = []
    for row in rows:
        term = str(row.get("word") or "").strip()
        meaning = normalize_text(str(row.get("meaning") or ""))
        if not term or not meaning:
            continue
        entries.append(
            {
                "term": term,
                "term_normalized": term.casefold(),
                "language": "tr",
                "meaning": meaning,
                "aliases": [],
                "source": {
                    "name": "Dream Interpretation Turkish Hugging Face Dataset",
                    "author": "teragron",
                    "url": "https://huggingface.co/datasets/teragron/dream_interpretation",
                    "license_note": "MIT License for dataset content.",
                },
            }
        )
    return entries


def parse_hf_tolgadev_ruyatabirleri_entries(csv_text: str) -> list[dict]:
    entries = []
    reader = csv.DictReader(io.StringIO(csv_text))
    for row in reader:
        question = normalize_text(str(row.get("soru") or ""))
        meaning = normalize_text(str(row.get("icerik") or ""))
        if not question or not meaning:
            continue
        term = question
        match = re.search(r"rüyada\s+(.+?)\s+görmek\s+ne anlama gelir", question, re.I)
        if match:
            term = match.group(1).strip()
        entries.append(
            {
                "term": term,
                "term_normalized": term.casefold(),
                "language": "tr",
                "meaning": meaning,
                "aliases": [question] if question.casefold() != term.casefold() else [],
                "source": {
                    "name": "Rüya Tabirleri Hugging Face Dataset",
                    "author": "tolgadev",
                    "url": "https://huggingface.co/datasets/tolgadev/ruyatabirleri",
                    "license_note": "Apache 2.0 License for dataset content.",
                },
            }
        )
    return entries


def parse_github_heartyearning_entries(rows: list[dict]) -> list[dict]:
    entries = []
    for row in rows:
        term = str(row.get("keyword") or "").strip()
        meaning = normalize_text(str(row.get("summary") or ""))
        if not term or not meaning:
            continue
        entries.append(
            {
                "term": term,
                "term_normalized": term.casefold(),
                "language": "en",
                "meaning": meaning,
                "aliases": [str(row.get("slug")).strip()] if row.get("slug") else [],
                "source": {
                    "name": "HeartYearning Dream Symbols Dataset",
                    "author": "ljt-one",
                    "url": "https://github.com/ljt-one/dream-symbols-dataset",
                    "license_note": "No explicit license file found; public GitHub JSON dataset, verify before redistribution.",
                },
            }
        )
    return entries


def parse_github_akmm_dream_dictionary_entries(payload: dict) -> list[dict]:
    entries = []
    for row in payload.get("BlogDetail", []):
        phrase = normalize_text(str(row.get("BlogContent") or ""))
        if not phrase:
            continue
        entries.append(
            {
                "term": phrase,
                "term_normalized": phrase.casefold(),
                "language": "my",
                "meaning": phrase,
                "aliases": [],
                "source": {
                    "name": "Myanmar Dream Dictionary GitHub Dataset",
                    "author": "akmm-dev",
                    "url": "https://github.com/akmm-dev/dream-dictionary",
                    "license_note": "No explicit license file found; public GitHub JSON dataset, verify before redistribution.",
                },
            }
        )
    return entries


def parse_github_blazor_myanmar_dream_entries(rows: list[dict]) -> list[dict]:
    entries = []
    for row in rows:
        phrase = normalize_text(str(row.get("title") or ""))
        if not phrase:
            continue
        entries.append(
            {
                "term": phrase,
                "term_normalized": phrase.casefold(),
                "language": "my",
                "meaning": phrase,
                "aliases": [],
                "source": {
                    "name": "Blazor Myanmar Dream Dictionary GitHub Dataset",
                    "author": "sannlynnhtun-coding",
                    "url": "https://github.com/sannlynnhtun-coding/BlazorWasm.DreamDictionary",
                    "license_note": "No explicit license file found; public GitHub JSON dataset, verify before redistribution.",
                },
            }
        )
    return entries


def parse_github_somniumsage_entries(csv_text: str) -> list[dict]:
    entries = []
    reader = csv.DictReader(io.StringIO(csv_text))
    for row in reader:
        term = normalize_text(str(row.get("dream_text") or ""))
        meaning = normalize_text(str(row.get("interpretation") or ""))
        if not term or not meaning:
            continue
        entries.append(
            {
                "term": term,
                "term_normalized": term.casefold(),
                "language": "en",
                "meaning": meaning,
                "aliases": [],
                "source": {
                    "name": "SomniumSage Dream Dataset",
                    "author": "makalin",
                    "url": "https://github.com/makalin/SomniumSage",
                    "license_note": "README states MIT, but no explicit license file was detected; verify before redistribution.",
                },
            }
        )
    return entries


def parse_hf_dreambook_guanaco_entries(rows: list[dict]) -> list[dict]:
    entries = []
    pattern = re.compile(
        r"### Human:\s*What does it mean if I dream about an?\s+(.+?)\?\s*### Assistant:\s*(.+)",
        re.I | re.S,
    )
    for row in rows:
        text = str(row.get("text") or "").strip()
        match = pattern.match(text)
        if not match:
            continue
        term = match.group(1).strip()
        meaning = normalize_text(match.group(2))
        if not term or not meaning:
            continue
        entries.append(
            {
                "term": term,
                "term_normalized": term.casefold(),
                "language": "en",
                "meaning": meaning,
                "aliases": [],
                "source": {
                    "name": "DreamBook Guanaco Format Hugging Face Dataset",
                    "author": "n3rd0",
                    "url": "https://huggingface.co/datasets/n3rd0/DreamBook_Guanaco_Format",
                    "license_note": "No explicit license tag found in Hugging Face metadata; public dataset, verify before redistribution.",
                },
            }
        )
    return entries


def parse_kaggle_dream_dictionary_entries(csv_text: str) -> list[dict]:
    entries = []
    reader = csv.DictReader(io.StringIO(csv_text))
    for row in reader:
        term = str(row.get("Word") or "").strip()
        meaning = normalize_text(str(row.get("Interpretation") or ""))
        if not term or not meaning:
            continue
        entries.append(
            {
                "term": term,
                "term_normalized": term.casefold(),
                "language": "en",
                "meaning": meaning,
                "aliases": [],
                "source": {
                    "name": "Dream_Dictionary Kaggle Dataset",
                    "author": "yuvrajsanghai",
                    "url": "https://www.kaggle.com/datasets/yuvrajsanghai/dream-dictionary",
                    "license_note": "Kaggle dataset; external paper reports Apache 2.0, verify on Kaggle before redistribution.",
                },
            }
        )
    return entries


def parse_kaggle_dictionary_of_dreams_entries(csv_text: str) -> list[dict]:
    entries = []
    reader = csv.DictReader(io.StringIO(csv_text))
    for row in reader:
        term = str(row.get("Dream Symbol") or "").strip()
        meaning = normalize_text(str(row.get("Interpretation") or ""))
        if not term or not meaning:
            continue
        entries.append(
            {
                "term": term,
                "term_normalized": term.casefold(),
                "language": "en",
                "meaning": meaning,
                "aliases": [],
                "source": {
                    "name": "Dictionary of Dreams Kaggle Dataset",
                    "author": "manswad",
                    "url": "https://www.kaggle.com/datasets/manswad/dictionary-of-dreams",
                    "license_note": "Kaggle dataset; external paper reports Apache 2.0, verify on Kaggle before redistribution.",
                },
            }
        )
    return entries


def parse_wikisource_zhougong_entries(raw_text: str) -> list[dict]:
    entries = []
    for line in raw_text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("{{") or stripped.startswith("=="):
            continue
        if stripped in {"詩曰"}:
            continue
        phrases = [part.strip() for part in re.split(r"[\u3000]{1,}|\s{2,}", stripped)]
        for phrase in phrases:
            if not phrase or len(phrase) < 5:
                continue
            if re.search(r"[A-Za-z{}[\]|=#]", phrase):
                continue
            entries.append(
                {
                    "term": phrase,
                    "term_normalized": phrase.casefold(),
                    "language": "zh-Hant",
                    "meaning": phrase,
                    "aliases": [],
                    "source": {
                        "name": "Wikisource 周公解夢",
                        "author": "周公旦",
                        "url": "https://zh.wikisource.org/wiki/%E5%91%A8%E5%85%AC%E8%A7%A3%E5%A4%A2",
                        "license_note": "Wikisource text; CC BY-SA terms apply to Wikisource contributions.",
                    },
                }
            )
    return entries


def project_gutenberg_core(text: str) -> str:
    start = "*** START OF THE PROJECT GUTENBERG EBOOK"
    end = "*** END OF THE PROJECT GUTENBERG EBOOK"
    if start in text:
        text = text.split(start, 1)[1]
    if end in text:
        text = text.split(end, 1)[0]
    return text


def make_project_gutenberg_entry(
    term: str,
    meaning: str,
    source_name: str,
    author: str,
    url: str,
) -> dict:
    return {
        "term": term.strip(),
        "term_normalized": term.strip().casefold(),
        "language": "en",
        "meaning": normalize_text(meaning),
        "aliases": [],
        "source": {
            "name": source_name,
            "author": author,
            "url": url,
            "license_note": "Project Gutenberg ebook; public-domain status depends on jurisdiction.",
        },
    }


def parse_underscore_dash_entries(
    text: str,
    source_name: str,
    author: str,
    url: str,
    start_marker: str,
    end_marker: str | None = None,
) -> list[dict]:
    core = project_gutenberg_core(text)
    if start_marker in core:
        core = core[core.index(start_marker) :]
    if end_marker and end_marker in core:
        core = core[: core.index(end_marker)]

    pattern = re.compile(r"(?ms)^\s*_([^_\n]{1,80})_[-—]+(.*?)(?=^\s*_[^_\n]{1,80}_[-—]+|\Z)")
    entries = []
    for match in pattern.finditer(core):
        term = match.group(1).strip()
        meaning = normalize_text(match.group(2))
        if term and meaning:
            entries.append(make_project_gutenberg_entry(term, meaning, source_name, author, url))
    return entries


def parse_witches_dream_book_entries(text: str) -> list[dict]:
    core = project_gutenberg_core(text)
    start_marker = "Appended will be found"
    if start_marker in core:
        core = core[core.index(start_marker) :]
    end_marker = "THE MOLES OF THE BODY"
    if end_marker in core:
        core = core[: core.index(end_marker)]

    pattern = re.compile(r"(?ms)=([^=\n]{1,80})=\.?--(.*?)(?==[^=\n]{1,80}=\.?--|\Z)")
    entries = []
    for match in pattern.finditer(core):
        term = match.group(1).strip().rstrip(".")
        meaning = normalize_text(match.group(2))
        if term and meaning:
            entries.append(
                make_project_gutenberg_entry(
                    term,
                    meaning,
                    "The Witches' Dream Book; and Fortune Teller",
                    "A. H. Noe",
                    GUTENBERG_WITCHES_DREAM_BOOK_URL,
                )
            )
    return entries


def parse_golden_wheel_entries(text: str) -> list[dict]:
    core = project_gutenberg_core(text)
    marker = "ALPHABETICAL LIST OF DREAMS"
    if marker in core:
        core = core[core.index(marker) :]
    end_marker = "HOW TO FIND LUCKY NUMBERS"
    if end_marker in core:
        core = core[: core.index(end_marker)]

    pattern = re.compile(r"(?ms)^([A-Z][A-Z' -]{1,60})\. (.*?)(?=^[A-Z][A-Z' -]{1,60}\. |\Z)")
    entries = []
    for match in pattern.finditer(core):
        term = match.group(1).title().strip()
        meaning = normalize_text(match.group(2))
        meaning = re.sub(r"\s+\d+(?:,\s*\d+)*\.$", ".", meaning)
        if term and meaning:
            entries.append(
                make_project_gutenberg_entry(
                    term,
                    meaning,
                    "The Golden Wheel Dream-book and Fortune-teller",
                    "Felix Fontaine",
                    GUTENBERG_GOLDEN_WHEEL_URL,
                )
            )
    return entries


def meaning_key(meaning: str) -> str:
    return re.sub(r"\s+", " ", meaning).strip().casefold()


def clean_entry_term(term: str) -> str:
    return re.sub(r"\s+", " ", str(term or "")).strip()


def merge_entries(entries: list[dict]) -> list[dict]:
    merged = {}
    meaning_keys_by_term = defaultdict(set)
    for entry in entries:
        term = clean_entry_term(entry["term"])
        if not term:
            continue
        if entry.get("language") == "en" and len(term) == 1:
            continue
        key = term.casefold()
        if key not in merged:
            merged[key] = {
                "term": term,
                "term_normalized": key,
                "language": entry["language"],
                "aliases": sorted(set(entry.get("aliases", [])), key=str.casefold),
                "meanings": [],
                "sources": [],
            }

        item = merged[key]
        item["aliases"] = sorted(
            set(item["aliases"]) | set(entry.get("aliases", [])),
            key=str.casefold,
        )

        mkey = meaning_key(entry["meaning"])
        if mkey not in meaning_keys_by_term[key]:
            meaning_keys_by_term[key].add(mkey)
            item["meanings"].append(
                {
                    "text": entry["meaning"],
                    "source_name": entry["source"]["name"],
                    "source_url": entry["source"]["url"],
                    "license_note": entry["source"]["license_note"],
                }
            )

        source_key = (entry["source"]["name"], entry["source"]["url"])
        if source_key not in {(s["name"], s["url"]) for s in item["sources"]}:
            item["sources"].append(
                {
                    "name": entry["source"]["name"],
                    "url": entry["source"]["url"],
                    "license_note": entry["source"]["license_note"],
                }
            )

    result = list(merged.values())
    for item in result:
        item["meaning_count"] = len(item["meanings"])
        item["source_count"] = len(item["sources"])
    return sorted(result, key=lambda item: item["term_normalized"])


def write_output(entries: list[dict]) -> None:
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "entry_count": len(entries),
        "schema": {
            "term": "Dream symbol or keyword.",
            "term_normalized": "Case-folded lookup key.",
            "language": "BCP-47 language code.",
            "meanings": "Deduplicated interpretation texts for the term.",
            "aliases": "Simple derived aliases when present.",
            "sources": "Source metadata for merged entries.",
        },
        "entries": entries,
    }
    OUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    OUT_MIN_PATH.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def main() -> int:
    entries = []
    entries.extend(parse_gutenberg_entries(fetch_gutenberg_text()))
    entries.extend(parse_hf_dream_decoder_entries(fetch_hf_dream_decoder_rows()))
    entries.extend(
        parse_hf_teragron_dream_interpretation_entries(
            fetch_hf_teragron_rows()
        )
    )
    entries.extend(
        parse_hf_dreambook_guanaco_entries(
            fetch_hf_rows_incremental(
                HF_DREAMBOOK_GUANACO,
                "hf_n3rd0_dreambook_guanaco_rows.json",
            )
        )
    )
    entries.extend(parse_hf_tolgadev_ruyatabirleri_entries(fetch_hf_tolgadev_ruya_csv()))
    entries.extend(
        parse_github_heartyearning_entries(
            fetch_json_url(
                GITHUB_HEARTYEARNING_DREAM_SYMBOLS_URL,
                "github_ljt_one_dream_symbols_dataset.json",
            )
        )
    )
    entries.extend(
        parse_github_akmm_dream_dictionary_entries(
            fetch_json_url(
                GITHUB_AKMM_DREAM_DICTIONARY_URL,
                "github_akmm_dev_dream_dictionary.json",
            )
        )
    )
    entries.extend(
        parse_github_blazor_myanmar_dream_entries(
            fetch_json_url(
                GITHUB_BLAZOR_MYANMAR_DREAM_DETAIL_URL,
                "github_sannlynnhtun_blazor_dream_dictionary_detail.json",
            )
        )
    )
    entries.extend(
        parse_github_somniumsage_entries(
            fetch_text_url(
                GITHUB_SOMNIUMSAGE_DREAM_DATASET_URL,
                "github_makalin_somniumsage_dream_dataset.csv",
            )
        )
    )
    entries.extend(parse_kaggle_dream_dictionary_entries(fetch_kaggle_dream_dictionary_csv()))
    entries.extend(parse_kaggle_dictionary_of_dreams_entries(fetch_kaggle_dictionary_of_dreams_csv()))
    entries.extend(parse_wikisource_zhougong_entries(fetch_wikisource_zhougong_raw()))
    entries.extend(
        parse_underscore_dash_entries(
            fetch_url_text(
                GUTENBERG_FORTUNES_AND_DREAMS_URL,
                "gutenberg_54774_fortunes_and_dreams.txt",
            ),
            "Fortunes and Dreams",
            "Astra Cielo",
            GUTENBERG_FORTUNES_AND_DREAMS_URL,
            "Dictionary of Dreams",
            "The Language of Flowers",
        )
    )
    entries.extend(
        parse_witches_dream_book_entries(
            fetch_url_text(
                GUTENBERG_WITCHES_DREAM_BOOK_URL,
                "gutenberg_53879_witches_dream_book.txt",
            )
        )
    )
    entries.extend(
        parse_golden_wheel_entries(
            fetch_url_text(
                GUTENBERG_GOLDEN_WHEEL_URL,
                "gutenberg_60045_golden_wheel_dream_book.txt",
            )
        )
    )
    entries = merge_entries(entries)
    if len(entries) < 100:
        print(f"Too few entries parsed: {len(entries)}", file=sys.stderr)
        return 1
    write_output(entries)
    print(json.dumps({"output": str(OUT_PATH), "entry_count": len(entries)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
