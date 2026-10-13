// Exercise the existing browser loader against staging, without replacing production.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.join(__dirname, '..');
const staging = path.resolve(process.argv[2] || path.join(root, '.codex/staging-sample'));
const fullBuild = process.argv.includes('--full');
const requests = [];
const context = vm.createContext({
  window: {matchMedia: () => ({matches: true})},
  document: {querySelector: () => ({})},
  tellerSay: message => { throw new Error(message); },
  fetch: async url => {
    requests.push(url);
    const pathname = url.split('?')[0];
    assert(pathname.startsWith('data/ja/'));
    const file = path.join(staging, path.basename(pathname));
    if (!fs.existsSync(file)) return {ok: false, status: 404};
    return {ok: true, json: async () => JSON.parse(fs.readFileSync(file, 'utf8'))};
  },
});
vm.runInContext(fs.readFileSync(path.join(root, 'app.js'), 'utf8').split('/* ---------- 占い師の語り')[0] +
  '\nrenderDictSuggest=()=>{};globalThis.api={state,loadData,getShard,buildContext,findMatches,attachMeanings,composeReading,reportMatches};', context);
(async () => {
  const api = context.api;
  await api.loadData();
  assert.equal(api.state.masterSchema, 'dream-master-1');
  if (fullBuild) {
    const index = JSON.parse(fs.readFileSync(path.join(staging, 'terms.min.json'), 'utf8'));
    const runtimeFile = path.join(staging, 'reading-semantics-runtime.json');
    const semanticsFile = fs.existsSync(runtimeFile) ? runtimeFile : path.join(staging, 'reading-semantics.json');
    const semanticsUrl = `data/ja/${path.basename(semanticsFile)}`;
    const runtimeSemantics = JSON.parse(fs.readFileSync(semanticsFile, 'utf8'));
    assert.equal(runtimeSemantics.version, 1);
    assert(Array.isArray(runtimeSemantics.profiles));
    assert(requests.includes(semanticsUrl));
    if (fs.existsSync(runtimeFile)) assert(!requests.includes('data/ja/reading-semantics.json'));
    assert.equal(api.state.rows.length, index.entry_count);
    assert(api.state.rows.length > 35000);
  } else {
    assert.equal(api.state.rows.length, 35);
  }
  for (const row of api.state.rows) {
    const shard = await api.getShard(row.shard);
    const senses = shard[row.idx];
    assert(senses.length);
    for (const sense of senses) {
      assert(sense.m.length && sense.m.every(m => typeof m === 'string' && m.length));
      assert(sense.s.length && sense.r && sense.id && sense.l);
    }
  }
  const curated = (fullBuild
    ? api.state.readingProfiles
    : JSON.parse(fs.readFileSync(path.join(root, 'data/ja/reading-semantics.json'), 'utf8')).profiles)
    .filter(profile => profile.source?.master_only);
  for (const profile of curated) {
    const row = api.state.rows.find(entry => entry.term === profile.term);
    if (!row) {
      if (fullBuild) continue;
      assert.fail(profile.id + ': source-backed headword missing');
    }
    const shard = await api.getShard(row.shard);
    const sense = shard[row.idx].find(candidate => fullBuild
      ? candidate.id === profile.source.sense_id
      : candidate.o === profile.source.orig && candidate.s.includes(profile.source.name) &&
        candidate.m.some(text => text.includes(profile.evidence)));
    assert(sense, profile.id + ': source-backed staged sense missing');
    if (!fullBuild) profile.source.sense_id = sense.id;
  }
  if (!fullBuild) api.state.readingProfiles.push(...curated);
  const diagnosisGaps = [];
  const diagnosisSamples = {};
  for (const text of ['銀行に預金した。', '夏だった。', '虎を見た。', '花束を見た。']) {
    const ctx = api.buildContext(text);
    const items = api.findMatches(ctx);
    await api.attachMeanings(items, ctx);
    assert(items.length, text + ': no dictionary match');
    assert(items.every(i => i.meanings.length && i.sources.length));
    const reading = api.composeReading(items, text, ctx);
    diagnosisSamples[text] = reading;
    assert(typeof reading === 'string' && reading.length, text + ': empty diagnosis');
    const expected = {
      '銀行に預金した。': ['損失への警告', '家庭内の問題'],
      '夏だった。': ['豊かさ|願い|財産|恵み', null],
      '虎を見た。': ['よくない兆し', null],
      '花束を見た。': ['地位を得る', null],
    }[text];
    assert(new RegExp(expected[0]).test(reading), text + ': missing source-grounded implication\n' + reading);
    if (expected[1]) assert(!reading.includes(expected[1]), text + ': unrelated conditional implication\n' + reading);
    if (/解釈を十分に確かめられません|解釈を、辞書から確かめられません/.test(reading)) diagnosisGaps.push(text);
  }
  for (const text of ['冬だった。', '夏ではなかった。']) {
    const ctx = api.buildContext(text);
    const items = api.findMatches(ctx);
    await api.attachMeanings(items, ctx);
    assert(!items.some(i => i.row.term === '夏'), text + ': summer false positive');
  }
  if (fullBuild) {
    const cases = [
      ['朝起きたら、犬になっていて、バタートーストを食べた。味はしなかった。次のシーンでは猫になって魚を食べていた。',
        ['変化は描かれているものの', '味がしなかった', '満足', '友人の不誠実な言動に用心'], ['ナッツ', '鳴き声', '黄金色', '親しさや信頼'], ['バター', '犬', '猫']],
      ['水の中を泳いでいたら、橋の向こうに白い犬がいて、最後は空を飛ぶように逃げた。',
        ['感情面の課題', '別の段階へ移る', '悪意や妨害', '避けているものにまだ気づいていない'], ['仕事における無関心'], ['空']],
    ];
    for (const [text, required, forbidden, excludedTerms] of cases) {
      const ctx = api.buildContext(text), items = api.findMatches(ctx);
      await api.attachMeanings(items, ctx);
      const reading = api.composeReading(items, text, ctx);
      for (const phrase of required) assert(reading.includes(phrase), `${text}: missing ${phrase}\n${reading}`);
      for (const phrase of forbidden) assert(!reading.includes(phrase), `${text}: unrelated ${phrase}\n${reading}`);
      const citedTerms = api.reportMatches(items, ctx).map((item) => item.row.term);
      for (const term of excludedTerms) assert(!citedTerms.includes(term), `${text}: uncited term ${term}`);
    }
  }
  assert(requests.some(u => /meanings-\d{2}\.min\.json\?v=/.test(u)));
  console.log(JSON.stringify({loader: 'PASS', terms: api.state.rows.length, shards: 'PASS', retrievalCases: 4,
    diagnosisReady: diagnosisGaps.length === 0, diagnosisGaps, diagnosisSamples}));
  if (process.argv.includes('--require-diagnosis')) assert.equal(diagnosisGaps.length, 0, 'Phase 2 promotion blocked: diagnosis handoff');
})().catch(e => {console.error(e); process.exitCode = 1;});
