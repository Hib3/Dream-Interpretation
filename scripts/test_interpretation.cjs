const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const zlib = require('node:zlib');
const root = path.join(__dirname, '..');
const context = vm.createContext({
  window: {matchMedia: () => ({matches: true})},
  document: {querySelector: () => ({})},
  fetch: async (url) => ({ok: true, json: async () => JSON.parse(fs.readFileSync(path.join(root, url.split('?')[0]), 'utf8'))}),
});
const code = fs.readFileSync(path.join(root, 'app.js'), 'utf8').split('/* ---------- 占い師の語り')[0];
vm.runInContext(code + '\nrenderDictSuggest=()=>{}; prefetchShards=()=>{}; globalThis.api={loadData,buildContext,findMatches,attachMeanings,composeReading,REVIEWED_SENSES};', context);
const api = context.api;

(async () => {
  await api.loadData();
  const cases = [
    ['橋を渡って友達に会った。', '橋', ['友達は橋から落ちました', '友達の友達', '渡す']],
    ['犬に追いかけられて怖かった。最後は逃げ切れて安心した。', '追われている', ['安さ', 'ひよこを切る', '追いかけられたが逃げられない']],
    ['歯が抜けた。', '歯が抜ける', ['落ちる', 'あざを抜く']],
    ['崖から落ちなかった。', '崖', ['落ちる']],
    ['空を飛ぶように走った。', null, ['飛ぶ']],
    ['鳥が空を飛んでいた。', '鳥', ['飛ぶ']],
    ['穏やかな水を眺めた。', '水', ['眺め', '穏やかな']],
    ['犬はいなかった。', null, ['犬']],
    ['犬じゃなく猫だった。', '猫', ['犬']],
    ['犬を見なかった。', null, ['犬']],
    ['現実では犬を飼っている。夢では橋を渡った。', '橋', ['犬']],
    ['プールで泳いでいた。', '泳ぐ', ['泳ぐ子牛の頭']],
    ['海で溺れたが、最後は助かった。', '溺れる', []],
    ['机の上にリンゴがあった。', 'リンゴ', ['に', '上']],
    ['犬には追われなかった。', '犬', ['追われている']],
    ['歯が抜けなかった。', null, ['歯が抜ける']],
    ['高い場所から落ちた後、空を飛んだ。', '飛ぶ', []],
    ['水の中を泳いだ。', '泳ぐ', []],
  ];
  const outputs = [];
  for (const [text, expected, excluded] of cases) {
    const ctx = api.buildContext(text);
    const matches = api.findMatches(ctx);
    const terms = matches.map((it) => it.row.term);
    if (expected) assert(terms.includes(expected), `${text}: missing ${expected}: ${terms}`);
    for (const term of excluded) assert(!terms.includes(term), `${text}: unexpected ${term}`);
    await api.attachMeanings(matches, ctx);
    const reading = api.composeReading(matches, text, ctx);
    assert(!/undefined|NaN|ひよこを切る|善と善/.test(reading));
    outputs.push({text, terms: [...terms], reading});
  }
  const denied = '犬に追われた。最後は助からなかった。';
  const ctx = api.buildContext(denied), items = api.findMatches(ctx);
  await api.attachMeanings(items, ctx);
  assert(!api.composeReading(items, denied, ctx).includes('抜け出す展開'));
  assert(outputs[1].reading.includes('抜け出す展開'));
  assert(outputs[6].reading.includes('穏やかな水'));
  assert(outputs[0].reading.includes('人との関係を通じて'));
  assert(outputs.at(-1).reading.includes('感情と付き合いながら'));
  const original = JSON.parse(zlib.gunzipSync(fs.readFileSync(path.join(root,'data/dream_terms.json.gz'))));
  for (const sense of Object.values(api.REVIEWED_SENSES)) {
    assert(original.entries.some((e) => e.term === sense.orig && e.meanings.some((m) => m.source_name === sense.source && m.text.includes(sense.quote))), `Missing source for ${sense.orig}`);
  }
  console.log(`${cases.length} real-dictionary scenarios, ending checks, and ${Object.keys(api.REVIEWED_SENSES).length} original-source checks passed.`);
  if (process.argv.includes('--examples')) for (const row of outputs.slice(0, 3)) console.log(JSON.stringify(row));
})().catch((error) => { console.error(error); process.exitCode = 1; });
