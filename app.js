/*
 * 夢日記占い — 日本語照合エンジン
 *
 * data/ja/terms.min.json  : 語彙インデックス(起動時に読込)
 * data/ja/meanings-NN.json: 意味シャード(必要分のみ遅延取得+アイドル先読み)
 *
 * 照合は「フレーズ一致(かな正規化した部分一致)」と
 * 「キーワード一致(漢字・カタカナ・ラテン文字の連続列)」の二段構え。
 */

const state = {
  rows: [], // { term, tone, langs, orig, shard, idx, phraseFold, kwKanji, kwOther, kwLatin }
  shards: new Map(), // shardId -> rows
  shardCount: 0,
  build: "",
  loaded: false,
  queryToken: 0, // 連打時に古い結果で上書きしないための世代カウンタ
};

const dreamInput = document.querySelector("#dreamInput");
const interpretBtn = document.querySelector("#interpretBtn");
const clearBtn = document.querySelector("#clearBtn");
const sampleBtn = document.querySelector("#sampleBtn");
const dataStatus = document.querySelector("#dataStatus");
const readingText = document.querySelector("#readingText");
const matchesEl = document.querySelector("#matches");
const matchCount = document.querySelector("#matchCount");
const resultCard = document.querySelector("#resultCard");
const matchedBlock = document.querySelector("#matchedBlock");
const termChips = document.querySelector("#termChips");

const samples = [
  "水の中を泳いでいたら、橋の向こうに白い犬がいて、最後は空を飛ぶように逃げた。",
  "高いビルから落ちる夢を見た。途中で大きな鳥に助けられて、海の上をゆっくり飛んだ。",
  "古い家の中で蛇を見つけた。怖かったけれど、蛇は金色に光っていて、逃げずにこちらを見ていた。",
];

const LANG_LABEL = { en: "英語辞書", tr: "トルコ語辞書", "zh-Hant": "中国語辞書", my: "ミャンマー語辞書" };
const TONE_LABEL = { 1: "吉", 0: "中", "-1": "注意" };

const RUN_RE = /[一-鿿々]+|[ァ-ヴー]+|[a-z0-9]+/g;

// ひらがな・カタカナ表記の象徴語を辞書の漢字termへ橋渡しする
// (キーはかな正規化後の表記。文法語と衝突しにくいものだけを載せる)
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
  // 単独では夢の象徴にならない汎用語
  "最後", "中", "上", "下", "前", "後", "時", "事", "者", "方", "分", "回", "向",
]);
const STOP_LATIN = new Set([
  "the", "and", "you", "your", "for", "with", "from", "into", "that", "this",
  "dream", "dreams", "dreaming", "about", "being", "rüyada", "görmek", "gelir",
]);

function normalize(value) {
  // toLocaleLowerCase はロケール解決が走り 58k 行のインデックス構築で
  // 数秒単位の差が出るため toLowerCase を使う
  return String(value || "")
    .normalize("NFKC")
    .toLowerCase()
    .replace(/\s+/g, " ")
    .trim();
}

// カタカナ→ひらがな(表記ゆれ吸収用)
function foldKana(value) {
  return value.replace(/[ァ-ヶ]/g, (ch) =>
    String.fromCharCode(ch.charCodeAt(0) - 0x60)
  );
}

// 以下の *Norm 関数は normalize 済みの文字列を受け取る(58k行の構築を1回の正規化で済ませる)
function phraseKeyNorm(norm) {
  return foldKana(norm).replace(/[\s「」『』()[\]。、.,!?！?・…"']/g, "");
}

function phraseKey(value) {
  return phraseKeyNorm(normalize(value));
}

function extractRunsNorm(norm) {
  return norm.match(RUN_RE) || [];
}

function extractRuns(value) {
  return extractRunsNorm(normalize(value));
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
      const { kwKanji, kwOther, kwLatin } = classifyKeywords(termNorm);
      // 元言語の単語もラテン文字キーワードとして拾う(英語入力の互換)
      for (const run of extractRunsNorm(normalize(orig))) {
        if (isLatin(run) && run.length >= 3 && !STOP_LATIN.has(run) && !kwLatin.includes(run)) {
          kwLatin.push(run);
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
        // ラテン文字だけの語は単語境界つきで照合する("rom"が"from"に紛れないように)
        latinPhrase: /^[a-z0-9 .'-]+$/.test(termNorm) ? termNorm : null,
        kwKanji,
        kwOther,
        kwLatin,
      };
    });
    state.loaded = true;
    dataStatus.textContent = `日本語辞書 ${data.entry_count.toLocaleString()} 語(原典 ${data.source_entry_count.toLocaleString()} 項目 / ${data.source_count} ソースを翻訳・統合)`;
    prefetchShards();
  } catch (error) {
    dataStatus.textContent = "辞書の読み込みに失敗しました";
    readingText.textContent = `data/ja/terms.min.json を確認してください。${error.message}`;
  }
}

async function getShard(shardId) {
  if (state.shards.has(shardId)) return state.shards.get(shardId);
  const id = String(shardId).padStart(2, "0");
  // ビルドIDでURLを変え、辞書更新時に古いキャッシュと混ざらないようにする
  const version = state.build ? `?v=${state.build}` : "";
  const rows = await fetchJson(`data/ja/meanings-${id}.min.json${version}`);
  state.shards.set(shardId, rows);
  return rows;
}

// アイドル時間に全シャードを静かに先読みしておく
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

/* ---------- 照合 ---------- */

// 夢日記のテキストから照合用の文脈(正規化文字列とキーワード集合)を作る
function buildContext(text) {
  const textFold = phraseKey(text);
  if (!textFold) return null;

  const kanjiRuns = [];
  const otherSet = new Set();
  const latinSet = new Set();
  for (const run of extractRuns(text)) {
    if (isKanji(run)) kanjiRuns.push(run);
    else if (isLatin(run)) latinSet.add(run);
    else otherSet.add(foldKana(run));
  }
  // かな表記の象徴語(「いぬ」「ビル」など)を対応する漢字語として補う
  for (const [kana, kanji] of KANA_SYNONYMS) {
    if (textFold.includes(kana)) kanjiRuns.push(kanji);
  }
  // ラテン文字フレーズの単語境界照合用(記号を空白に潰して前後に空白を足す)
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

    // 日本語キーワード(漢字は部分一致、カタカナは3文字以上のみ部分一致)
    let jaMatched = 0;
    let jaTotal = 0;
    for (const kw of row.kwKanji) {
      const w = kw.length * kw.length * 2;
      jaTotal += w;
      if (kanjiRuns.some((run) => run.includes(kw))) {
        jaMatched += w;
      }
    }
    for (const kw of row.kwOther) {
      const w = kw.length * kw.length * 1.2;
      jaTotal += w;
      if (otherSet.has(kw) || (kw.length >= 3 && textFold.includes(kw))) {
        jaMatched += w;
      }
    }

    // ラテン文字キーワード(英語入力など)
    let latinMatched = 0;
    let latinTotal = 0;
    for (const kw of row.kwLatin) {
      const w = kw.length * 1.5;
      latinTotal += w;
      if (latinSet.has(kw)) latinMatched += w;
    }

    // 全キーワード一致なら短い語でも採用、部分一致は6割以上かつ十分な重みを要求
    const jaFull = jaTotal > 0 && jaMatched === jaTotal;
    const jaOk =
      jaTotal > 0 && (jaFull || (jaMatched / jaTotal >= 0.6 && jaMatched >= 6));
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

  // ほぼ同じ語・上位語と重複する語・冗長な質問文形式の語を除いて採用
  const sameKwSet = (a, b) =>
    a.length > 0 &&
    a.length === b.length &&
    a.every((kw) => b.includes(kw));

  const accepted = [];
  for (const item of scored) {
    if (accepted.length >= 12) break;
    const dupe = accepted.some(
      (a) =>
        a.row.phraseFold.includes(item.row.phraseFold) ||
        item.row.phraseFold.includes(a.row.phraseFold) ||
        // 「助けます」「助けて」のような同じ漢字語幹の言い換えは1つに絞る
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
  // 訳文が一文で異常に長い場合は読みやすい長さで切る
  if (out.length > limit * 1.6) out = `${out.slice(0, Math.floor(limit * 1.5))}…`;
  return out.trim();
}

function toneSummary(items) {
  const tones = items.map((it) => (it.tone === undefined ? it.row.tone : it.tone));
  const pos = tones.filter((t) => t === 1).length;
  const neg = tones.filter((t) => t === -1).length;
  if (pos > 0 && neg === 0) {
    return "全体としては、明るい流れを感じさせる夢です。いま気になっていることに一歩踏み出すには、良いタイミングかもしれません。";
  }
  if (neg > 0 && pos === 0) {
    return "全体としては、立ち止まって足もとを確かめるよう促す夢です。無理を重ねず、心と体を休めることを優先してみてください。";
  }
  if (pos > 0 && neg > 0) {
    return "良い流れと注意のサインが入り混じった夢です。焦って結論を出さず、変化の兆しをゆっくり見極めていきましょう。";
  }
  return "大きな吉凶よりも、いまの心の状態を映し出している夢のようです。印象に残った場面を手がかりに、自分の気持ちと向き合ってみてください。";
}

function composeReading(items) {
  // 文章に引用する象徴は、簡潔な語(質問文形式でないもの)を優先する
  const concise = items.filter((it) => it.row.term.length <= 14);
  const pool = concise.length >= 3 ? concise : items;
  const top = pool.slice(0, 5);
  const names = top
    .slice(0, 3)
    .map((it) => `〈${it.row.term}〉`)
    .join("");
  const intro = `今回の夢からは、${names} といった象徴が浮かび上がっています。`;

  const lines = [];
  for (const it of top.slice(0, 4)) {
    const body = it.meanings && it.meanings.length ? firstSentences(it.meanings[0]) : "";
    if (!body) continue;
    const tone = TONE_LABEL[it.tone] ? `【${TONE_LABEL[it.tone]}】` : "";
    lines.push(`✦〈${it.row.term}〉${tone} ${body}`);
  }

  return [intro, lines.join("\n"), toneSummary(top)].filter(Boolean).join("\n\n");
}

/* ---------- 描画 ---------- */

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
      const card = matchesEl.children[i];
      if (!card) return;
      card.scrollIntoView({ block: "center" });
      card.classList.add("flash");
      setTimeout(() => card.classList.remove("flash"), 1200);
    });
    termChips.appendChild(chip);
  });
}

function renderMatches(items) {
  matchCount.textContent = `${items.length}件`;
  matchedBlock.hidden = items.length === 0;
  matchesEl.innerHTML = "";
  items.forEach((it, i) => {
    const meanings = it.meanings || [];
    const sources = it.sources || [];
    const card = document.createElement("article");
    card.className = "match-card reveal";
    card.style.animationDelay = `${Math.min(i * 70, 700)}ms`;
    card.innerHTML = `
      <div class="match-head">
        <div class="match-term"></div>
        <div class="match-lang"></div>
      </div>
      <p class="match-meaning"></p>
      <p class="match-source"></p>
    `;
    card.querySelector(".match-term").textContent = it.row.term;
    card.querySelector(".match-lang").textContent = it.row.langs
      .map((l) => LANG_LABEL[l] || l)
      .join(" / ");
    card.querySelector(".match-meaning").textContent =
      meanings.slice(0, 2).join(" / ") || "(意味データなし)";
    const orig = it.orig || it.row.orig;
    const origNote = orig && orig !== it.row.term ? `原語: ${orig}` : "";
    card.querySelector(".match-source").textContent = [origNote, sources.join(", ")]
      .filter(Boolean)
      .join(" — ");
    matchesEl.appendChild(card);
  });
}

/* ---------- 下書きの自動保存 ---------- */

const DRAFT_KEY = "dreamDiaryDraft";

function saveDraft(value) {
  try {
    if (value) localStorage.setItem(DRAFT_KEY, value);
    else localStorage.removeItem(DRAFT_KEY);
  } catch {
    /* プライベートモードなどで使えない場合は諦める */
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

// 同音異義語(例: ビル=building/bill、飛ぶ=flying/flies)は、
// 夢日記の文脈語と各語義の意味文・原語との重なりでどの語義かを判定する
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
    // 英語入力なら原語(building/bill)そのものと突き合わせる
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

// 各マッチに、文脈に合う語義の意味・トーン・原語・出典を取り付ける
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
    const sense = pickSense(shard ? shard[it.row.idx] : null, ctx);
    it.meanings = sense ? sense.m : [];
    it.tone = sense ? sense.t : it.row.tone;
    it.orig = sense ? sense.o : it.row.orig;
    it.sources = sense ? sense.s : [];
  }
}

async function interpret() {
  const text = dreamInput.value;
  const token = ++state.queryToken;
  if (!text.trim()) {
    readingText.textContent = "夢日記を入力して「占う」を押してください。";
    renderTermChips([]);
    renderMatches([]);
    return;
  }
  if (!state.loaded) {
    readingText.textContent = "辞書を読み込み中です。少し待ってからもう一度押してください。";
    return;
  }

  const ctx = buildContext(text);
  const items = ctx ? findMatches(ctx) : [];
  if (items.length === 0) {
    readingText.textContent =
      "今回の夢は、辞書の象徴と強く一致するものが見つかりませんでした。印象に残った物や人、場所、感情を名詞で具体的に(例:「犬」「海」「古い家」)書き足すと照合しやすくなります。";
    renderTermChips([]);
    renderMatches([]);
    replay(resultCard);
    return;
  }

  interpretBtn.disabled = true;
  readingText.textContent = "夢を読み解いています…";
  try {
    await attachMeanings(items, ctx);
    if (token !== state.queryToken) return; // すでに新しい占いが始まっている
    readingText.textContent = composeReading(items);
    renderTermChips(items);
    renderMatches(items);
  } catch (error) {
    if (token === state.queryToken) {
      readingText.textContent = `意味データの取得に失敗しました。${error.message}`;
    }
  } finally {
    interpretBtn.disabled = false;
  }
  if (token !== state.queryToken) return;
  replay(resultCard);
  resultCard.scrollIntoView({ block: "nearest", behavior: "smooth" });
}

let draftTimer = 0;
dreamInput.addEventListener("input", () => {
  clearTimeout(draftTimer);
  draftTimer = setTimeout(() => saveDraft(dreamInput.value), 300);
});

interpretBtn.addEventListener("click", interpret);
clearBtn.addEventListener("click", () => {
  dreamInput.value = "";
  saveDraft("");
  state.queryToken += 1;
  readingText.textContent = "夢日記を入力して「占う」を押してください。";
  renderTermChips([]);
  renderMatches([]);
  dreamInput.focus();
});
sampleBtn.addEventListener("click", () => {
  const current = samples.shift();
  samples.push(current);
  dreamInput.value = current;
  saveDraft(current);
  interpret();
});

restoreDraft();
loadData();
