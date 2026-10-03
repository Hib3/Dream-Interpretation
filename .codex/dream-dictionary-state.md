# Dream Dictionary Rebuild State

## Project goal
原典の意味・文化・出典を保持した日本語masterを作り、QA通過分から既存サイト互換のruntime辞書を再生成する。
Phase 1では工場の実装と代表サンプル検証まで。全量LLM処理、production置換、サイト公開は行わない。

## Current phase
Phase 1 COMPLETE。MODEL CHANGE REQUIREDで停止。Phase 2は未承認。
PHASE_1_GATE: PASS
PRODUCTION_GATE: BLOCKED (new dictionary diagnostic prose handoff, full processing and final QA pending)
要求モデル: GPT-6 Astra / Middle。実行中モデル・推論強度の実効値は未確認。

## Git / safety
- Branch: codex/dictionary-rebuild
- Rollback/base commit: 650ffb950c875318d27085b5f48567c550b98165
- 開始時の既存未追跡ファイル: .playwright-cli/。削除・上書きしない。
- data/raw/, data/dream_terms.json.gz, data/source_registry.json, data/ja/ は原本として保全する。
- 新パイプラインは別出力ディレクトリを使用する。

## Completed tasks
- Git状態、raw inventory、原データと出典のschemaを確認。
- scripts/translate_dictionary.py / build_ja_dictionary.pyを調査。
- 専用branch作成。
- 全量の決定的監査: .codex/artifacts/legacy-audit.json。
- dictionary_pipeline.py: SQLite checkpoint / content cache / strict schema / source span coverage / QA / staging converter。
- dictionary_sample.py: 実原典50件+重複・不正fixture2件を抽出。
- evaluate_dictionary_sample.py: 会話内で原文を読んで作成した訳例を通した。実原典50件中36件通過、14件隔離。不正fixture1件も隔離。重複は1件に統合。
- dictionary_codex_runner.py: ChatGPT認証のCodex CLIを利用。APIキーを渡さず、逐次・少数batch・Phaseモデル照合。自動の再送なし。
- 実transport: .codex/transport-work の英語/tr/zh-Hant/my計4件が正常完了。応答を原文と照合済み。
- app.js: master_schemaのある新辞書では旧手書きREVIEWED_SENSESで本文を上書きしない。旧辞書の動作を維持。

## Discovered problems
- PIPELINE_ISSUE: 翻訳対象が先頭2meaningに限定され、原文も280文字目安で文単位に削減される。
- PIPELINE_ISSUE: 日本語meaning220文字・term60文字・original48文字・4sense・3meaning・2source上限。
- PIPELINE_ISSUE: 先頭36文字一致を重複とみなす。newline順序でbatch訳を対応させる。
- TRANSLATION_ISSUE: termをmeaningと独立翻訳し、多義語の文脈が失われる。
- SEMANTIC_AMBIGUITY: 吉凶は単語数で決定され、mixed/unknownを保持しない。
- 実測: source entries 79,114 / source meanings 86,073 / 日本語見出し58,577。
- source言語別: en 10,437 (14,968 meanings), tr 67,225 (69,653), zh-Hant 996 (996), my 456 (456)。
- 先頭2meaning制限で2,317意味文が除外。旧原文前処理で12,502意味文が短縮、計11,120,111文字減少。
- 日本語本文80,709件、語義76,355件。先頭48文字による原語照合で未対応2,759項目（推定指標であり欠落確定数ではない）。
- zh-Hantは全件term=meaning、7文字988件・5文字8件。序詩の混入を代表sampleで確認。
- myも全456件term=meaning。全件に集約出典2件あるが直接meaning出典は1件。既存mergeで出典関係が失われている。

## Architecture decisions
- Windows上でstdlib Pythonを中心に実装。ユーザー回答によりCodex利用枠内に固定。API課金なし。Phase 1で全量処理しない。
- 単位は原典のterm + 完全なmeaning + source provenance。原文はimmutable。
- masterとruntimeを分離する。cache keyからmodel名を除外し、意味入力とprompt/schema versionを含める。
- SQLite checkpointとcontent-addressed responseを使用し、batchはID集合を照合する。

## Data schema / cache / checkpoint
- schema: dream-master-1 / prompt: faithful-ja-1。source raw全文、source_aliases、aggregate_sources、originsをmaster JSONLへ保持。
- response: results[id,status,action,reason,senses]。各senseはheadword, aliases, ordered source_quote+ja units, tone, ambiguity, confidences, flags。
- cache: checkpoint.sqlite cache(key,payload,checksum,model)。keyはnormalized source + prompt/schema version + pass、review時はcurrent/flagsも含む。model名は含まない。
- checkpoint: entries / batches / events / meta。active snapshotで変更・削除済み原典をproductionから除外し、旧cacheは保持。
- request/response JSONはbatch hash名、対応はID集合照合。応答未保存の中断はUNCERTAIN_DISPATCHで停止。
- production形式は6要素のindex tupleとmeaning shardsを維持。metadataはsenseの追加field。出典別senseは統合しない。

## Important paths
- scripts/translate_dictionary.py / scripts/build_ja_dictionary.py: 旧pipeline（比較用に保存）
- data/dream_terms.json.gz: 既存の原語統合データ
- data/source_registry.json: 原典情報
- data/raw/: 取得済みraw
- app.js: loaderと診断処理
- scripts/test_interpretation.cjs / scripts/test_reading_report.cjs: 既存回帰テスト

## QA thresholds
schema failure >0.1%、unexpected language >0.5%、ID mismatch >0%、empty translation >0.2%、systematic suspicious translation >2%で停止。cache破損・checkpoint不整合は即停止。
採用条件・分母・少数batchでの扱いを実装時に明記する。未確認項目は合格にしない。

## Tests / metrics / commands
- python -X utf8 -m unittest discover -s scripts -p test_dictionary_pipeline.py -q: 29件PASS。
- node scripts/test_reading_report.cjs: 16 case / 22 profile PASS。
- python -X utf8 scripts/dictionary_sample.py: 52件生成済み。
- python -X utf8 scripts/evaluate_dictionary_sample.py: .codex/sample-work/evaluation.json、.codex/staging-sample生成済み。
- sampleの再実行時は新しい --out を指定する（非空出力先への上書き禁止）。
- python -X utf8 scripts/dictionary_codex_runner.py --work .codex/transport-work --model gpt-6-astra --limit 4 --max-batches 1 --usage-checked: 実4件PASS。
- scripts/test_dictionary_runtime.cjs: loader35語・全shard参照・検索取得4case PASS。「季節が夏」と「夏だった。」の条件照合を修正。冬・否定文の誤一致なし。
- 同テストの --require-diagnosis は想定どおりFAIL。4caseとも旧meaningClaimsが新しい訳を診断文へ採用できず保留文になる。本番反映前の必須修正として残す。
- node scripts/test_interpretation.cjs: 18実辞書case、28条件照合、15原典確認などPASS。
- python -X utf8 scripts/check_dictionary_phase1.py: Phase 1基盤gate PASS、production_ready=false。根拠 .codex/artifacts/phase1-verification.json。
- 全量用台帳: 86,073 meaning records。cached complete4、pending86,031、長文unresolved38。全量用LLM呼び出し0。
- 全量用master.jsonl / unresolved.jsonl / metrics.jsonを書き出し済み。raw inventory全18ファイルのhash一致、Git上data差分なし。
- Playwrightで1366x900と390x844を確認。sample35語のload、夏の検索、日本語本文・出典表示、横幅overflowなし、console error0。診断本文は上記未完。
- screenshots: .playwright-cli/dictionary-staging-desktop.png / dictionary-staging-mobile.png。
- 日本語skill lint: DICTIONARY_REBUILD.mdはfinding0、DICTIONARY_AUDIT.mdは文長リズムwarning1。数値中心の監査文として意図的に維持。

## Known limitations
- 旧translation cacheはREADMEが外部パスを要求しており、リポジトリ内では現時点で未発見。
- 現行sourceのmerge以前に失われた重複出典の対応は、原データからの復元可能性を監査する。
- transportモデル指定はgpt-6-astra/medium。CLI応答で実効モデルが証明されていないためmaster.model_effective=nullを維持。
- 手作業の訳例再生とlive transportを区別する。独立した言語別精度測定は未完了。
- 単語・数値検査は意味忠実性の証明ではない。ランダム監査とflagged second passが必要。
- QA false positiveのreview keepはSOURCE_QUALITY_ISSUEのみ承認可能。他flagは修正か隔離。閾値の解除には根拠をeventsに記録する。
- retry管理、metrics、README/architecture文書、追加テストを実装・検証済み。判断不能なtransportは自動再送しない。
- 18,000文字超38件は原文付き隔離。長文の抽出確認・lossless segmentation migrationは未実装で、Phase 2以降の確認対象。
- 新辞書の診断文接続はCriticalな公開阻止条件。Phase 1の工場gateと最終production gateを分離する。検証結果にproduction_ready=falseを明記した。
- 実ブラウザQAではSWがPlaywrightのrouteを迂回するため、scripts/serve_dictionary_staging.pyで実URLをstagingへ対応させた。旧SWの本番更新不具合と誤認しない。

## Last successful operation
2026-10-03: check_dictionary_phase1.py完了、現行data不変、Phase 1全gate確認。全量翻訳・production置換・公開なし。

## NEXT ACTION
停止。ユーザーがLuna Middleへ変更して「続けて」と指示するまで、Phase 2を開始しない。
その後は本state、git diff、DICTIONARY_REBUILD.md、pipeline/runnerを確認し、利用枠の実残量を確認する。
1. python -X utf8 scripts/dictionary_pipeline.py activate-phase2 --confirmation USER_REQUESTED_CONTINUE_WITH_LUNA
2. python -X utf8 scripts/dictionary_codex_runner.py --work .codex/dictionary-work --model gpt-6-luna --limit 8 --max-batches 1 --usage-checked
3. 100件→500件の段階でschema/意味/停止率を調べ、利用枠を確認しつつ全量、QA、flagged review、言語別random auditへ進む。
4. 診断文接続を修正し、staging runtimeテスト --require-diagnosis を必ず通す。全量のPC/スマホQAと配備確認が終わるまでは最終READY禁止。
全量手順・cache・resume・build・長文隔離の制約は DICTIONARY_REBUILD.md。既存rawや正常cacheを削除しない。

## Usage reset wait checkpoint
- 2026-10-03 13:37 JST: primary used92% / remaining8%、weekly used14%。ordinaryUsageAllowed=true。
- 次の原因分析・QA補強・文書化のまとまった処理で4%を割る可能性があるため、ここで安全に待機。
- primary resetsAt=1791017961。時計UTC 2026-10-03 04:37:26からreset+60秒まで計算してclock.sleep。
- 全ツールセッション完了済み。production未変更。未完了のままモデル切替通知を出さない。
- 待機終了/入力割込み後は時計とget_usage_limitsを再取得し、実回復を確認してNEXT ACTIONへ戻る。
- 2026-10-03 18:00 JST、短期used0%・利用可能を確認して再開済み。リセット権・有料クレジットは使用していない。
