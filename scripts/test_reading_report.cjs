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
vm.runInContext(fs.readFileSync(path.join(root, 'app.js'), 'utf8').split('/* ---------- 占い師の語り')[0] +
  '\nrenderDictSuggest=()=>{};globalThis.api={state,loadData,buildContext,findMatches,attachMeanings,composeReading,buildReadingPlan,meaningClaims,REVIEWED_SENSES};', context);
const api = context.api;
const cases = [
  ['古い家の中で蛇を見つけた。怖かったけれど、蛇は金色に光っていて、逃げずにこちらを見ていた。', ['不注意', '平穏', '休息', '再生'], ['最後の光', '親戚の死', 'ナッツ', '蛇から逃げ']],
  ['古い家の中で蛇を見つけた。怖かった。最後に光が見えた。', ['不注意', '平穏', '最後の光', '休息'], ['親戚', '金運', '神によって']],
  ['古い家の中で蛇を見つけた。怖かった。', ['不注意', '問題が解決', '休息'], ['豊かさや平穏', '最後の光']],
  ['古い家を見た。', ['不注意'], ['休息', '平穏', '問題が解決']],
  ['光が見えた。最後に古い家を見た。', ['不注意', '平穏'], ['最後の光']],
  ['水の中を泳いでいたら、橋の向こうに白い犬がいて、最後は空を飛ぶように逃げた。', ['状況を動かそう', '離れたい', '身を守る', '次へ移る'], ['空を飛ぶ場面', '逃げ切', '犬から逃げ']],
  ['朝起きたら、犬になっていて、バタートーストを食べた。味はしなかった。次のシーンでは猫になって魚を食べていた。', ['変化', '充足', '手応え'], ['ナッツ', '犬は守', '魚は幸運']],
  ['橋を渡って友達に会った。', ['次の段階', '取り入れたい'], ['仕事が成功', '恋愛運']],
  ['海で溺れたが、最後は助かった。', ['抱えきれ', '回復や解放'], ['溺死']],
  ['蛇を見た。', ['破壊', '再生'], ['死にます', '病気になる']],
  ['穏やかな水を眺めた。', ['落ち着き', '安心感'], ['感情が大きく揺れ']],
  ['白い犬はいなかった。', [], ['身を守る', '善良', '誠実']],
  ['パンを食べた。次の場面では味がしなかった。', ['心を満たし'], ['手応えの不足', '満足や手応えを得られない']],
  ['光は見えなかった。古い家を見た。', ['不注意'], ['平穏', '豊かさ']],
  ['古い家には入らなかった。', ['不注意'], ['休息', '問題が解決']],
  ['犬に追われた。最後は逃げ切った。', ['距離を置きたい', '回復や解放'], ['自分を守ってくれる存在']],
];

(async () => {
  await api.loadData();
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
      assert(original.entries.some((e) => e.term === profile.source.orig && e.meanings.some((m) =>
        m.source_name === profile.source.name && m.text.includes(profile.source.quote))), profile.id + ': missing original');
      const row = api.state.rows.find((r) => r.term === profile.term);
      const shard = JSON.parse(fs.readFileSync(path.join(root, `data/ja/meanings-${row.shard}.min.json`), 'utf8'));
      assert(shard[row.idx].some((s) => s.o === profile.source.orig && s.s.includes(profile.source.name) &&
        s.m.some((m) => m.includes(profile.evidence))), profile.id + ': changed translation');
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
