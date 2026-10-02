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
  "私", "彼", "彼女", // 人称は夢の象徴として扱わない
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

async function fetchJson(path, cacheMode = "force-cache") {
  const response = await fetch(path, { cache: cacheMode });
  if (!response.ok) throw new Error(`HTTP ${response.status} (${path})`);
  return response.json();
}

async function loadData() {
  try {
    // 語彙インデックスはURLに版が乗らないため、HTTPの再検証(ETag)で更新を拾う
    const data = await fetchJson("data/ja/terms.min.json", "no-cache");
    state.shardCount = data.shard_count;
    state.build = data.build || "";
    state.rows = data.entries.map((row) => {
      let [term, tone, langs, orig, shard, idx] = row;
      // 本文がナッツを指す語を、動詞「食べた」に誤訳した既知の見出しを補正する。
      if (term === "食べた" && orig === "yemiş") term = "ナッツ";
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

/* ---------- 照合エンジン ---------- */

const WORD_SEGMENTER = typeof Intl.Segmenter === "function"
  ? new Intl.Segmenter("ja", { granularity: "word" }) : null;
const NARRATIVE_WORDS = new Set(["見る", "見た", "眺め", "会う", "怖い", "安心する", "穏やかな", "最後", "夢"]);
const EVENT_RULES = [
  ["追われている", /追いかけられ|追われ|追い回され|追って(?:き|く)/gu],
  ["落ちる", /落ち|落下|転落/gu, /歯.{0,8}(?:落ち|抜け)|落ち着|恋に落ち|雨.{0,5}落ち/u],
  ["飛ぶ", /飛ん|飛ぶ|浮遊/gu, /飛ぶよう|飛ぶみたい|飛行機|鳥.{0,8}飛/u],
  ["泳ぐ", /泳い|泳ぐ|泳ぎ/gu],
  ["溺れる", /溺れ|おぼれ/gu],
  ["歯が抜ける", /歯.{0,8}(?:抜け|ぬけ|欠け|折れ)/gu],
  ["食べる", /食べ|食事をし/gu],
  ["変身", /変身|(?:犬|猫|鳥|魚|蛇|動物|別人)(?:の姿)?に(?:なっ|なる|変わ)/gu],
];

function isAsserted(text, index, length) {
  const after = text.slice(index + length);
  if (/^(?:る|た|だ|ている|でいる|ていた|でいた)?(?:予定|つもり|かもしれ|はず|そうにな|そうだった|ようと思|ことを考|のを想像|ことを想像|なら|という言葉|という文字)/u.test(after)) return false;
  if (/^(?:たい|たかった|たくな|てみたい|でみたい|[」』](?:という|と書))/u.test(after)) return false;
  if (/もし/u.test(text.slice(0, index)) && /^(?:るなら|たら|だら|なら|れば|ば)/u.test(after)) return false;
  if (/^(?:が|は|を)なくな/u.test(after)) return true; // なくなる出来事と、存在の否定を区別する
  if (/^(?:[はがもをに]|て|で|い|く|れ|ら|じゃ)*(?:ない|なかった|なく|ず|ません|しない|しなかった)/u.test(after)) return false;
  if (/^(?:を|は)?(?:見|見て|見えて|出て)(?:い|こ)?(?:ない|なかった|ず)/u.test(after)) return false;
  if (/^(?:る|た|だ)?(?:ように|みたいに|わけではない|かどうか)/u.test(after)) return false;
  return !/(?:not|never|no)\s+$/iu.test(text.slice(0, index));
}

function splitNarrativeClauses(text) {
  // 読点の後に新しい主題・主語がある場合だけ分け、主語と目的語の間の読点は残す。
  return text.split(/[。！？!?\n]|(?:けれど|けど|しかし)|(?<=た)が[、，]?|(?=現実では|普段は|夢では|夢の中では)/u)
    .flatMap((sentence) => sentence.replace(/[、，]([^、，]*)/gu, (whole, tail) => {
      if (!WORD_SEGMENTER) return whole;
      const tokens = [...WORD_SEGMENTER.segment(tail)].filter((s) => s.isWordLike);
      const newSubject = tokens.some((token, i) => token.index < 16 && i > 0 &&
        ["が", "は", "も"].includes(token.segment));
      return newSubject ? "\n" + tail : whole;
    }).split("\n")).filter((s) => s.trim());
}

function narrativeScenes(text) {
  let reality = false;
  const units = [];
  for (const raw of splitNarrativeClauses(text)) {
    // 主語を省いた短い感情文だけを直前へ結び付ける。場面転換や別の人物には持ち越さない。
    if (units.length && /^(?:[、\s]*)(?:とても|すごく|非常に|少し|ちょっと|あまり|全然)?(?:怖(?:かった|くなかった)|嬉しかった|うれしかった|楽しかった|悲しかった|苦しかった|安心した|ほっとした)[、\s]*$/u.test(raw)) {
      units[units.length - 1] += "。" + raw;
    } else units.push(raw);
  }
  return units.flatMap((raw, id) => {
    if (/現実では|普段は|起きてから/u.test(raw)) reality = true;
    if (/夢では|夢の中/u.test(raw)) reality = false;
    if (reality) return [];
    // 予定だけを述べた節の場所・対象も、実際に登場したものと決め付けない。
    if (/(?:予定|つもり)(?:だった|でした|です|だ)?$/u.test(raw.trim())) return [];
    const folded = phraseKey(raw);
    const bounds = new Set([0, folded.length]);
    if (WORD_SEGMENTER) for (const s of WORD_SEGMENTER.segment(folded)) {
      bounds.add(s.index); bounds.add(s.index + s.segment.length);
    }
    const events = new Set();
    for (const [term, pattern, exclude] of EVENT_RULES) {
      if (exclude?.test(raw)) continue;
      if ([...raw.matchAll(pattern)].some((m) => isAsserted(raw, m.index, m[0].length))) events.add(term);
    }
    const feelings = [...raw.matchAll(/怖[いくか]|恐ろし|不安|悲し|寂し|安心|ほっと|嬉し|うれし|楽し|楽しか|苦し/gu)]
      .filter((m) => isAsserted(raw, m.index, m[0].length))
      .map((m) => ({ word: m[0], intensity: /とても|すごく|非常に|ひどく|強く/u.test(raw.slice(Math.max(0, m.index - 8), m.index)) ? 2
        : /少し|ちょっと|やや/u.test(raw.slice(Math.max(0, m.index - 8), m.index)) ? 0.5 : 1 }));
    return [{ id, raw, folded, bounds, events, feelings, roles: caseRoles(raw) }];
  });
}

function scenePriority(scene) {
  const emphasized = assertedPattern(scene.raw, /一番|いちばん|特に|印象に残|何度も|繰り返/gu);
  const background = /ちらっと|通りすがり|背景に|気にならな|覚えていない/u.test(scene.raw);
  return (emphasized ? 16 : 0) - (background ? 12 : 0)
    + Math.min(6, scene.feelings.reduce((sum, f) => sum + f.intensity * 3, 0));
}

function sceneMatches(row, scene) {
  if (scene.events.has(row.term)) return true;
  // 動詞の活用と否定は専用ルールに任せ、語幹だけの一致では補わない。
  if (EVENT_RULES.some(([term]) => term === row.term)) return false;
  const variants = [row.phraseFold, ...KANA_SYNONYMS.filter(([, kanji]) => kanji === row.term).map(([kana]) => kana)];
  return variants.some((term) => {
    let at = scene.folded.indexOf(term);
    while (term && at !== -1) {
      if ((!WORD_SEGMENTER || (scene.bounds.has(at) && scene.bounds.has(at + term.length))) && isAsserted(scene.folded, at, term.length)) return true;
      at = scene.folded.indexOf(term, at + 1);
    }
    return false;
  });
}

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
  // かな表記の象徴語を漢字へ橋渡しする。
  // 「浴びる→ビル」「受け取り→とり」のような活用語の一部への誤発火を防ぐため、
  // カタカナ表記(イヌ等)はそのまま採用し、ひらがな表記は直前が
  // ひらがな・漢字以外(文頭・句読点・カタカナの後)の時だけ拾う
  const WORDISH = /[ぁ-ん一-鿿々]/;
  for (const [kana, kanji] of KANA_SYNONYMS) {
    if (otherSet.has(kana)) {
      kanjiRuns.push(kanji);
      continue;
    }
    let idx = textFold.indexOf(kana);
    while (idx !== -1) {
      if (idx === 0 || !WORDISH.test(textFold[idx - 1])) {
        kanjiRuns.push(kanji);
        break;
      }
      idx = textFold.indexOf(kana, idx + 1);
    }
  }
  const textPad = ` ${normalize(text).replace(/[^\p{Letter}\p{Number}]+/gu, " ").trim()} `;
  return { textFold, textPad, kanjiRuns, otherSet, latinSet, scenes: narrativeScenes(text), raw: text };
}

function findMatches(ctx) {
  const scored = [];
  for (const row of state.rows) {
    if (NARRATIVE_WORDS.has(row.term) || STOP_KW.has(row.term) || /^[ぁ-ん]$/u.test(row.term)) continue;
    const scenes = ctx.scenes.filter((scene) => sceneMatches(row, scene));
    if (!scenes.length) continue;
    const event = scenes.some((scene) => scene.events.has(row.term));
    const action = row.term === "橋" && scenes.some((scene) => assertedPattern(scene.raw, /渡っ|渡る/gu));
    const score = 5 + Math.min(row.term.length, 8) + (event ? 12 : 0) + (action ? 6 : 0)
      + Math.max(...scenes.map(scenePriority)) + Math.min(scenes.length - 1, 3);
    scored.push({ row, score, phraseHit: true, jaFull: true, scene: scenes[0].raw, sceneEvidence: scenes.map((s) => s.raw), sceneIds: scenes.map((s) => s.id) });
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
    if (accepted.length >= 24) break; // 本文の条件確認で除外する前に候補を確保する
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
    delete it.grounding;
    const shard = shards.get(it.row.shard);
    it.senses = shard ? shard[it.row.idx] : null;
    const selection = it.scene ? selectContextualSense(it.senses, it.sceneEvidence || [it.scene]) : null;
    const sense = it.scene ? selection?.sense : pickSense(it.senses, ctx);
    it.meanings = sense ? sense.m : [];
    it.tone = sense ? sense.t : it.row.tone;
    it.orig = sense ? sense.o : it.row.orig;
    it.sources = sense ? sense.s : [];
    if (it.scene || REVIEWED_SENSES[it.row.term]) {
      const transformation = EVENT_RULES.find(([term]) => term === "変身")[1];
      const transformed = (it.sceneEvidence || []).some((scene) => [...scene.matchAll(transformation)]
        .some((m) => m[0].includes(it.row.term) && isAsserted(scene, m.index, m[0].length)));
      const reviewed = transformed ? REVIEWED_SENSES["変身"] : REVIEWED_SENSES[it.row.term];
      if (reviewed) {
        it.meanings = [reviewed.text];
        it.orig = reviewed.orig;
        it.sources = [reviewed.source + "（原文確認・日本語要約）"];
        it.tone = 0;
        if (transformed) it.meanings = [`〈${it.row.term}〉は、姿が変わった先として書かれています。この場面は〈変身〉の解釈にまとめています。`];
        if (it.row.term === "水") {
          if (assertedPattern(it.scene || "", /穏やか|静か/gu)) it.meanings = ["穏やかな水は、気持ちの落ち着きや安心感の象徴として読めます。"];
          else if (assertedPattern(it.scene || "", /荒れ|荒波|大波/gu)) it.meanings = ["荒れた水は、感情が大きく揺れたり、抱えきれなくなったりする感覚の象徴として読めます。"];
        }
        it.grounding = { kind: transformed ? "redirect" : "reviewed", text: it.meanings[0], scenes: [...(it.sceneEvidence || [])] };
      } else {
        it.meanings = selection ? [selection.text] : [];
        it.tone = 0; // 語全体の吉凶を、選んだ一場面へ無条件に引き継がない
        if (selection) it.grounding = { kind: "dictionary", text: selection.text, scenes: [selection.scene] };
      }
    }
  }
  // 見出しが一致しても、本文の前提が合わない候補は結果にも根拠一覧にも出さない。
  for (let i = items.length - 1; i >= 0; i--) {
    if (items[i].scene && !items[i].meanings.length) items.splice(i, 1);
  }
}

const REVIEWED_SENSES = {
  "海": {orig:"Sea", source:"Dream_Dictionary Kaggle Dataset", quote:"What is taking place in or around the sea are symbols that relate to your inner feelings.", text:"海が出てくる夢は、まだはっきり意識できていない感情や、自分の内側の状態を映していると読めます。"},
  "変身": {orig:"Transformation", source:"Dream_Dictionary Kaggle Dataset", quote:"What was transforming in your dream? Was the process good or bad?", text:"姿が変わる場面は、新しい始まりや、変化への向き合い方を象徴すると読めます。何に変わったかだけでなく、その姿でどう感じたかも大切です。"},
  "食べる": {orig:"eating", source:"Dream_Dictionary Kaggle Dataset", quote:"The type of food will be metaphoric to what you are fulfilling inside you.", text:"食べる場面には、心の充足や、活力を取り入れることの象徴という読み方があります。何を食べ、どんな感覚がしたかを手がかりにできます。"},
  "友達": {orig:"Friend", source:"Dream_Dictionary Kaggle Dataset", quote:"You may see qualities in them you want to incorporate within yourself.", text:"友人は、その人の中に見ている性格や、自分も大切にしたい一面を振り返る手がかりになります。"},
  "泳ぐ": {orig:"swimming", source:"Dream_Dictionary Kaggle Dataset", quote:"Swimming brings your focus on managing your emotional stability in your life.", text:"泳ぐ場面は、自分の感情とどう付き合い、進んでいくかを考える手がかりになります。楽に泳げたか、苦しかったかによって読み方も変わります。"},
  "追われている": {orig:"Being Chased", source:"HeartYearning Dream Symbols Dataset", quote:"Avoidance of a person, responsibility, or emotion.", text:"追われる場面は、人との関係や責任、向き合いにくい感情から距離を置きたい気持ちの象徴として読めます。"},
  "落ちる": {orig:"Falling", source:"HeartYearning Dream Symbols Dataset", quote:"Insecurity, loss of support, or feeling out of control.", text:"落下する場面には、支えを失う不安や、自分では状況を動かせない感覚を重ねる読み方があります。"},
  "飛ぶ": {orig:"Flying", source:"HeartYearning Dream Symbols Dataset", quote:"Desire for freedom, escape, or a higher perspective on life.", text:"空を飛ぶ場面は、自由になりたい願いや、今いる場所から離れて物事を見直したい気持ちにつながります。"},
  "歯が抜ける": {orig:"Teeth", source:"Dream_Dictionary Kaggle Dataset", quote:"Sometimes it can represent some sort of personal loss or feelings of inadequacy.", text:"歯が抜ける場面には、大切なものを失う不安や、自信の揺らぎを重ねる読み方があります。"},
  "水": {orig:"Water", source:"HeartYearning Dream Symbols Dataset", quote:"Emotional state. Calm water means peace; turbulent water means overwhelmed.", text:"水の様子は、感情を読む手がかりです。穏やかな水は落ち着き、荒れた水は感情を抱えきれない感覚の象徴として読まれます。"},
  "橋": {orig:"Bridge", source:"Dream_Dictionary Kaggle Dataset", quote:"You might be transitioning from one place to another", text:"橋は、人や場所をつなぐことや、今までの段階から次へ移ることの象徴として読めます。"},
  "犬": {orig:"Dogs", source:"Dream_Dictionary Kaggle Dataset", quote:"companionship, loyalty, protectors, guardians", text:"犬には、親しさや信頼、守ってくれる存在の象徴という読み方があります。ただし、犬との関係や行動によって受け止め方は変わります。"},
  "溺れる": {orig:"Drowning", source:"Dream_Dictionary Kaggle Dataset", quote:"feelings of being emotionally overwhelmed, burdened or consumed", text:"溺れる場面は、感情や負担を抱えきれない感覚の象徴として読めます。誰が溺れていたか、助かったかどうかも大切です。"},
};

function assertedPattern(text, pattern) {
  return [...String(text).matchAll(pattern)].some((m) => isAsserted(text, m.index, m[0].length));
}

const PREMISE_ACTIONS = [
  /食べ|食事/gu, /飲[むんみま]|飲酒/gu, /聞[くいきか]|聴[くいきか]/gu,
  /買[うっいわ]|購入/gu, /売[るっりら]|販売/gu, /殺[すしさ]|殺害/gu,
  /噛[むんみま]|咬/gu, /攻撃|襲[うっいわ]/gu, /泳[ぐいぎが]/gu,
  /飛[ぶんびば]/gu, /落ち|落下|転落/gu, /失[うっいわ]|なくし|紛失/gu,
  /壊[すれしさ]|破損/gu, /腐[るっりら]/gu, /泣[くいきか]/gu,
];
const PREMISE_FILLER = new Set(["夢", "中", "自分", "自身", "姿", "人", "あなた", "私", "誰", "何", "場合", "時", "こと", "もの", "それ", "これ", "夢想", "者"]);
const PREMISE_ALIASES = [["パン", "トースト"], ["食事", "食べ"], ...KANA_SYNONYMS.map(([kana, kanji]) => [kana, kanji])];

function caseRoles(text) {
  const roles = new Map();
  if (!WORD_SEGMENTER) return roles;
  const tokens = [...WORD_SEGMENTER.segment(text)];
  for (let i = 1; i < tokens.length; i++) {
    const particle = tokens[i].segment;
    const noun = tokens[i - 1].segment;
    if (!["を", "が", "に"].includes(particle) || !/[一-鿿ァ-ヶ]/u.test(noun) || PREMISE_FILLER.has(noun)) continue;
    const key = phraseKey(noun);
    if (!roles.has(key)) roles.set(key, new Set());
    roles.get(key).add(particle);
  }
  return roles;
}

function premiseSupported(sentence, scene) {
  const clauses = splitNarrativeClauses(scene);
  if (clauses.length > 1) return clauses.some((clause) => premiseSupported(sentence, clause));
  // ponytail: bounded linguistic rules; unknown conditions are withheld, not guessed.
  const end = /ということは|ことは|夢は|姿は|のは|場合|とき|時は|たら|なら|を見ると|見れば|は[、，]?/u.exec(sentence);
  if (!end) return false;
  const premise = sentence.slice(0, end.index).replace(/^夢の中で|^夢で|^夢に/u, "");
  if (!premise || /それ|これ|その|彼|彼女/u.test(premise)) return false;
  const sceneFold = phraseKey(scene);
  for (const qualifier of premise.match(/たくさん|大勢|多く|きれい|とても|必ず|[0-9０-９]+/gu) || []) {
    if (!sceneFold.includes(phraseKey(qualifier))) return false;
  }
  let nouns = premise;
  let hasAction = false;
  for (const action of PREMISE_ACTIONS) {
    const required = [...premise.matchAll(action)];
    if (required.length) {
      hasAction = true;
      const positive = required.some((m) => isAsserted(premise, m.index, m[0].length));
      const actual = [...scene.matchAll(action)];
      if (!actual.some((m) => isAsserted(scene, m.index, m[0].length) === positive)) return false;
      nouns = nouns.replace(action, " ");
    }
  }
  if (hasAction) {
    const actualRoles = caseRoles(scene);
    for (const [noun, roles] of caseRoles(premise)) {
      const actual = actualRoles.get(noun);
      if (actual && ![...roles].some((role) => actual.has(role))) return false;
    }
  }
  // 「見る」は対象の存在だけを指す一般条件。その他の動作は上で明示的に照合する。
  nouns = nouns.replace(/見[るたてえい]|する|した|して|いる|いた|いう|なった/gu, " ");
  const tokens = WORD_SEGMENTER ? [...WORD_SEGMENTER.segment(nouns)].filter((s) => s.isWordLike).map((s) => s.segment) : extractRunsNorm(normalize(nouns));
  const facts = tokens.filter((word) => /[一-鿿ァ-ヶ]/u.test(word) && !PREMISE_FILLER.has(word));
  if (!facts.length) return false;
  return facts.every((fact) => {
    const aliases = PREMISE_ALIASES.find((group) => group.includes(fact)) || [fact];
    return aliases.some((word) => {
      const needle = phraseKey(word);
      let at = sceneFold.indexOf(needle);
      while (at >= 0) {
        if (isAsserted(sceneFold, at, needle.length)) return true;
        at = sceneFold.indexOf(needle, at + 1);
      }
      return false;
    });
  });
}

function selectContextualSense(senses, scenes) {
  let best = null;
  for (const sense of senses || []) {
    for (const meaning of sense.m || []) {
      for (const sentence of String(meaning).split(/(?<=[。！？])/u)) {
        const text = sentence.trim();
        if (text.length < 8 || text.length > 180 || !/[。！？]$/u.test(text)) continue;
        if (/現在の課題|善と善|参照|によると|ナブルシ|(.{3,12})、\1、\1/u.test(text)) continue;
        // 複数の予言を一文へ詰め込んだ訳や同語反復は、短く切って正当化せず保留する。
        if ((text.match(/[、，]/gu) || []).length > 3 || /([一-鿿]{2,8})と\1/u.test(text)) continue;
        for (const scene of scenes) {
          if (!premiseSupported(text, scene)) continue;
          const words = extractRunsNorm(normalize(scene)).filter((w) => w.length >= 2 && !STOP_KW.has(w));
          const score = words.reduce((n,w) => n + (text.includes(w) ? w.length : 0), 0);
          if (!best || score > best.score) best = {sense, text, score, scene};
        }
      }
    }
  }
  return best;
}

/* ---------- 占い文の組み立て ---------- */

/* 意味テキストを、完結した文・節の単位で要約する(中途切断はしない)。
 * まとまりが作れない場合は空文字を返し、呼び出し側は引用を諦める。 */
function summarizeMeaning(text, limit = 110) {
  const clean = String(text || "").replace(/\s+/g, " ").trim();
  const parts = clean.split(/(?<=[。!?！?])/).filter((p) => p.trim());
  let out = "";
  for (const part of parts) {
    if (!out && part.length > limit) {
      // 一文目から長すぎる: 節単位で完結する範囲に要約する
      const packed = packClauses(part, limit - 2);
      return packed ? `${packed}。` : "";
    }
    if (out && out.length + part.length > limit) break;
    out += part;
    if (out.length >= limit * 0.55) break;
  }
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

/* 象徴の意味テキストからテーマと吉凶を読み取り、統合した占い文を織り上げる。
 * 辞書の原文は「該当する単語の辞書」に委ね、ここでは転載しない。 */

const THEMES = [
  {
    key: "love",
    label: "恋愛・絆",
    kws: ["愛", "恋", "結婚", "恋人", "パートナー", "出会い", "絆", "ロマン", "異性", "縁", "花嫁", "花婿"],
    pos: "素直なひとことが、思いのほか遠くまで届くでしょう。",
    neg: "言葉を惜しまないことが、いちばんの守りになります。",
  },
  {
    key: "work",
    label: "仕事・挑戦",
    kws: ["仕事", "職", "キャリア", "昇進", "事業", "努力", "成功", "達成", "目標", "挑戦", "勉強", "試験", "計画"],
    pos: "温めている計画は、動かして良い頃合いです。",
    neg: "焦らず、手順をひとつずつ確かめてください。",
  },
  {
    key: "money",
    label: "金運",
    kws: ["お金", "金銭", "財", "富", "利益", "収入", "繁栄", "損失", "貧", "豊か", "宝"],
    pos: "思わぬところから、小さな豊かさが舞い込みそうです。",
    neg: "大きな決断は、少しだけ寝かせるのが吉です。",
  },
  {
    key: "social",
    label: "人とのあいだ",
    kws: ["友人", "友達", "人間関係", "信頼", "仲間", "家族", "敵", "裏切", "嫉妬", "悪意", "援助", "助け", "周囲", "中傷", "親戚"],
    pos: "頼ることは、弱さではありませんよ。",
    neg: "噂ではなく、本人の言葉を確かめてください。",
  },
  {
    key: "health",
    label: "心と体",
    kws: ["健康", "病", "体", "疲れ", "回復", "癒し", "休息", "ストレス", "心配", "不安", "安らぎ", "眠"],
    pos: "眠りを大切にすれば、回復はさらに速まります。",
    neg: "予定をひとつ減らす勇気を持ってください。",
  },
  {
    key: "change",
    label: "変化・転機",
    kws: ["変化", "転機", "移行", "新しい", "始ま", "終わ", "旅", "別れ", "再会", "チャンス", "運命", "知らせ", "扉", "道"],
    pos: "この変化は、あなたの味方です。",
    neg: "急がなくても、季節は必ず移ろいますから。",
  },
];

const THEME_NEUTRAL = "二、三日、心の温度を観察してみてください。";

/* 節が意味的に完結しているか(接続形・助詞で終わっていないか) */
function clauseComplete(text) {
  if (/(こと|もの|ため|とき|よう|さま)$/.test(text)) return true;
  if (/[してにをがはでとやのへ、か]$/.test(text)) return false;
  // 「〜になり」「〜され」など連用形で終わる節は文が続いてしまう
  if (/(なり|あり|おり|され|でき|しまい|ており|に入り)$/.test(text)) return false;
  return true;
}

// 列挙文の確実な区切り(「〜こと」「〜ます」等)。これがある文はここまで戻す
const STRONG_END = /(こと|です|ます|でしょう|ください|しれません)$/;

/* 長い文を「、」区切りの節単位で要約する。
 * 完結する節だけを頭から詰め、途中で切れる節は丸ごと捨てる(中途切断をしない)。
 * 完結単位が作れなければ空文字を返し、呼び出し側は引用自体を諦める。 */
function packClauses(sentence, limit) {
  const segs = String(sentence || "")
    .replace(/[。!?！?]$/, "")
    .split("、")
    .map((s) => s.trim())
    .filter(Boolean);
  let out = "";
  for (const seg of segs) {
    const candidate = out ? `${out}、${seg}` : seg;
    if (candidate.length > limit) break;
    out = candidate;
  }
  // 「〜こと、〜こと、…」の列挙文なら、確実な区切りまで戻す
  if (segs.some((seg) => STRONG_END.test(seg))) {
    while (out && !STRONG_END.test(out)) {
      const pos = out.lastIndexOf("、");
      out = pos === -1 ? "" : out.slice(0, pos);
    }
  }
  while (out && !clauseComplete(out)) {
    const pos = out.lastIndexOf("、");
    out = pos === -1 ? "" : out.slice(0, pos);
  }
  return out.length >= 8 ? out : "";
}

/* 辞書の意味文から「結果句」だけを抜き出す(内容は辞書由来のまま文体を織り直すため)。
 * 例:「夢の中で犬を見ることは、忠実な友人を意味します」→「忠実な友人」 */
function extractEssence(text) {
  const sentences = String(text || "").replace(/\s+/g, "").split(/(?<=[。!?！?])/).slice(0, 3);
  for (const sentence of sentences) {
    let core = sentence.replace(/[。!?！?]$/, "");
    // 「夢の中で〜は、」などの前置きを外す
    const lead = core.match(/^.{0,42}?(?:ことは|場合(?:は|、)|のは、?|は、|なら、?)(.+)$/);
    if (lead && lead[1].length >= 6) core = lead[1];
    core = core.replace(/^(それは|これは|あなたが|あなたの)/, "");
    // 「〜を意味します」などの定型の尻尾を外す
    const tail = core.match(
      /^(.*?)(?:こと|の)?(?:を意味します|を意味する|を示しています|を示します|を表しています|を表します|と解釈されます|とされています|と言われています|の(?:兆し|兆候|前兆|しるし)です|を告げています|につながります)$/
    );
    if (tail && tail[1] && tail[1].length >= 2) core = tail[1];
    core = core
      .replace(/[「」『』()（）]/g, "")
      .replace(/^[、。・]+/, "")
      .replace(/[、。]$/, "");
    // 話者注記や相互参照は結果句ではないので次の文を試す
    if (/(言いました|言われました|によると|曰く|参照)/.test(core)) continue;
    if (core.length >= 6 && core.length <= 55 && clauseComplete(core)) return core;
    if (core.length > 55) {
      // 中途で切らず、完結する節だけを残して要約。できなければ次の文へ
      const packed = packClauses(core, 48);
      if (packed) return packed;
    }
  }
  return "";
}

/* 象徴の組み合わせに対する読み(一般的な夢象徴の定石)。1回の占いで最大1行 */
const PAIR_RULES = [
  [["水", "海", "泳"], ["犬"], "水は心の流れ、犬は身近な信頼の象徴とされます。感情の波の中でも、そばで支えてくれる存在がいるようです。"],
  [["落ち"], ["飛"], "「落ちる」不安と「飛ぶ」解放が同じ夜に同居しています。何かを手放すことへの怖れと憧れが、いま揺れているのでしょう。"],
  [["蛇"], ["金", "光"], "蛇は変化と再生、金色の光は価値あるものの気配とされます。怖れの中にこそ、大切な転機が隠れているようです。"],
  [["追いかけ", "追わ"], ["逃げ"], "追われて逃げる夢は、向き合うことを先送りしている何かの合図と読まれてきました。逃げた方角に、その正体のヒントがあります。"],
  [["古い家", "古い建物"], ["蛇", "虫", "影"], "古い家はあなた自身の内面、そこに現れるものは長く目を向けていなかった感情とされます。掃除のつもりで、少し心の棚卸しを。"],
  [["結婚", "指輪", "花嫁"], ["黒", "失く", "失う", "壊れ"], "誓いの象徴に影が差すのは、関係そのものより「うまくやらねば」という気負いの表れとされることが多いのです。"],
  [["空"], ["飛"], "空を飛ぶ夢は、束縛からの解放や視野の広がりを映すとされます。着地の場面まで覚えていたら、それが次の目的地です。"],
  [["歯"], ["抜け", "折れ"], "歯が欠ける夢は、自信や言葉にまつわる小さな不安の表れとされます。大事な話は、急がず整えてから。"],
  [["水", "海", "川"], ["月", "星"], "水面に映る光は、揺れる感情の中にも確かな指針があることのしるしとされます。"],
  [["死", "亡くな"], ["生", "赤ちゃん", "誕生"], "死と誕生が並ぶ夢は、終わりではなく入れ替わりの象徴とされます。一区切りの先に、新しい始まりが待っています。"],
  [["階段", "登る", "上る"], ["落ち", "降り"], "昇り降りの夢は、目標との距離の測り直しとされます。一段抜かしではなく、一段ずつで大丈夫。"],
  [["雨", "嵐", "雷"], ["家", "屋根"], "荒れる空と家の組み合わせは、外のざわめきから内側を守ろうとする心の働きとされます。"],
];

function findPairInsight(items, ctx) {
  const hay = ctx.textFold + items.map((it) => it.row.term).join("");
  for (const [groupA, groupB, insight] of PAIR_RULES) {
    if (groupA.some((k) => hay.includes(k)) && groupB.some((k) => hay.includes(k))) {
      return insight;
    }
  }
  return "";
}

const POS_WORDS = ["吉", "幸運", "幸せ", "成功", "繁栄", "喜び", "順調", "達成", "利益", "豊か", "昇進", "健康", "平和", "安心", "祝福", "満足", "発展", "勝利", "良い", "希望", "恵まれ", "報われ"];
const NEG_WORDS = ["凶", "不吉", "警告", "注意", "不安", "失敗", "病気", "トラブル", "危険", "損失", "悪い", "困難", "裏切り", "別れ", "苦し", "災い", "悩み", "対立", "喪失", "孤独", "悪意", "死", "不運", "不幸"];

function analyzeThemes(items) {
  const agg = new Map();
  for (const it of items) {
    const text = (it.meanings || []).join("");
    if (!text) continue;
    const pos = POS_WORDS.reduce((n, w) => n + (text.includes(w) ? 1 : 0), 0);
    const neg = NEG_WORDS.reduce((n, w) => n + (text.includes(w) ? 1 : 0), 0);
    const polarity = it.tone !== 0 ? it.tone : Math.sign(pos - neg);
    for (const theme of THEMES) {
      let hits = 0;
      for (const kw of theme.kws) {
        if (text.includes(kw)) hits += 1;
      }
      if (hits === 0) continue;
      const slot =
        agg.get(theme.key) || { theme, score: 0, polarity: 0, symbols: [], items: [] };
      slot.score += hits;
      slot.polarity += polarity * hits;
      if (!slot.symbols.includes(it.row.term)) slot.symbols.push(it.row.term);
      slot.items.push({ it, hits });
      agg.set(theme.key, slot);
    }
  }
  return [...agg.values()].sort((a, b) => b.score - a.score);
}

const INTRO_MOOD = {
  pos: "どれも、良い風が吹き込む前触れです。",
  neg: "少し立ち止まって、と夢が囁いているようです。",
  mixed: "光と影が、ひとつの夜に同居していますね。",
  neutral: "いまのあなたの心を、静かに映す象徴たちです。",
};

// 総括のひとこと用: テーマの呼び名と、夜の気配のことば
const THEME_NOUN = {
  love: "絆",
  work: "挑戦",
  money: "実り",
  social: "人とのつながり",
  health: "休息",
  change: "変わり目",
};
const MOOD_EPITHET = {
  pos: "追い風の吹く",
  neg: "足もとを確かめたい",
  mixed: "光と影のあわいにある",
  neutral: "心を静かに映す",
};

function groundedItems(items, ctx) {
  return items.filter((it) => {
    const proof = it.grounding;
    if (!proof || proof.kind === "redirect" || !it.meanings?.includes(proof.text)) return false;
    const scenes = ctx.scenes.filter((s) => proof.scenes.includes(s.raw) && sceneMatches(it.row, s));
    if (!scenes.length) return false;
    if (proof.kind === "dictionary") {
      return it.senses?.some((sense) => sense.m?.some((m) => String(m).includes(proof.text)))
        && scenes.some((s) => premiseSupported(proof.text, s.raw));
    }
    const reviewed = REVIEWED_SENSES[it.row.term];
    if (!reviewed) return false;
    if (proof.text === reviewed.text) return true;
    return it.row.term === "水" && scenes.some((s) =>
      (proof.text === "穏やかな水は、気持ちの落ち着きや安心感の象徴として読めます。" && assertedPattern(s.raw, /穏やか|静か/gu)) ||
      (proof.text === "荒れた水は、感情が大きく揺れたり、抱えきれなくなったりする感覚の象徴として読めます。" && assertedPattern(s.raw, /荒れ|荒波|大波/gu)));
  });
}

function buildReadingPlan(items, ctx) {
  const meaningful = groundedItems(items, ctx).sort((a, b) => b.score - a.score);
  if (!meaningful.length) return [];
  const strongest = meaningful[0].score || 0;
  const top = meaningful.filter((it) => !strongest || it.score >= strongest - 10 ||
    (meaningful[0].row.term === "泳ぐ" && it.row.term === "水")).slice(0, 3);
  const blocks = [];
  const add = (kind, text, refs, scenes) => blocks.push({ kind, text, terms: refs.map((it) => it.row.term), scenes });
  for (const it of top) {
    const sourceText = REVIEWED_SENSES[it.row.term] ? it.grounding.text.split(/(?<=。)/u)[0] : it.grounding.text;
    const meaning = summarizeMeaning(sourceText, 180);
    if (meaning) add("interpretation", REVIEWED_SENSES[it.row.term]
      ? meaning
      : `〈${it.row.term}〉の場面に合う辞書の解釈は、「${meaning}」というものです。`, [it], it.grounding.scenes);
  }
  const terms = new Set(top.map((it) => it.row.term));
  const combination = [
    [["変身", "食べる"], "この夢は、新しい自分のあり方と、自分を満たすものを探している夢と読めます。姿の変化と、何かを自分の中に取り入れる行動が重なっているためです。"],
    [["橋", "友達"], "この夢は、人との関係を通じて次の段階へ移ることを表している、と読めます。友人に感じている魅力や大切にしたい性質が、その変化に関わるという解釈です。"],
    [["水", "泳ぐ"], "この夢は、自分の感情と付き合いながら前に進む姿を映している、と読めます。水の状態が感情を、その中を泳ぐ行動が感情への向き合い方を表すという解釈です。"],
    [["追われている", "飛ぶ"], "この夢は、負担から距離を置いて自由を取り戻したい気持ちを表している、と読めます。追われる圧迫感と、飛ぶことによる解放が結び付いています。"],
    [["落ちる", "飛ぶ"], "この夢は、支えを失う不安と、束縛を離れたい願いが重なった夢と読めます。落ちることと飛ぶことは、同じ宙に浮く体験でも、不安定さと自由という異なる意味を持ちます。"],
  ].find(([required]) => required.every((term) => terms.has(term)) &&
    ctx.scenes.some((s) => required.every((term) => top.find((it) => it.row.term === term).grounding.scenes.includes(s.raw))));
  if (combination) {
    const refs = top.filter((it) => combination[0].includes(it.row.term));
    add("connection", combination[1], refs, ctx.scenes.filter((s) => refs.every((it) => it.grounding.scenes.includes(s.raw))).map((s) => s.raw));
  }
  const feelingScene = ctx.scenes.filter((s) => top.some((it) => it.grounding.scenes.includes(s.raw)) &&
    (s.feelings.length || /怖くなかった|不安ではなかった/u.test(s.raw)))
    .sort((a, b) => scenePriority(b) - scenePriority(a))[0];
  if (feelingScene && feelingScene.raw.trim().length <= 90) {
    add("observation", `夢の中での受け止め方は、「${feelingScene.raw.trim()}」という描写に表れています。`, [], [feelingScene.raw]);
  }
  // ponytail: 省略された感覚の対象は、場面転換を挟まない直後の文だけに結び付ける。
  const tasteScene = ctx.scenes.find((s, index) => /味(?:が|は)?(?:しなかった|しない|なかった|ない)/u.test(s.raw) &&
    !/次の|別の|場面が変/u.test(s.raw) && (s.events.has("食べる") || ctx.scenes[index - 1]?.events.has("食べる")));
  if (terms.has("食べる") && tasteScene) {
    add("observation", "食べる行動はあるのに、味がしなかった。この対比を充足の象徴と重ねると、何かを取り入れても満足や手応えが伴わない感覚を表している、と解釈できます。", top.filter((it) => it.row.term === "食べる"), [tasteScene.raw]);
    const changeAndEating = blocks.find((b) => b.kind === "connection" && b.terms.includes("変身") && b.terms.includes("食べる"));
    if (changeAndEating && changeAndEating.scenes.some((raw) => raw === tasteScene.raw || ctx.scenes[ctx.scenes.indexOf(tasteScene) - 1]?.raw === raw)) {
      changeAndEating.text = "この夢の中心は、変化と充足感のずれです。姿が変わっても、食べ物の味はしない。外側の変化に内側の満足感が追いついていない、という解釈になります。";
      changeAndEating.scenes.push(tasteScene.raw);
      blocks.pop(); // 味の解釈は結論へ統合したため、同じ説明を繰り返さない。
    }
  }
  const last = ctx.scenes.at(-1)?.raw || "";
  if (assertedPattern(last, /逃げ切|逃げき|助か|救われ|助けられ|抜け出/gu)) {
    add("ending", "結末には、困っていた状況から抜け出す展開があります。そのため、途中の負担だけでなく、そこからの回復や解放までを含んだ夢と読めます。", [], [last]);
  } else if (assertedPattern(last, /安心|ほっと|嬉し|うれし|楽しかった/gu)) {
    add("ending", "結末の安心や楽しさは、その状況を前向きに受け止めていることを表す、と読めます。", [], [last]);
  }
  const connection = blocks.find((b) => b.kind === "connection");
  if (connection) {
    blocks.splice(blocks.indexOf(connection), 1);
    blocks.unshift(connection);
  }
  return blocks;
}

function composeReading(items, diaryText, ctx) {
  ctx = ctx || buildContext(diaryText);
  if (!ctx) return NO_MATCH_MESSAGE;
  const validTerms = new Set(groundedItems(items, ctx).map((it) => it.row.term));
  const seen = new Set();
  // 出力計画の参照先を再確認する。未知の文の意味を一般的に判定するAIではない。
  const blocks = buildReadingPlan(items, ctx).filter((block) => {
    if (!block.text || !/[。！？]$/u.test(block.text) || /undefined|NaN/u.test(block.text) || seen.has(block.text)) return false;
    if (!block.terms.every((term) => validTerms.has(term)) || !block.scenes.every((raw) => ctx.scenes.some((s) => s.raw === raw))) return false;
    seen.add(block.text);
    return true;
  });
  if (!blocks.length) return "この夢の場面に合う解釈を、辞書の内容から十分に確かめられませんでした。印象に残った出来事や、そのときの気持ちをもう少し聞かせてください。";
  return blocks.map((block) => block.text).join("\n\n");
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
      replay(readingRow, "mist-reveal");
      tellerSay("……うーん。");
    } else {
      readingText.textContent = composeReading(items, text, ctx);
      renderTermChips(items);
      renderMatches(matchesEl, items);
      matchCount.textContent = `${items.length}件`;
      matchedFold.hidden = false;
      readingRow.hidden = false;
      replay(readingRow, "mist-reveal");
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

function replay(el, cls = "reveal") {
  el.classList.remove("reveal", "mist-reveal");
  void el.offsetWidth;
  el.classList.add(cls);
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

let switchToken = 0;

async function switchView(name) {
  if (!views[name]) name = "fortune";
  const current = Object.keys(views).find((k) => !views[k].hidden);
  if (current === name) return;
  const token = ++switchToken;

  for (const tab of tabs) {
    const active = tab.dataset.view === name;
    tab.classList.toggle("active", active);
    tab.setAttribute("aria-selected", String(active));
  }

  // いまのビューが靄に溶けてから、次のビューが霧の中から現れる
  if (!reduceMotion && current) {
    views[current].classList.add("leaving");
    await delay(400);
    views[current].classList.remove("leaving");
    if (token !== switchToken) return;
  }

  for (const [key, el] of Object.entries(views)) {
    el.hidden = key !== name;
    el.classList.remove("enter", "leaving");
  }
  const target = views[name];
  void target.offsetWidth;
  target.classList.add("enter");

  if (name === "history") renderHistory();
  if (name === "dictionary") renderDictSuggest();
  window.scrollTo({ top: 0, behavior: reduceMotion ? "auto" : "smooth" });
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
views.fortune.classList.add("enter");
const initialView = location.hash.replace("#", "");
if (initialView && views[initialView]) switchView(initialView);
tellerSay(GREETINGS[Math.floor(Math.random() * GREETINGS.length)]);
loadData();
