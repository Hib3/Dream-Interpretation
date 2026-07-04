const state = {
  entries: [],
  index: [],
  loaded: false,
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
  "I was falling from a tall building, then found a hidden door and crossed a bridge over dark water.",
  "Rüyada deniz gördüm, sonra beyaz bir kuş uçtu ve eski bir eve girdim.",
];

const stopWords = new Set([
  "dream",
  "dreaming",
  "about",
  "being",
  "with",
  "from",
  "into",
  "that",
  "this",
  "your",
  "you",
  "and",
  "the",
  "rüyada",
  "görmek",
  "anlama",
  "gelir",
]);

function normalize(value) {
  return String(value || "")
    .toLocaleLowerCase()
    .normalize("NFKC")
    .replace(/[^\p{Letter}\p{Number}\s-]/gu, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function tokenize(value) {
  return normalize(value)
    .split(" ")
    .filter((token) => token.length >= 3 && !stopWords.has(token));
}

function compactMeaning(text, maxLength = 260) {
  const clean = String(text || "").replace(/\s+/g, " ").trim();
  if (clean.length <= maxLength) return clean;
  return `${clean.slice(0, maxLength - 1).trim()}...`;
}

function buildIndex(entries) {
  return entries
    .map((entry) => {
      const haystack = [
        entry.term,
        entry.term_normalized,
        ...(entry.aliases || []),
      ].join(" ");
      const tokens = tokenize(haystack);
      return {
        entry,
        normalizedTerm: normalize(entry.term),
        tokens,
        tokenSet: new Set(tokens),
      };
    })
    .filter((item) => item.normalizedTerm || item.tokens.length);
}

async function loadData() {
  try {
    const response = await fetch("data/dream_terms.min.json", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    state.entries = data.entries || [];
    state.index = buildIndex(state.entries);
    state.loaded = true;
    const languages = Object.entries(data.language_counts || {})
      .map(([lang, count]) => `${lang}:${count.toLocaleString()}`)
      .join(" / ");
    dataStatus.textContent = `辞書 ${data.entry_count.toLocaleString()} 件 / ${data.source_count || "-"} sources / ${languages}`;
  } catch (error) {
    dataStatus.textContent = "辞書の読み込みに失敗しました";
    readingText.textContent = `data/dream_terms.json を確認してください。${error.message}`;
  }
}

function findMatches(text) {
  const normalizedText = normalize(text);
  const inputTokens = new Set(tokenize(text));
  if (!normalizedText) return [];

  const scored = [];
  for (const item of state.index) {
    let score = 0;
    if (item.normalizedTerm.length >= 3 && normalizedText.includes(item.normalizedTerm)) {
      score += Math.min(12, Math.ceil(item.normalizedTerm.length / 2));
    }
    for (const token of item.tokenSet) {
      if (inputTokens.has(token)) score += 2;
    }
    if (score > 0) scored.push({ ...item, score });
  }

  return scored
    .sort((a, b) => b.score - a.score || a.entry.term.length - b.entry.term.length)
    .slice(0, 24)
    .map((item) => item.entry);
}

function makeReading(matches, text) {
  if (!text.trim()) {
    return "夢日記を入力して「占う」を押してください。";
  }
  if (!state.loaded) {
    return "辞書を読み込み中です。少し待ってからもう一度押してください。";
  }
  if (matches.length === 0) {
    return "今回の夢は、辞書内の単語と強く一致するものが見つかりませんでした。印象に残った物、人、場所、感情をもう少し具体的に書くと照合しやすくなります。";
  }

  const top = matches.slice(0, 5);
  const terms = top.map((entry) => `「${entry.term}」`).join("、");
  const tones = top
    .map((entry) => entry.meanings?.[0]?.text || "")
    .filter(Boolean)
    .map((meaning) => compactMeaning(meaning, 90));

  return [
    `この夢では ${terms} が強く出ています。`,
    "全体として、感情・変化・不安・移動に関わる象徴が重なっている可能性があります。",
    tones.length ? `主な読み筋: ${tones.join(" / ")}` : "",
    "夢占いは断定ではなく、起きている時の気分や最近の出来事を見直すための手がかりとして読んでください。",
  ]
    .filter(Boolean)
    .join("\n\n");
}

function replay(el) {
  el.classList.remove("reveal");
  void el.offsetWidth; // アニメーションを再生し直すためのリフロー
  el.classList.add("reveal");
}

function renderTermChips(matches) {
  termChips.innerHTML = "";
  termChips.hidden = matches.length === 0;
  matches.forEach((entry, i) => {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "term-chip";
    chip.textContent = entry.term;
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

function renderMatches(matches) {
  matchCount.textContent = `${matches.length}件`;
  matchedBlock.hidden = matches.length === 0;
  matchesEl.innerHTML = "";
  matches.forEach((entry, i) => {
    const meaning = entry.meanings?.[0];
    const source = entry.sources?.[0];
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
    card.querySelector(".match-term").textContent = entry.term;
    card.querySelector(".match-lang").textContent = entry.language;
    card.querySelector(".match-meaning").textContent = compactMeaning(meaning?.text || "");
    card.querySelector(".match-source").textContent = source
      ? `${source.name} / meanings: ${entry.meaning_count}`
      : `meanings: ${entry.meaning_count}`;
    matchesEl.appendChild(card);
  });
}

function interpret() {
  const text = dreamInput.value;
  const matches = findMatches(text);
  readingText.textContent = makeReading(matches, text);
  renderTermChips(matches);
  renderMatches(matches);
  replay(resultCard);
  if (text.trim()) {
    resultCard.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }
}

interpretBtn.addEventListener("click", interpret);
clearBtn.addEventListener("click", () => {
  dreamInput.value = "";
  interpret();
  dreamInput.focus();
});
sampleBtn.addEventListener("click", () => {
  const current = samples.shift();
  samples.push(current);
  dreamInput.value = current;
  interpret();
});

loadData();
