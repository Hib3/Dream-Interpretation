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
  // 省データ設定・遅い回線では全シャード(約40MB)の先読みをしない
  const conn = navigator.connection;
  if (conn && (conn.saveData || /(^|-)2g/.test(conn.effectiveType || ""))) return;
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
    if (tail && tail[1] && tail[1].length >= 4) core = tail[1];
    core = core.replace(/^[、。・]+/, "").replace(/[、。]$/, "");
    // 話者注記や相互参照は結果句ではないので次の文を試す
    if (/(言いました|言われました|によると|曰く|参照)/.test(core)) continue;
    if (core.length >= 6 && core.length <= 55) return core;
    if (core.length > 55) {
      const cut = core.slice(0, 44);
      const pos = cut.lastIndexOf("、");
      return (pos > 14 ? cut.slice(0, pos) : cut) + "…";
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
const NEG_WORDS = ["凶", "不吉", "警告", "注意", "不安", "失敗", "病気", "トラブル", "危険", "損失", "悪い", "困難", "裏切り", "別れ", "苦し", "災い", "悩み", "対立", "喪失", "孤独", "悪意", "死"];

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

function composeReading(items, diaryText, ctx) {
  const concise = items.filter((it) => it.row.term.length <= 14);
  const pool = concise.length >= 3 ? concise : items;
  const top = pool.slice(0, 6);

  const tones = top.map((it) => (it.tone === undefined ? it.row.tone : it.tone));
  const posCount = tones.filter((t) => t === 1).length;
  const negCount = tones.filter((t) => t === -1).length;
  const mood =
    posCount > 0 && negCount === 0
      ? "pos"
      : negCount > 0 && posCount === 0
        ? "neg"
        : posCount > 0 && negCount > 0
          ? "mixed"
          : "neutral";

  // 導入: あなた自身の夢のことばを引いて、象徴を並べる
  const scene = String(diaryText || "").split(/[。!?！?\n]/)[0].trim();
  const sceneCut = scene.length > 26 ? `${scene.slice(0, 24)}…` : scene;
  const names = top
    .slice(0, 3)
    .map((it) => `〈${it.row.term}〉`)
    .join("");
  const intro = sceneCut
    ? `『${sceneCut}』——その夜の景色から、${names}が浮かび上がってきました。${INTRO_MOOD[mood]}`
    : `……視えましたよ。あなたの夢を漂っていたのは、${names}。${INTRO_MOOD[mood]}`;

  // 象徴の重なり(共起ルール)
  const pairInsight = ctx ? findPairInsight(top, ctx) : "";

  // テーマ別の読み: 辞書から抜き出した結果句を織り合わせ、助言を一文だけ添える
  const themeLines = [];
  const quoted = new Set();
  const themeSlots = analyzeThemes(top);
  for (const slot of themeSlots.slice(0, 3)) {
    const symsNote = slot.symbols
      .slice()
      .sort((a, b) => a.length - b.length)
      .filter((t) => t.length <= 10)
      .slice(0, 2)
      .join("・");

    // 寄与の大きい順に、まだ引いていない象徴から結果句を最大2つ
    const ranked = slot.items.sort((a, b) => b.hits - a.hits).map((s) => s.it);
    const threads = [];
    for (const it of ranked) {
      if (threads.length >= 2) break;
      if (quoted.has(it)) continue;
      const essence = extractEssence((it.meanings || [])[0]);
      if (!essence) continue;
      quoted.add(it);
      threads.push({ it, essence });
    }

    const advice =
      slot.polarity >= 1
        ? slot.theme.pos
        : slot.polarity <= -1
          ? slot.theme.neg
          : THEME_NEUTRAL;

    let body;
    if (threads.length === 2) {
      body = `〈${threads[0].it.row.term}〉は「${threads[0].essence}」、〈${threads[1].it.row.term}〉は「${threads[1].essence}」——そう夢は告げています。`;
    } else if (threads.length === 1) {
      body = `〈${threads[0].it.row.term}〉は「${threads[0].essence}」と告げています。`;
    } else {
      // 結果句が抜けない場合は原文を短く引用(辞書から離れない)
      const src = ranked.find((it) => !quoted.has(it));
      const snippet = src ? firstSentences((src.meanings || [])[0] || "", 80) : "";
      if (snippet) {
        quoted.add(src);
        body = `〈${src.row.term}〉は「${snippet}」とされます。`;
      } else {
        body = "";
      }
    }

    // 吉凶が割れているテーマは、反対側の声も並べて調停する
    const mainTone = slot.polarity >= 1 ? 1 : slot.polarity <= -1 ? -1 : 0;
    if (mainTone !== 0) {
      const counter = ranked.find((it) => {
        const t = it.tone === undefined ? it.row.tone : it.tone;
        return t === -mainTone && !quoted.has(it);
      });
      if (counter) {
        const counterEssence = extractEssence((counter.meanings || [])[0]);
        if (counterEssence) {
          quoted.add(counter);
          body += `一方で〈${counter.row.term}〉は「${counterEssence}」とも。`;
        }
      }
    }

    if (!body && !advice) continue;
    themeLines.push(`✦${slot.theme.label}${symsNote ? `(${symsNote})` : ""} ${body}${advice}`);
  }

  // テーマが読み取れない夢でも、辞書の意味そのものは必ず伝える
  if (themeLines.length === 0) {
    for (const it of top.slice(0, 3)) {
      const snippet = firstSentences((it.meanings || [])[0] || "", 90);
      if (!snippet) continue;
      const tone = TONE_LABEL[it.tone] ? `【${TONE_LABEL[it.tone]}】` : "";
      themeLines.push(`✦〈${it.row.term}〉${tone} ${snippet}`);
    }
  }

  // 今夜のしるし(強く出た象徴のうち、短く覚えやすいもの)
  const omenTerm = top
    .slice(0, 3)
    .map((it) => it.row.term)
    .sort((a, b) => a.length - b.length)[0];
  const omen = `今夜のしるしは〈${omenTerm}〉。眠る前にひとつだけ、それを思い浮かべてみてください。`;

  // 総括のひとこと: 主なテーマと夜の気配をひとつの句に束ねる
  const nouns = themeSlots.slice(0, 2).map((slot) => THEME_NOUN[slot.theme.key]);
  const nightName = nouns.length
    ? `${MOOD_EPITHET[mood]}、${nouns.join("と")}の夜`
    : `〈${omenTerm}〉が寄り添う夜`;
  const closing = `——ひとことで言うなら、今夜は「${nightName}」。どうか、良い夢の続きを。`;

  const parts = [intro];
  if (pairInsight) parts.push(`✧象徴の重なり — ${pairInsight}`);
  if (themeLines.length) parts.push(themeLines.join("\n"));
  parts.push(toneSummary(top));
  parts.push(omen);
  parts.push(closing);
  return parts.join("\n\n");
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
