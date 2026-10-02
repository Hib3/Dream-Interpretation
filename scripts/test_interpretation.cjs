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
vm.runInContext(code + '\nrenderDictSuggest=()=>{}; globalThis.api={state,loadData,buildContext,findMatches,attachMeanings,composeReading,buildReadingPlan,groundedItems,selectContextualSense,REVIEWED_SENSES};', context);
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
  assert(!outputs[1].reading.includes('守ってくれる存在'));
  assert(outputs[6].reading.includes('穏やかな水'));
  assert(outputs[0].reading.includes('次へ移ること') && outputs[0].reading.includes('取り入れたい性質'));
  assert(outputs.at(-1).reading.includes('感情を揺さぶられる課題') && outputs.at(-1).reading.includes('心の状態や感情'));
  const report = '朝起きたら、犬になっていて、バタートーストを食べた。味はしなかった。次のシーンでは猫になって魚を食べていた。';
  const reportCtx = api.buildContext(report), reportItems = api.findMatches(reportCtx);
  const started = performance.now();
  await api.attachMeanings(reportItems, reportCtx);
  const reportReading = api.composeReading(reportItems, report, reportCtx);
  const selectionMs = Math.round(performance.now() - started);
  assert.equal(api.state.rows.find((r) => r.orig === 'yemiş').term, 'ナッツ');
  assert(reportItems.some((i) => i.row.term === '変身') && reportReading.includes('心の充足') && reportReading.includes('味がしなかった'));
  assert(reportReading.startsWith('この夢では、'));
  assert(!/未来の予測|振り返ってみて|どんな感覚があったでしょう/.test(reportReading));
  assert(!/ナッツ|鳴き声|黄金色|婚約|食べた〉/.test(reportReading + reportItems.flatMap((i) => i.meanings).join('')));
  const sense = (text) => ({o:'test', t:0, m:[text], s:['test']});
  const gates = [
    ['夢の中でナッツを食べることは、幸運を意味します。', 'トーストを食べた', false],
    ['夢の中でナッツを食べることは、幸運を意味します。', 'ナッツを食べた', true],
    ['猫の鳴き声を聞いたら、よい知らせがあります。', '猫になって魚を食べた', false],
    ['猫の鳴き声を聞いたら、よい知らせがあります。', '猫の鳴き声を聞いた', true],
    ['猫の鳴き声を聞いたら、よい知らせがあります。', '猫の鳴き声を聞かなかった', false],
    ['腐ったバターを食べることは、苦労を意味します。', 'バターを食べた', false],
    ['バターを売ることは、小さな利益を意味します。', 'バターを買った', false],
    ['新鮮な黄金色のバターを食べる夢は、健康の表れです。', 'バタートーストを食べた', false],
    ['たくさんの猫を見ることは、秘密を意味します。', '猫を見た', false],
    ['猫が魚を食べることは、幸運を意味します。', '魚が猫を食べた', false],
    ['猫が魚を食べることは、幸運を意味します。', '猫が魚を食べた', true],
  ];
  for (const [text, scene, expected] of gates) assert.equal(Boolean(api.selectContextualSense([sense(text)], [scene])), expected, `${scene}: ${text}`);
  const wrong = sense('夢の中でナッツを食べることは、幸運を意味します。');
  const right = sense('夢の中でトーストを食べることは、満足を意味します。');
  assert.equal(api.selectContextualSense([wrong, right], ['トーストを食べた']).sense, right);
  assert.equal(api.selectContextualSense([sense('猫が魚を食べることは、幸運を意味します。')], ['猫を見た', '犬が魚を食べた']), null);
  const negativeChange = api.buildContext('犬になっていなかった。');
  const negativeItems = api.findMatches(negativeChange);
  await api.attachMeanings(negativeItems, negativeChange);
  assert(!negativeItems.some((i) => i.row.term === '変身' || i.meanings.some((m) => m.includes('姿が変わった先'))));
  const interpret = async (text) => {
    const ctx = api.buildContext(text), items = api.findMatches(ctx);
    await api.attachMeanings(items, ctx);
    return {ctx, items, plan: api.buildReadingPlan(items, ctx), reading: api.composeReading(items, text, ctx)};
  };
  const genericCtx = api.buildContext('鐘と鍵があった。');
  const dictionaryFixture = (term, text, score) => ({row:api.state.rows.find((r) => r.term === term), score,
    meanings:[text], senses:[{m:[text]}], grounding:{kind:'dictionary', text, scenes:[genericCtx.scenes[0].raw]}});
  const genericItems = [dictionaryFixture('鐘', '鐘は、新しい知らせを暗示します。', 20), dictionaryFixture('鍵', '鍵は、問題の解決を暗示します。', 19)];
  const genericPlan = api.buildReadingPlan(genericItems, genericCtx);
  assert.equal(genericPlan[0].kind, 'connection');
  assert(genericPlan[0].text.includes('新しい知らせ') && genericPlan[0].text.includes('問題の解決'));
  for (const claim of genericPlan[0].claims) assert(genericItems.find((i) => i.row.term === claim.term).grounding.text.includes(claim.text));
  genericItems[0] = dictionaryFixture('鐘', '鐘は、長く待った再会を暗示します。', 20);
  const updatedReading = api.composeReading(genericItems, '鐘と鍵があった。', genericCtx);
  assert(updatedReading.includes('長く待った再会') && !updatedReading.includes('新しい知らせ'));
  const fullNarrative = '水の中を泳いでいたら、橋の向こうに白い犬がいて、最後は空を飛ぶように逃げた。';
  const narrative = await interpret(fullNarrative);
  for (const term of ['水', '泳ぐ', '橋', '白い犬', '逃げる']) {
    assert(narrative.items.some((i) => i.row.term === term), `Missing dictionary match: ${term}`);
    assert(narrative.plan.some((b) => b.terms.includes(term)), `Omitted from reading: ${term}`);
  }
  assert(!narrative.items.some((i) => i.row.term === '飛ぶ'));
  for (const meaning of ['悪意', '身を守る', '次へ移る', '不安や問題を避け', '今の状況から離れたい']) assert(narrative.reading.includes(meaning), `Missing implication: ${meaning}`);
  assert(!/手がかり|振り返|逃げ切|追ってきた|犬から逃げ/.test(narrative.reading));
  const noEscape = await interpret(fullNarrative.replace('逃げた', '逃げなかった'));
  assert(!noEscape.items.some((i) => i.row.term === '逃げる'));
  assert(!noEscape.plan.some((b) => b.kind === 'connection' && b.terms.includes('逃げる')));
  const differentColor = await interpret(fullNarrative.replace('白い犬', '黒い犬'));
  assert(!differentColor.items.some((i) => i.row.term === '白い犬'));
  assert(!differentColor.reading.includes('自分の善良さや高潔さ'));
  const reversedSimile = await interpret('最後は逃げるように空を飛んだ。');
  assert(reversedSimile.items.some((i) => i.row.term === '飛ぶ'));
  assert(!reversedSimile.items.some((i) => i.row.term === '逃げる'));
  const differentEpisode = await interpret('水の中を泳いだ。次の場面では、最後に逃げた。');
  assert(!differentEpisode.plan.some((b) => b.kind === 'connection' && b.terms.includes('泳ぐ') && b.terms.includes('逃げる')));
  const reverseOrder = await interpret('最後は逃げて泳いだ。');
  assert(reverseOrder.reading.indexOf('〈逃げる〉') < reverseOrder.reading.indexOf('〈泳ぐ〉'));
  const swimmingEmphasis = await interpret('特に水の中を泳いだことが印象に残った。最後は逃げた。');
  assert(swimmingEmphasis.plan[0].terms.includes('逃げる'));
  const dogEmphasis = await interpret('水の中を泳いだ。白い犬が一番印象に残った。最後は逃げた。');
  assert.equal(dogEmphasis.plan[0].terms[0], '白い犬');
  for (const text of ['逃げ道を探した。', '逃げ場がなかった。', '逃走計画を立てた。', '逃げる予定だった。']) {
    assert(!api.buildContext(text).scenes.some((s) => s.events.has('逃げる')), text);
  }
  for (const text of ['逃げ出した。', '逃走した。', '逃亡していた。', '逃げ切った。']) {
    assert(api.buildContext(text).scenes.some((s) => s.events.has('逃げる')), text);
  }
  console.log('Narrative coverage, source implications, negated escape, color, simile and episode tests passed.');
  if (process.argv.includes('--examples')) console.log(JSON.stringify({text:fullNarrative, reading:narrative.reading, terms:narrative.items.map((i) => i.row.term)}));
  const eventMatrix = [
    ['海で泳ぐ予定だった。', '泳ぐ', false],
    ['海で泳いだ。', '泳ぐ', true],
    ['空を飛ぶつもりだった。', '飛ぶ', false],
    ['空を飛んだ。', '飛ぶ', true],
    ['もし空を飛ぶなら楽しいと思った。', '飛ぶ', false],
    ['落ちるかもしれないと心配した。', '落ちる', false],
    ['崖から落ちた。', '落ちる', true],
    ['落ちそうになったが落ちなかった。', '落ちる', false],
    ['歯が抜けるのを想像した。', '歯が抜ける', false],
    ['歯が抜けた。', '歯が抜ける', true],
    ['泳ぎたかった。', '泳ぐ', false],
    ['パンを食べたいと思った。', '食べる', false],
    ['パンを食べた。', '食べる', true],
    ['「飛ぶ」という文字を見た。', '飛ぶ', false],
    ['鳥が空を飛んでいた。', '飛ぶ', false],
    ['鳥が空を飛んでいて、私も空を飛んだ。', '飛ぶ', true],
    ['空を飛んだわけではない。', '飛ぶ', false],
    ['橋を渡った。現実では泳いだ。', '泳ぐ', false],
  ];
  for (const [text, term, expected] of eventMatrix) {
    assert.equal(api.buildContext(text).scenes.some((s) => s.events.has(term)), expected, text);
  }
  const relationMatrix = [
    ['猫がいて、犬が魚を食べた', false],
    ['猫が魚を見て、犬が肉を食べた', false],
    ['猫が、魚を食べた', true],
    ['猫が、魚をとてもおいしく食べた', true],
    ['猫が魚を食べた', true],
    ['魚が猫を食べた', false],
    ['猫が魚を食べる予定だった', false],
  ];
  for (const [scene, expected] of relationMatrix) {
    assert.equal(Boolean(api.selectContextualSense([sense('猫が魚を食べることは、幸運を意味します。')], [scene])), expected, scene);
  }
  for (const [required, actual] of [['赤い車', '青い車'], ['高い橋', '低い橋'], ['腐ったパン', 'パン']]) {
    assert.equal(api.selectContextualSense([sense(`${required}を見ることは、変化を意味します。`)], [`${actual}を見た`]), null);
  }
  const satisfying = await interpret('鳥になってパンを食べた。とてもおいしかった。');
  assert(!satisfying.reading.includes('充足感のずれ'));
  const imaginary = await interpret('海で泳ぐ予定だった。');
  assert.equal(imaginary.plan.length, 0);
  assert.equal(imaginary.items.length, 0);
  assert.equal(api.selectContextualSense([sense('海を見ることは、両親の心が壊れ、仕事が成功し、不運が起き、利益が増え、争いに勝つことを意味します。')], ['海を見た']), null);
  assert.equal(api.selectContextualSense([sense('家を見ることは、悪意と悪意のある人に勝つことを意味します。')], ['家を見た']), null);
  assert(api.selectContextualSense([sense('海を見ることは、豊かな感情を意味します。')], ['海を見た']));
  const sea = await interpret('海を見た。');
  assert(sea.reading.includes('自分の内側の状態'));
  assert(!/両親|不運|新しい仕事|悪意/.test(sea.reading));
  console.log(`${eventMatrix.length + relationMatrix.length + 3} cross-category assertion, role and qualifier checks passed.`);
  const emphasis = await interpret('犬は背景にちらっと見えた。一番印象に残ったのは橋を渡ったことだった。');
  assert.equal(emphasis.plan[0].terms[0], '橋');
  assert(!emphasis.plan[0].terms.includes('犬'));
  const mild = await interpret('泳いだ。少し怖かった。');
  const strong = await interpret('泳いだ。とても怖かった。');
  assert(strong.items.find((i) => i.row.term === '泳ぐ').score > mild.items.find((i) => i.row.term === '泳ぐ').score);
  assert(strong.reading.includes('とても怖かった'));
  const unafraid = await interpret('泳いだ。怖くなかった。');
  assert.equal(unafraid.ctx.scenes[0].feelings.length, 0);
  assert(unafraid.reading.includes('怖くなかった'));
  const separate = await interpret('橋を渡った。次の場面で友達に会った。');
  assert(!separate.plan.some((block) => block.kind === 'connection'));
  const together = await interpret('橋を渡って友達に会った。');
  assert(together.plan.some((block) => block.kind === 'connection'));
  const contrast = api.buildContext('猫を見たが、犬が魚を食べた。');
  assert.equal(api.selectContextualSense([sense('猫が魚を食べることは、幸運を意味します。')], contrast.scenes.map((s) => s.raw)), null);
  const detachedTaste = await interpret('パンを食べた。次の場面では味がしなかった。');
  assert(!/満足や手応えが伴わない|充足感のずれ/.test(detachedTaste.reading));
  const reality = await interpret('犬に追われた。現実では助かった。');
  assert(!reality.reading.includes('抜け出す展開'));
  const irrelevantEnding = await interpret('橋を渡った。最後に財布が見つかった。');
  assert(!irrelevantEnding.reading.includes('抜け出す展開'));
  const damaged = {...reportItems.find((i) => i.row.term === '食べる'), meanings:['ナッツを食べると幸運です。']};
  assert.equal(api.groundedItems([damaged], reportCtx).length, 0);
  assert(!api.composeReading([damaged], report, reportCtx).includes('ナッツ'));
  const mixed = {...damaged, meanings:[...damaged.meanings, damaged.grounding.text]};
  assert(!api.composeReading([mixed], report, reportCtx).includes('ナッツ'));
  assert.equal(api.groundedItems(reportItems, api.buildContext('橋を渡った。')).length, 0);
  assert.equal(new Set(together.plan.map((b) => b.text)).size, together.plan.length);
  for (const result of [emphasis, mild, strong, unafraid, separate, together, detachedTaste, reality]) {
    for (const block of result.plan) assert(block.scenes.every((raw) => result.ctx.scenes.some((s) => s.raw === raw)));
  }
  console.log('Nuance, intensity, scene separation, provenance, tampering and output-plan checks passed.');
  const original = JSON.parse(zlib.gunzipSync(fs.readFileSync(path.join(root,'data/dream_terms.json.gz'))));
  for (const sense of Object.values(api.REVIEWED_SENSES)) {
    assert(original.entries.some((e) => e.term === sense.orig && e.meanings.some((m) => m.source_name === sense.source && m.text.includes(sense.quote))), `Missing source for ${sense.orig}`);
  }
  console.log(`${cases.length} real-dictionary scenarios, ending checks, and ${Object.keys(api.REVIEWED_SENSES).length} original-source checks passed.`);
  console.log(`User regression and ${gates.length + 2} premise/sense checks passed; local selection/composition ${selectionMs} ms.`);
  if (process.argv.includes('--examples')) console.log(JSON.stringify({text:report, reading:reportReading, terms:reportItems.map((i) => i.row.term)}));
  if (process.argv.includes('--examples')) for (const row of outputs.slice(0, 3)) console.log(JSON.stringify(row));
})().catch((error) => { console.error(error); process.exitCode = 1; });
