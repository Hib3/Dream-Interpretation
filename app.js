/*
 * 夢日記占い — 会話型UI + 日本語照合エンジン
 *
 * ビュー構成:
 *   夢占い   — 占い師との会話形式で夢日記を占う
 *   夢単語辞書 — 58,000語の日本語夢辞書をその場で検索
 *   履歴     — 占いの記録を端末内(localStorage)に保存
 *
 * data/ja/terms.min.json  : 語彙インデックス(起動時に読込)
 * data/ja/meanings-NN.json: 意味シャード(必要分のみ遅延取得+アイドル先読み)
 */

const state = {
  rows: [],
  shards: new Map(),
  shardCount: 0,
  build: "",
  loaded: false,
  queryToken: 0, // 連打時に古い結果で上書きしないための世代カウンタ
};

const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

/* ---------- DOM ---------- */

const $ = (sel) => document.querySelector(sel);

const dataStatus = $("#dataStatus");
const tellerText = $("#tellerText");
const tellerAvatar = document.querySelector(".talk-row.teller .avatar");
const dreamInput = $("#dreamInput");
const interpretBtn = $("#interpretBtn");
const clearBtn = $("#clearBtn");
const sampleBtn = $("#sampleBtn");
const diaryRow = $("#diaryRow");
const diarySaidRow = $("#diarySaidRow");
const diarySaidText = $("#diarySaidText");
const rewriteBtn = $("#rewriteBtn");
const readingRow = $("#readingRow");
const readingText = $("#readingText");
const termChips = $("#termChips");
const savedNote = $("#savedNote");
const matchedFold = $("#matchedFold");
const matchesEl = $("#matches");
const matchCount = $("#matchCount");
const againRow = $("#againRow");
const againBtn = $("#againBtn");
const dictSearch = $("#dictSearch");
const dictSuggest = $("#dictSuggest");
const dictHint = $("#dictHint");
const dictResults = $("#dictResults");
const historyList = $("#historyList");
const historyEmpty = $("#historyEmpty");
const historyClearBtn = $("#historyClearBtn");

const samples = [
  "水の中を泳いでいたら、橋の向こうに白い犬がいて、最後は空を飛ぶように逃げた。",
  "高いビルから落ちる夢を見た。途中で大きな鳥に助けられて、海の上をゆっくり飛んだ。",
  "古い家の中で蛇を見つけた。怖かったけれど、蛇は金色に光っていて、逃げずにこちらを見ていた。",
];

const LANG_LABEL = { en: "英語辞書", tr: "トルコ語辞書", "zh-Hant": "中国語辞書", my: "ミャンマー語辞書" };
const TONE_LABEL = { 1: "吉", 0: "中", "-1": "注意" };

/* ---------- 文字処理 ---------- */

const RUN_RE = /[一-鿿々]+|[ァ-ヴー]+|[a-z0-9]+/g;

// ひらがな・カタカナ表記の象徴語を辞書の漢字termへ橋渡しする
const KANA_SYNONYMS = [
  ["いぬ", "犬"],
  ["ねこ", "猫"],
  ["へび", "蛇"],
  ["くま", "熊"],
  ["とり", "鳥"],
  ["そら", "空"],
  ["くるま", "車"],
  ["おかね", "お金"],
  ["さかな", "魚"],
  ["おばけ", "お化け"],
  ["ゆうれい", "幽霊"],
  ["ひこうき", "飛行機"],
  ["でんしゃ", "電車"],
  ["がっこう", "学校"],
  ["かいだん", "階段"],
  ["とびら", "扉"],
  ["まど", "窓"],
  ["びる", "建物"],
];

const STOP_KW = new Set([
  "夢", "見", "意味", "兆", "暗示", "象徴", "解釈", "占",
  "最後", "中", "上", "下", "前", "後", "時", "事", "者", "方", "分", "回", "向",
]);
const STOP_LATIN = new Set([
  "the", "and", "you", "your", "for", "with", "from", "into", "that", "this",
  "dream", "dreams", "dreaming", "about", "being", "rüyada", "görmek", "gelir",
]);

function normalize(value) {
  return String(value || "")
    .normalize("NFKC")
    .toLowerCase()
    .replace(/\s+/g, " ")
    .trim();
}

function foldKana(value) {
  return value.replace(/[ァ-ヶ]/g, (ch) => String.fromCharCode(ch.charCodeAt(0) - 0x60));
}

function phraseKeyNorm(norm) {
  return foldKana(norm).replace(/[\s「」『』()[\]。、.,!?！?・…"']/g, "");
}

function phraseKey(value) {
  return phraseKeyNorm(normalize(value));
}

function extractRunsNorm(norm) {
  return norm.match(RUN_RE) || [];
}

function isKanji(run) {
  return /^[一-鿿々]+$/.test(run);
}

function isLatin(run) {
  return /^[a-z0-9]+$/.test(run);
}

function classifyKeywords(termNorm) {
  const kwKanji = [];
  const kwOther = [];
  const kwLatin = [];
  for (const run of extractRunsNorm(termNorm)) {
    if (STOP_KW.has(run)) continue;
    if (isKanji(run)) {
      kwKanji.push(run);
    } else if (isLatin(run)) {
      if (run.length >= 3 && !STOP_LATIN.has(run)) kwLatin.push(run);
    } else if (run.length >= 2) {
      kwOther.push(foldKana(run));
    }
  }
  return { kwKanji, kwOther, kwLatin };
}

/* ---------- データ読込 ---------- */

async function fetchJson(path) {
  const response = await fetch(path, { cache: "force-cache" });
  if (!response.ok) throw new Error(`HTTP ${response.status} (${path})`);
  return response.json();
}

async function loadData() {
  try {
    const data = await fetchJson("data/ja/terms.min.json");
    state.shardCount = data.shard_count;
    state.build = data.build || "";
    state.rows = data.entries.map((row) => {
      const [term, tone, langs, orig, shard, idx] = row;
      const termNorm = normalize(term);
      const kws = classifyKeywords(termNorm);
      for (const run of extractRunsNorm(normalize(orig))) {
        if (isLatin(run) && run.length >= 3 && !STOP_LATIN.has(run) && !kws.kwLatin.includes(run)) {
          kws.kwLatin.push(run);
        }
      }
      return {
        term,
        tone,
        langs: langs.split(","),
        orig,
        shard,
        idx,
        phraseFold: phraseKeyNorm(termNorm),
        // ラテン文字だけの語は単語境界つきで照合する
        latinPhrase: /^[a-z0-9 .'-]+$/.test(termNorm) ? termNorm : null,
        ...kws,
      };
    });
    state.loaded = true;
    dataStatus.textContent = `日本語辞書 ${data.entry_count.toLocaleString()} 語(原典 ${data.source_entry_count.toLocaleString()} 項目 / ${data.source_count} ソースを翻訳・統合)`;
    renderDictSuggest();
    prefetchShards();
  } catch (error) {
    dataStatus.textContent = "辞書の読み込みに失敗しました";
    tellerSay(`辞書が開けないようです…。${error.message}`);
  }
}

async function getShard(shardId) {
  if (state.shards.has(shardId)) return state.shards.get(shardId);
  const id = String(shardId).padStart(2, "0");
  const version = state.build ? `?v=${state.build}` : "";
  const rows = await fetchJson(`data/ja/meanings-${id}.min.json${version}`);
  state.shards.set(shardId, rows);
  return rows;
}

function prefetchShards() {
  let next = 0;
  const idle =
    typeof window.requestIdleCallback === "function"
      ? (fn) => window.requestIdleCallback(fn)
      : (fn) => setTimeout(fn, 400);
  const step = () => {
    while (next < state.shardCount && state.shards.has(next)) next += 1;
    if (next >= state.shardCount) return;
    getShard(next)
      .catch(() => {})
      .finally(() => idle(step));
  };
  idle(step);
}

/* ---------- 照合エンジン ---------- */

function buildContext(text) {
  const textFold = phraseKey(text);
  if (!textFold) return null;

  const kanjiRuns = [];
  const otherSet = new Set();
  const latinSet = new Set();
  for (const run of extractRunsNorm(normalize(text))) {
    if (isKanji(run)) kanjiRuns.push(run);
    else if (isLatin(run)) latinSet.add(run);
    else otherSet.add(foldKana(run));
  }
  for (const [kana, kanji] of KANA_SYNONYMS) {
    if (textFold.includes(kana)) kanjiRuns.push(kanji);
  }
  const textPad = ` ${normalize(text).replace(/[^\p{Letter}\p{Number}]+/gu, " ").trim()} `;
  return { textFold, textPad, kanjiRuns, otherSet, latinSet };
}

function findMatches(ctx) {
  const { textFold, textPad, kanjiRuns, otherSet, latinSet } = ctx;

  const scored = [];
  for (const row of state.rows) {
    let score = 0;

    const phraseHit = row.latinPhrase
      ? row.latinPhrase.length >= 3 && textPad.includes(` ${row.latinPhrase} `)
      : row.phraseFold.length >= 3 && textFold.includes(row.phraseFold);
    if (phraseHit) score += 6 + row.phraseFold.length * 2;

    let jaMatched = 0;
    let jaTotal = 0;
    for (const kw of row.kwKanji) {
      const w = kw.length * kw.length * 2;
      jaTotal += w;
      if (kanjiRuns.some((run) => run.includes(kw))) jaMatched += w;
    }
    for (const kw of row.kwOther) {
      const w = kw.length * kw.length * 1.2;
      jaTotal += w;
      if (otherSet.has(kw) || (kw.length >= 3 && textFold.includes(kw))) jaMatched += w;
    }

    let latinMatched = 0;
    let latinTotal = 0;
    for (const kw of row.kwLatin) {
      const w = kw.length * 1.5;
      latinTotal += w;
      if (latinSet.has(kw)) latinMatched += w;
    }

    const jaFull = jaTotal > 0 && jaMatched === jaTotal;
    const jaOk = jaTotal > 0 && (jaFull || (jaMatched / jaTotal >= 0.6 && jaMatched >= 6));
    const latinOk = latinTotal > 0 && latinMatched / latinTotal >= 0.6;
    if (!phraseHit && !jaOk && !latinOk) continue;

    score += (jaOk ? jaMatched : 0) + (latinOk ? latinMatched : 0);
    if (jaFull && row.term.length <= 2) score += 6; // 「犬」「蛇」など単独の象徴を優先
    if (score <= 0) continue;
    scored.push({ row, score, phraseHit, jaFull });
  }

  scored.sort(
    (a, b) =>
      b.score - a.score ||
      b.row.phraseFold.length - a.row.phraseFold.length ||
      a.row.term.length - b.row.term.length
  );

  const sameKwSet = (a, b) => a.length > 0 && a.length === b.length && a.every((kw) => b.includes(kw));

  const accepted = [];
  for (const item of scored) {
    if (accepted.length >= 12) break;
    const dupe = accepted.some(
      (a) =>
        a.row.phraseFold.includes(item.row.phraseFold) ||
        item.row.phraseFold.includes(a.row.phraseFold) ||
        (a.jaFull && item.jaFull && sameKwSet(a.row.kwKanji, item.row.kwKanji))
    );
    if (dupe) continue;
    // 同じ漢字語幹(井戸・結婚など)を共有する語は最大2件まで
    if (item.row.kwKanji.length > 0) {
      const sameStem = accepted.filter((a) =>
        a.row.kwKanji.some((kw) => item.row.kwKanji.includes(kw))
      ).length;
      if (sameStem >= 2) continue;
    }
    accepted.push(item);
  }
  return accepted;
}

// 同音異義語は、夢日記の文脈語と各語義の意味文・原語との重なりで判定する
function pickSense(senses, ctx) {
  if (!Array.isArray(senses) || senses.length === 0) return null;
  if (senses.length === 1) return senses[0];

  let best = senses[0];
  let bestScore = 0;
  for (const sense of senses) {
    const hay = foldKana(normalize(sense.m.join("")));
    let score = 0;
    for (const run of ctx.kanjiRuns) {
      if (!STOP_KW.has(run) && hay.includes(run)) score += run.length * run.length;
    }
    for (const kw of ctx.otherSet) {
      if (kw.length >= 2 && hay.includes(kw)) score += kw.length;
    }
    const orig = normalize(sense.o);
    for (const kw of ctx.latinSet) {
      if (kw.length >= 3 && !STOP_LATIN.has(kw) && orig.includes(kw)) score += kw.length * 2;
    }
    if (score > bestScore) {
      bestScore = score;
      best = sense;
    }
  }
  return best;
}

async function attachMeanings(items, ctx) {
  const shardIds = [...new Set(items.map((it) => it.row.shard))];
  const shards = new Map();
  await Promise.all(
    shardIds.map(async (id) => {
      try {
        shards.set(id, await getShard(id));
      } catch {
        shards.set(id, null);
      }
    })
  );
  for (const it of items) {
    const shard = shards.get(it.row.shard);
    it.senses = shard ? shard[it.row.idx] : null;
    const sense = pickSense(it.senses, ctx);
    it.meanings = sense ? sense.m : [];
    it.tone = sense ? sense.t : it.row.tone;
    it.orig = sense ? sense.o : it.row.orig;
    it.sources = sense ? sense.s : [];
  }
}

/* ---------- 占い文の組み立て ---------- */

function firstSentences(text, limit = 110) {
  const clean = String(text || "").replace(/\s+/g, " ").trim();
  const parts = clean.split(/(?<=[。!?！?])/);
  let out = "";
  for (const part of parts) {
    if (out && out.length + part.length > limit) break;
    out += part;
    if (out.length >= limit * 0.55) break;
  }
  if (!out) out = clean.slice(0, limit);
  if (out.length > limit * 1.6) out = `${out.slice(0, Math.floor(limit * 1.5))}…`;
  return out.trim();
}

function toneSummary(items) {
  const tones = items.map((it) => (it.tone === undefined ? it.row.tone : it.tone));
  const pos = tones.filter((t) => t === 1).length;
  const neg = tones.filter((t) => t === -1).length;
  if (pos > 0 && neg === 0) {
    return "全体としては、明るい流れを感じさせる夢です。いま気になっていることに一歩踏み出すには、良いタイミングかもしれませんね。";
  }
  if (neg > 0 && pos === 0) {
    return "全体としては、立ち止まって足もとを確かめるよう促す夢です。無理を重ねず、心と体を休めることを優先してあげてください。";
  }
  if (pos > 0 && neg > 0) {
    return "良い流れと注意のサインが入り混じった夢ですね。焦って結論を出さず、変化の兆しをゆっくり見極めていきましょう。";
  }
  return "大きな吉凶よりも、いまの心の状態を映し出している夢のようです。印象に残った場面を手がかりに、ご自分の気持ちと向き合ってみてください。";
}

function composeReading(items) {
  const concise = items.filter((it) => it.row.term.length <= 14);
  const pool = concise.length >= 3 ? concise : items;
  const top = pool.slice(0, 5);
  const names = top
    .slice(0, 3)
    .map((it) => `〈${it.row.term}〉`)
    .join("");
  const intro = `……視えましたよ。あなたの夢からは、${names} といった象徴が浮かび上がっています。`;

  const lines = [];
  for (const it of top.slice(0, 4)) {
    const body = it.meanings && it.meanings.length ? firstSentences(it.meanings[0]) : "";
    if (!body) continue;
    const tone = TONE_LABEL[it.tone] ? `【${TONE_LABEL[it.tone]}】` : "";
    lines.push(`✦〈${it.row.term}〉${tone} ${body}`);
  }

  return [intro, lines.join("\n"), toneSummary(top)].filter(Boolean).join("\n\n");
}

const NO_MATCH_MESSAGE =
  "……霧が濃くて、今夜はうまく視えないようです。印象に残った物や人、場所、感情を名詞で具体的に(例:「犬」「海」「古い家」)書き足して、もう一度話してみてください。";

/* ---------- 占い師の語り(タイプライター) ---------- */

function delay(ms) {
  return new Promise((res) => setTimeout(res, ms));
}

let tellerToken = 0;

function tellerSay(text, { speed = 34 } = {}) {
  const token = ++tellerToken;
  tellerText.classList.remove("thinking-dots");
  if (reduceMotion) {
    tellerText.textContent = text;
    return Promise.resolve();
  }
  tellerText.classList.add("typing");
  tellerText.textContent = "";
  return new Promise((resolve) => {
    let i = 0;
    const tick = () => {
      if (token !== tellerToken) return resolve(); // 新しいセリフに割り込まれた
      i += 1;
      tellerText.textContent = text.slice(0, i);
      if (i >= text.length) {
        tellerText.classList.remove("typing");
        return resolve();
      }
      const pause = "。、…!?！?".includes(text[i - 1]) ? 200 : 0;
      setTimeout(tick, speed + pause);
    };
    tick();
  });
}

const GREETINGS = [
  "ようこそ、夜の帳へ。……ゆうべは、どんな夢を見ましたか?覚えているままに、話してみてください。",
  "お待ちしていましたよ。……今夜は、どんな夢の話を聞かせてくれますか?",
  "星がよく視える夜です。……あなたの夢、水晶に映してみましょう。",
];

const RETRY_LINES = [
  "……ええ、聞いていますよ。続きをどうぞ。",
  "もう一度、聞かせてくださいね。",
];

/* ---------- 占いフロー ---------- */

function resetToInput(line) {
  state.queryToken += 1;
  diaryRow.hidden = false;
  diarySaidRow.hidden = true;
  readingRow.hidden = true;
  matchedFold.hidden = true;
  matchedFold.open = false;
  againRow.hidden = true;
  savedNote.hidden = true;
  if (line) tellerSay(line);
  dreamInput.focus();
}

async function interpret() {
  const text = dreamInput.value.trim();
  if (!text) {
    tellerSay("……まだ、夢の話が聞こえません。どんな小さなかけらでも構いませんよ。");
    dreamInput.focus();
    return;
  }
  if (!state.loaded) {
    tellerSay("いま夢の辞書を開いているところです。少しだけ待っていてくださいね……。");
    return;
  }

  const token = ++state.queryToken;

  // あなたの夢を吹き出しとして確定
  diarySaidText.textContent = dreamInput.value;
  diaryRow.hidden = true;
  diarySaidRow.hidden = false;
  replay(diarySaidRow);
  readingRow.hidden = true;
  matchedFold.hidden = true;
  matchedFold.open = false;
  againRow.hidden = true;
  savedNote.hidden = true;

  // 占い中の演出
  interpretBtn.disabled = true;
  tellerAvatar.classList.add("divining");
  const speak = tellerSay("……ふむ。目を閉じて、あなたの夢を辿っています");
  const minWait = delay(reduceMotion ? 0 : 1500);

  try {
    const ctx = buildContext(text);
    const items = ctx ? findMatches(ctx) : [];
    if (items.length > 0) await attachMeanings(items, ctx);
    await speak;
    if (!reduceMotion) tellerText.classList.add("thinking-dots");
    await minWait;
    if (token !== state.queryToken) return;

    tellerText.classList.remove("thinking-dots");
    tellerAvatar.classList.remove("divining");

    if (items.length === 0) {
      readingText.textContent = NO_MATCH_MESSAGE;
      renderTermChips([]);
      matchedFold.hidden = true;
      readingRow.hidden = false;
      replay(readingRow);
      tellerSay("……うーん。");
    } else {
      readingText.textContent = composeReading(items);
      renderTermChips(items);
      renderMatches(matchesEl, items);
      matchCount.textContent = `${items.length}件`;
      matchedFold.hidden = false;
      readingRow.hidden = false;
      replay(readingRow);
      tellerSay("……視えましたよ。");
      saveHistoryEntry(text, items);
      savedNote.hidden = false;
    }
    againRow.hidden = false;
    readingRow.scrollIntoView({ block: "nearest", behavior: reduceMotion ? "auto" : "smooth" });
  } catch (error) {
    if (token === state.queryToken) {
      tellerAvatar.classList.remove("divining");
      tellerText.classList.remove("thinking-dots");
      readingText.textContent = `意味データの取得に失敗しました。${error.message}`;
      readingRow.hidden = false;
      againRow.hidden = false;
    }
  } finally {
    interpretBtn.disabled = false;
  }
}

/* ---------- 描画部品 ---------- */

function replay(el) {
  el.classList.remove("reveal");
  void el.offsetWidth;
  el.classList.add("reveal");
}

function renderTermChips(items) {
  termChips.innerHTML = "";
  termChips.hidden = items.length === 0;
  items.forEach((it, i) => {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "term-chip";
    chip.textContent = it.row.term;
    chip.addEventListener("click", () => {
      matchedFold.open = true;
      const card = matchesEl.children[i];
      if (!card) return;
      card.scrollIntoView({ block: "center" });
      card.classList.add("flash");
      setTimeout(() => card.classList.remove("flash"), 1200);
    });
    termChips.appendChild(chip);
  });
}

function makeMatchCard(it, i) {
  const meanings = it.meanings || [];
  const sources = it.sources || [];
  const card = document.createElement("article");
  card.className = "match-card reveal";
  card.style.animationDelay = `${Math.min(i * 60, 600)}ms`;
  card.innerHTML = `
    <div class="match-head">
      <div class="match-term"></div>
      <div class="match-lang"></div>
    </div>
    <p class="match-meaning"></p>
    <p class="match-source"></p>
  `;
  card.querySelector(".match-term").textContent = it.row.term;
  const toneLabel = TONE_LABEL[it.tone === undefined ? it.row.tone : it.tone];
  card.querySelector(".match-lang").textContent = [
    toneLabel ? `【${toneLabel}】` : "",
    it.row.langs.map((l) => LANG_LABEL[l] || l).join(" / "),
  ]
    .filter(Boolean)
    .join(" ");
  card.querySelector(".match-meaning").textContent =
    meanings.slice(0, 2).join(" / ") || "(意味データなし)";
  const orig = it.orig || it.row.orig;
  const origNote = orig && orig !== it.row.term ? `原語: ${orig}` : "";
  card.querySelector(".match-source").textContent = [origNote, sources.join(", ")]
    .filter(Boolean)
    .join(" — ");
  return card;
}

function renderMatches(container, items) {
  container.innerHTML = "";
  items.forEach((it, i) => container.appendChild(makeMatchCard(it, i)));
}

/* ---------- 履歴(この端末の中だけ) ---------- */

const HISTORY_KEY = "dreamHistory.v1";

function loadHistory() {
  try {
    const parsed = JSON.parse(localStorage.getItem(HISTORY_KEY));
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

function storeHistory(list) {
  try {
    localStorage.setItem(HISTORY_KEY, JSON.stringify(list));
  } catch {
    /* 容量超過などは諦める */
  }
}

function saveHistoryEntry(diary, items) {
  const entry = {
    id: `${Date.now()}-${Math.floor(Math.random() * 1e6)}`,
    ts: new Date().toISOString(),
    diary,
    reading: readingText.textContent,
    matches: items.map((it) => ({
      term: it.row.term,
      tone: it.tone === undefined ? it.row.tone : it.tone,
      langs: it.row.langs,
      orig: it.orig || it.row.orig,
      meaning: (it.meanings || [])[0] || "",
      sources: it.sources || [],
    })),
  };
  const list = loadHistory();
  list.unshift(entry);
  if (list.length > 60) list.length = 60;
  storeHistory(list);
}

function formatDate(iso) {
  try {
    return new Date(iso).toLocaleString("ja-JP", {
      year: "numeric",
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return iso;
  }
}

function toneDigest(matches) {
  const pos = matches.filter((m) => m.tone === 1).length;
  const neg = matches.filter((m) => m.tone === -1).length;
  const parts = [];
  if (pos) parts.push(`吉${pos}`);
  if (neg) parts.push(`注意${neg}`);
  return parts.length ? parts.join(" / ") : "中";
}

function renderHistory() {
  const list = loadHistory();
  historyList.innerHTML = "";
  historyEmpty.hidden = list.length > 0;
  historyClearBtn.hidden = list.length === 0;

  list.forEach((entry, i) => {
    const card = document.createElement("details");
    card.className = "history-card reveal";
    card.style.animationDelay = `${Math.min(i * 50, 400)}ms`;

    const summary = document.createElement("summary");
    summary.innerHTML = `
      <div class="history-date"><span aria-hidden="true">☽</span><span class="d"></span><span class="history-tone"></span></div>
      <p class="history-excerpt"></p>
      <p class="history-terms"></p>
    `;
    summary.querySelector(".d").textContent = formatDate(entry.ts);
    summary.querySelector(".history-tone").textContent = toneDigest(entry.matches || []);
    summary.querySelector(".history-excerpt").textContent = entry.diary;
    summary.querySelector(".history-terms").textContent = (entry.matches || [])
      .slice(0, 5)
      .map((m) => `〈${m.term}〉`)
      .join(" ");

    const body = document.createElement("div");
    body.className = "history-body";
    const diaryP = document.createElement("p");
    diaryP.className = "history-diary";
    diaryP.textContent = entry.diary;
    const readingP = document.createElement("p");
    readingP.className = "history-reading";
    readingP.textContent = entry.reading;
    const actions = document.createElement("div");
    actions.className = "history-actions";
    const delBtn = document.createElement("button");
    delBtn.type = "button";
    delBtn.className = "ghost-btn danger";
    delBtn.textContent = "この記録を消す";
    delBtn.addEventListener("click", () => {
      storeHistory(loadHistory().filter((e) => e.id !== entry.id));
      renderHistory();
    });
    actions.appendChild(delBtn);
    body.append(diaryP, readingP, actions);

    card.append(summary, body);
    historyList.appendChild(card);
  });
}

historyClearBtn.addEventListener("click", () => {
  if (window.confirm("履歴をすべて消しますか?この操作は戻せません。")) {
    storeHistory([]);
    renderHistory();
  }
});

/* ---------- 夢単語辞書ビュー ---------- */

function searchDictionary(query) {
  const q = normalize(query);
  const qFold = phraseKeyNorm(q);
  if (!qFold) return [];
  const results = [];
  for (const row of state.rows) {
    let rank = 0;
    if (row.phraseFold === qFold) rank = 4;
    else if (row.phraseFold.startsWith(qFold)) rank = 3;
    else if (row.phraseFold.includes(qFold)) rank = 2;
    else if (row.latinPhrase && row.latinPhrase.includes(q)) rank = 1;
    else if (normalize(row.orig).includes(q) && q.length >= 3) rank = 1;
    if (rank > 0) results.push({ row, rank });
    if (results.length >= 400) break;
  }
  results.sort(
    (a, b) => b.rank - a.rank || a.row.term.length - b.row.term.length
  );
  return results.slice(0, 20);
}

async function renderDictResults(query) {
  const items = searchDictionary(query);
  if (items.length === 0) {
    dictHint.textContent = "見つかりませんでした。別の言い方や、短い単語で試してみてください。";
    dictResults.innerHTML = "";
    return;
  }
  dictHint.textContent = `${items.length}件がひらめきました`;
  const ctx = buildContext(query) || { kanjiRuns: [], otherSet: new Set(), latinSet: new Set() };
  await attachMeanings(items, ctx);
  // 検索が続けて起きた場合は最後の結果だけ描く
  if (normalize(dictSearch.value) !== normalize(query)) return;
  renderMatches(dictResults, items);
}

function renderDictSuggest() {
  if (!state.loaded || dictSuggest.childElementCount > 0) return;
  const shortRows = [];
  // 適度に散らした位置から短い語を拾う(毎回同じにならないように)
  const start = Math.floor(Math.random() * state.rows.length);
  for (let i = 0; i < state.rows.length && shortRows.length < 8; i += 997) {
    const row = state.rows[(start + i) % state.rows.length];
    if (row.term.length >= 1 && row.term.length <= 4 && !/[a-z]/.test(row.term)) {
      shortRows.push(row);
    }
  }
  for (const row of shortRows) {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "term-chip";
    chip.textContent = row.term;
    chip.addEventListener("click", () => {
      dictSearch.value = row.term;
      renderDictResults(row.term);
    });
    dictSuggest.appendChild(chip);
  }
  dictHint.textContent = "単語をえらぶか、検索してみてください。";
}

let dictTimer = 0;
dictSearch.addEventListener("input", () => {
  clearTimeout(dictTimer);
  const value = dictSearch.value;
  dictTimer = setTimeout(() => {
    if (!value.trim()) {
      dictResults.innerHTML = "";
      dictHint.textContent = "単語をえらぶか、検索してみてください。";
      return;
    }
    renderDictResults(value);
  }, 220);
});

/* ---------- ビュー切替 ---------- */

const tabs = [...document.querySelectorAll(".tab")];
const views = {
  fortune: $("#view-fortune"),
  dictionary: $("#view-dictionary"),
  history: $("#view-history"),
};

function switchView(name) {
  if (!views[name]) name = "fortune";
  for (const tab of tabs) {
    const active = tab.dataset.view === name;
    tab.classList.toggle("active", active);
    tab.setAttribute("aria-selected", String(active));
  }
  for (const [key, el] of Object.entries(views)) {
    el.hidden = key !== name;
    el.classList.toggle("active", key === name);
    if (key === name) replay(el);
  }
  if (name === "history") renderHistory();
  if (name === "dictionary") renderDictSuggest();
  if (history.replaceState) history.replaceState(null, "", `#${name}`);
}

for (const tab of tabs) {
  tab.addEventListener("click", () => switchView(tab.dataset.view));
}

/* ---------- 下書きの自動保存 ---------- */

const DRAFT_KEY = "dreamDiaryDraft";

function saveDraft(value) {
  try {
    if (value) localStorage.setItem(DRAFT_KEY, value);
    else localStorage.removeItem(DRAFT_KEY);
  } catch {
    /* noop */
  }
}

function restoreDraft() {
  try {
    const saved = localStorage.getItem(DRAFT_KEY);
    if (saved && !dreamInput.value) dreamInput.value = saved;
  } catch {
    /* noop */
  }
}

let draftTimer = 0;
dreamInput.addEventListener("input", () => {
  clearTimeout(draftTimer);
  draftTimer = setTimeout(() => saveDraft(dreamInput.value), 300);
});

/* ---------- イベント ---------- */

interpretBtn.addEventListener("click", interpret);

clearBtn.addEventListener("click", () => {
  dreamInput.value = "";
  saveDraft("");
  dreamInput.focus();
});

sampleBtn.addEventListener("click", () => {
  const current = samples.shift();
  samples.push(current);
  dreamInput.value = current;
  saveDraft(current);
});

rewriteBtn.addEventListener("click", () => {
  resetToInput(RETRY_LINES[1]);
});

againBtn.addEventListener("click", () => {
  resetToInput(RETRY_LINES[0]);
});

/* ---------- PWA ---------- */

if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("sw.js").catch(() => {});
  });
}

/* ---------- 起動 ---------- */

restoreDraft();
const initialView = location.hash.replace("#", "");
if (initialView && views[initialView]) switchView(initialView);
tellerSay(GREETINGS[Math.floor(Math.random() * GREETINGS.length)]);
loadData();
