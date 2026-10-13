const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const zlib = require('node:zlib');
const root = path.join(__dirname, '..');
const dataRoot = path.resolve(process.env.DICTIONARY_DATA_DIR || path.join(root, 'data/ja'));
const context = vm.createContext({
  window: {matchMedia: () => ({matches: true})},
  document: {querySelector: () => ({})},
  fetch: async (url) => ({ok: true, json: async () => JSON.parse(fs.readFileSync(
    path.join(dataRoot, url.split('?')[0].replace(/^data\/ja\//, '')), 'utf8'))}),
});
vm.runInContext(fs.readFileSync(path.join(root, 'app.js'), 'utf8').split('/* ---------- 占い師の語り')[0] +
  '\nrenderDictSuggest=()=>{};globalThis.api={state,loadData,buildContext,findMatches,attachMeanings,composeReading,buildReadingPlan,meaningClaims,readingProfile,REVIEWED_SENSES};', context);
const api = context.api;
const cases = [
  ['古い家の中で蛇を見つけた。怖かったけれど、蛇は金色に光っていて、逃げずにこちらを見ていた。', ['不注意', '問題が解決', '休息', '再生'], ['最後の光', '親戚の死', 'ナッツ', '蛇から逃げ']],
  ['古い家の中で蛇を見つけた。怖かった。最後に光が見えた。', ['不注意', '平穏', '最後の光', '休息'], ['親戚', '金運', '神によって']],
  ['古い家の中で蛇を見つけた。怖かった。', ['不注意', '問題が解決', '休息'], ['豊かさや平穏', '最後の光']],
  ['古い家を見た。', ['不注意'], ['休息', '平穏', '問題が解決']],
  ['光が見えた。最後に古い家を見た。', ['不注意', '平穏'], ['最後の光']],
  ['水の中を泳いでいたら、橋の向こうに白い犬がいて、最後は空を飛ぶように逃げた。', ['感情面の課題に直面', '避けているものにまだ気づいていない', '身を守る', '別の段階へ移る'], ['空を飛ぶ場面', '逃げ切', '犬から逃げ']],
  ['朝起きたら、犬になっていて、バタートーストを食べた。味はしなかった。次のシーンでは猫になって魚を食べていた。', ['変化', '満たそう', '手応え'], ['ナッツ', '犬は守', '魚は幸運']],
  ['橋を渡って友達に会った。', ['次の段階', '取り入れたい'], ['仕事が成功', '恋愛運']],
  ['海で溺れたが、最後は助かった。', ['抱えきれ', '最後には助かる結末'], ['溺死', '回復や解放']],
  ['蛇を見た。', ['破壊', '再生'], ['死にます', '病気になる']],
  ['穏やかな水を眺めた。', ['落ち着き', '安心感'], ['感情が大きく揺れ']],
  ['荒れた水を泳いだ。', ['現実の問題が荒れ狂っている', '感情面の試練'], ['落ち着きや安心感']],
  ['白い犬はいなかった。', [], ['身を守る', '善良', '誠実']],
  ['パンを食べた。次の場面では味がしなかった。', ['満たそうとしているもの'], ['手応えの不足', '満足や手応えを得られない']],
  ['光は見えなかった。古い家を見た。', ['不注意'], ['平穏', '豊かさ']],
  ['古い家には入らなかった。', ['不注意'], ['休息', '問題が解決']],
  ['犬に追われた。最後は逃げ切った。', ['距離を置きたい', '無自覚'], ['自分を守ってくれる存在', '回復や解放', '安心した感覚']],
];

(async () => {
  await api.loadData();
  const loadedProfiles = api.state.readingProfiles;
  const profile = {id: 'selected-sense', term: '銀行', evidence: '古い表現', source: {sense_id: 'sense-a'}, claims: []};
  api.state.readingProfiles = [profile];
  const selected = {row: {term: '銀行'}, selectedSenseId: 'sense-a', grounding: {text: '別表現だが同じ選択語義'}};
  assert.equal(api.readingProfile(selected), profile, 'stable source sense id should survive translation wording changes');
  selected.selectedSenseId = 'sense-b';
  assert.equal(api.readingProfile(selected), undefined, 'profile must not leak to another sense with the same headword');
  api.state.readingProfiles = loadedProfiles;
  const bank = api.state.readingProfiles.find(p => p.id === 'bank-deposit-loss');
  const bankItem = {row: {term: '銀行'}, selectedSenseId: 'bank-sense', grounding: {text: bank.evidence},
    scene: '銀行に預金した。', sceneEvidence: ['銀行に預金した。']};
  api.state.readingProfiles = [{...bank, source: {...bank.source, sense_id: 'bank-sense'}}];
  assert.deepEqual(Array.from(api.meaningClaims(bankItem), c => c.id), ['deposit-loss']);
  bankItem.scene = '銀行からお金を引き出した。';
  bankItem.sceneEvidence = [bankItem.scene];
  assert.deepEqual(Array.from(api.meaningClaims(bankItem), c => c.id), ['withdrawal-trouble']);
  bankItem.scene = '銀行を見た。';
  bankItem.sceneEvidence = [bankItem.scene];
  assert.equal(api.meaningClaims(bankItem).length, 0, 'do not infer either bank action when omitted');
  api.state.readingProfiles = loadedProfiles;
  const original = JSON.parse(zlib.gunzipSync(fs.readFileSync(path.join(root, 'data/dream_terms.json.gz'))));
  const ids = new Set();
  for (const profile of api.state.readingProfiles) {
    assert(!ids.has(profile.id)); ids.add(profile.id);
    assert(profile.claims.length > 0);
    for (const claim of profile.claims) {
      assert(claim.id && claim.summary && /。$/.test(claim.sentence));
      assert(Array.isArray(claim.themes));
    }
    if (profile.source) {
      const quote = profile.source.quote.replace(/\s+/g, ' ').trim();
      assert(original.entries.some((e) => e.term === profile.source.orig && e.meanings.some((m) =>
        m.source_name === profile.source.name && m.text.replace(/\s+/g, ' ').trim().includes(quote))), profile.id + ': missing original');
      if (profile.source.master_only && !profile.source.sense_id) continue;
      const row = api.state.rows.find((r) => r.term === profile.term);
      const shard = JSON.parse(fs.readFileSync(path.join(dataRoot, `meanings-${String(row.shard).padStart(2, '0')}.min.json`), 'utf8'));
      const linkedSense = profile.source.sense_id
        ? shard[row.idx].some((s) => s.id === profile.source.sense_id && s.o === profile.source.orig &&
          s.s.includes(profile.source.name))
        : shard[row.idx].some((s) => s.o === profile.source.orig && s.s.includes(profile.source.name) &&
          s.m.some((m) => m.includes(profile.evidence)));
      assert(linkedSense, profile.id + ': source sense is not linked to the runtime entry');
    } else {
      assert(profile.evidence === api.REVIEWED_SENSES[profile.term]?.text ||
        (profile.term === '水' && ['water-calm', 'water-rough'].includes(profile.id)), profile.id + ': changed reviewed meaning');
    }
  }
  const reports = [];
  for (const [text, include, exclude] of cases) {
    const ctx = api.buildContext(text), items = api.findMatches(ctx);
    await api.attachMeanings(items, ctx);
    const reading = api.composeReading(items, text, ctx), plan = api.buildReadingPlan(items, ctx);
    for (const needle of include) assert(reading.includes(needle), `${text}: missing ${needle}\n${reading}`);
    for (const needle of exclude) assert(!reading.includes(needle), `${text}: unexpected ${needle}\n${reading}`);
    assert(!/場面に合う辞書|というものです|自分自身を集め|問題が問題|振り返って|どんな感覚|〈|「/.test(reading), reading);
    assert(reading.length < 1000, 'Report should not exhaustively repeat every meaning');
    for (const block of plan) for (const claim of block.claims) {
      assert(items.some((item) => item.row.term === claim.term && api.meaningClaims(item).some((c) =>
        c.id === claim.claimId && c.profileId === claim.profileId && c.evidence === claim.text)));
    }
    if (process.argv.includes('--examples')) console.log(JSON.stringify({text, reading, terms:items.map(i=>i.row.term)}));
    reports.push(reading);
  }
  if (process.argv.includes('--write-reports')) {
    const directory = path.join(root, '.playwright-cli', 'reading-reports');
    fs.mkdirSync(directory, {recursive:true});
    reports.forEach((reading, index) => fs.writeFileSync(path.join(directory, `${index + 1}.md`), reading + '\n'));
  }
  console.log(`${cases.length} report cases and ${ids.size} semantic profiles verified.`);
})().catch(error => { console.error(error); process.exitCode = 1; });
