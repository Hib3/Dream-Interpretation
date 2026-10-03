// Exercise the existing browser loader against staging, without replacing production.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.join(__dirname, '..');
const staging = path.resolve(process.argv[2] || path.join(root, '.codex/staging-sample'));
const requests = [];
const context = vm.createContext({
  window: {matchMedia: () => ({matches: true})},
  document: {querySelector: () => ({})},
  fetch: async url => {
    requests.push(url);
    const pathname = url.split('?')[0];
    assert(pathname.startsWith('data/ja/'));
    return {ok: true, json: async () => JSON.parse(fs.readFileSync(path.join(staging, path.basename(pathname)), 'utf8'))};
  },
});
vm.runInContext(fs.readFileSync(path.join(root, 'app.js'), 'utf8').split('/* ---------- 占い師の語り')[0] +
  '\nrenderDictSuggest=()=>{};globalThis.api={state,loadData,getShard,buildContext,findMatches,attachMeanings,composeReading};', context);
(async () => {
  const api = context.api;
  await api.loadData();
  assert.equal(api.state.masterSchema, 'dream-master-1');
  assert.equal(api.state.rows.length, 35);
  for (const row of api.state.rows) {
    const shard = await api.getShard(row.shard);
    const senses = shard[row.idx];
    assert(senses.length);
    for (const sense of senses) {
      assert(sense.m.length && sense.m.every(m => typeof m === 'string' && m.length));
      assert(sense.s.length && sense.r && sense.id && sense.l);
    }
  }
  const diagnosisGaps = [];
  for (const text of ['銀行に預金した。', '夏だった。', '虎を見た。', '花束を見た。']) {
    const ctx = api.buildContext(text);
    const items = api.findMatches(ctx);
    await api.attachMeanings(items, ctx);
    assert(items.length, text + ': no dictionary match');
    assert(items.every(i => i.meanings.length && i.sources.length));
    const reading = api.composeReading(items, text, ctx);
    assert(typeof reading === 'string' && reading.length, text + ': empty diagnosis');
    if (/解釈を十分に確かめられません|解釈を、辞書から確かめられません/.test(reading)) diagnosisGaps.push(text);
  }
  for (const text of ['冬だった。', '夏ではなかった。']) {
    const ctx = api.buildContext(text);
    const items = api.findMatches(ctx);
    await api.attachMeanings(items, ctx);
    assert(!items.some(i => i.row.term === '夏'), text + ': summer false positive');
  }
  assert(requests.some(u => /meanings-\d{2}\.min\.json\?v=/.test(u)));
  console.log(JSON.stringify({loader: 'PASS', terms: api.state.rows.length, shards: 'PASS', retrievalCases: 4,
    diagnosisReady: diagnosisGaps.length === 0, diagnosisGaps}));
  if (process.argv.includes('--require-diagnosis')) assert.equal(diagnosisGaps.length, 0, 'Phase 2 promotion blocked: diagnosis handoff');
})().catch(e => {console.error(e); process.exitCode = 1;});
