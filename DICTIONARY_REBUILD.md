# 日本語夢辞書の再構築

対象は辞書データの保守担当者と、Phase 2を引き継ぐCodexです。最初に「原典と生成物を分ける」「Phase 2を開始する」を読み、以後は必要な手順を参照してください。進捗の正本は `.codex/dream-dictionary-state.md` です。ここに書かれた予定を、実行済みの結果と混同しないでください。

## 原典と生成物を分ける

旧サイトの原典は79,114項目、意味文は86,073件です。翻訳の単位を「原語の見出し＋完全な意味文＋直接の出典」に変更しました。見出しだけを先に訳しません。Pythonが正規化・完全一致重複・保存・検査を担当し、Codexが翻訳・語義判定・意味単位分割・吉凶分類を担当します。

原典の宗教観、予言、否定、条件、数字を保持します。科学的な説明への置換や、夢占いらしい内容の補作はしません。出典の利用条件は元データの記載を保持します。ライセンス未確認を利用許諾済みに変更する処理はありません。

| 種類 | 保存先 | 扱い |
|---|---|---|
| 元データ | `data/dream_terms.json.gz`, `data/raw/`, `data/source_registry.json` | 変更しない |
| 現行配信辞書 | `data/ja/` | Phase 1では変更しない |
| 進捗・cache | `.codex/dictionary-work/checkpoint.sqlite` | WAL、同期書込、整合性検査 |
| 送受信 | 同ディレクトリの `requests/`, `responses/`, `transport/` | batch ID別、再開時に利用 |
| 全metadata | 同ディレクトリの `master.jsonl` | 原文、訳、語義、QA、処理履歴 |
| 隔離 | 同ディレクトリの `unresolved.jsonl` | 原文・理由・試行数を保持 |
| 配信用候補 | `.codex/staging-<版>/` | QA通過分のみ。既存ディレクトリへの上書き禁止 |

SQLiteは実行時の正本、JSONLは再生成できる書き出しです。作業ディレクトリはGit対象外です。バックアップ時は処理を止めてSQLiteのbackup APIを使うか、接続終了後にディレクトリごと保存してください。稼働中のDB本体だけをコピーしないでください。

## 原文の欠落を拒否する

schemaは `dream-master-1`、promptは `faithful-ja-1` です。応答の各senseには、日本語見出し、同義語、吉凶、曖昧さ、二種類のconfidence、flags、意味単位を持たせます。各単位は `source_quote`, `ja`, `kind` からなります。

`source_quote` を順番に連結すると、正規化後の原文全体を覆う必要があります。末尾欠落・順序変更・引用の創作は拒否します。ただし、引用が揃っていても訳が忠実とは限りません。意味の検査は別途必要です。

masterには正規化前の原文、直接出典、集約出典、抽出位置、原語aliasも保存します。直接の意味文と結び付いていない集約出典を、直接出典として表示しません。

18,000文字を超える原文は全文のまま隔離します。現在38件が該当します。これは切り詰めではなく、長文の抽出品質・分割方針を確認するための保留です。自動で部分訳を公開しません。原典を調査して分割する場合は、親ID・全source spanとの対応を保つmigrationが必要です。この長文用migrationは未実装です。

## Cacheはモデル変更だけでは捨てない

keyは正規化した原語・全文・言語・直接出典とprompt/schema version、処理passのhashです。reviewには現在の訳とflagsも含みます。モデル名は含みません。保存結果にはchecksumを付け、同一keyへの異なる応答は黙って上書きしません。

README、frontend、shard数、書式だけの変更でversionを上げないでください。意味に関わる規則を変える場合だけ、対象を明示したmigrationを行います。既存checkpointでkeyが変わった場合は停止します。正常な訳の全面再生成はしません。

同じ作業ディレクトリで `prepare` を再実行すると、未変更の訳を残し、新規・変更された原典だけをpendingにします。削除された原典はactive対象から外し、過去のcacheは残します。入力は追加分だけでなく、現在の原典snapshot全体を渡してください。

検証済みの別作業からcacheを移す場合:

```powershell
python -X utf8 scripts/dictionary_pipeline.py import-cache --from-work .codex/transport-work
```

会話内で作成した `representative-translations.json` は評価用の訳例です。独立した精度評価でも、Lunaの実測でもありません。production用cacheへ無条件に取り込まないでください。実transportの4件は別に検証し、production用checkpointへ取り込み済みです。

## Phase 2を開始する

**この手順はPhase 1完了後、ユーザーがLuna Middleへ変更して「続けて」と指示してから実行します。Phase 1で実行しません。**

state、`git diff`、本書、pipelineを確認し、`get_usage_limits` で実際の枠を確認します。残量約4%を保持し、reset後は時計だけでなく残量回復を確認します。CLI runnerは利用枠を読み取る機能を持たず、監督するCodexが確認します。`--usage-checked` は確認の代わりにはなりません。

```powershell
python -X utf8 scripts/dictionary_pipeline.py activate-phase2 --confirmation USER_REQUESTED_CONTINUE_WITH_LUNA
python -X utf8 scripts/dictionary_pipeline.py prepare
python -X utf8 scripts/dictionary_codex_runner.py --work .codex/dictionary-work --model gpt-6-luna --limit 8 --max-batches 1 --usage-checked
```

最初の100件、続いて500件まで少数batchで進め、各区切りで失敗率と実際の文章を確認します。その後も1〜5batchごとにrunnerが終了するため、共有利用枠を確認して次へ進めます。モデルはLuna固定、推論設定はmediumです。認証はChatGPTに限定し、APIキーを環境から除外します。

CLIは固有のsystem contextを持ちます。4件の実測では入力20,119 token、出力668 tokenでした。課金APIは使っていませんが、利用枠を消費します。実効モデル名が応答から確認できない場合は `model_effective=null` のままにし、要求したモデル名と区別します。

## 停止理由を確認してから再開する

`pending / processing / complete / failed / needs_review / unresolved` を区別します。batchは全ID集合と重複を検査し、順序で対応させません。

```powershell
python -X utf8 scripts/dictionary_pipeline.py status
python -X utf8 scripts/dictionary_pipeline.py recover --model gpt-6-luna
```

応答ファイルが残っていれば再送せずに取込みます。応答がない場合は `UNCERTAIN_DISPATCH` で停止します。ネットワーク切断だけでは「処理されなかった」と判断できません。providerが推論前に拒否したことをログから確認できた場合だけ、次を使います。

```powershell
python -X utf8 scripts/dictionary_pipeline.py retry-unprocessed --batch <id> --confirmed-not-processed --reason "拒否理由と、推論前に停止したことを確認した具体的な証拠"
```

再送は最大3試行、意味QAは原則2passまでです。上限後は隔離します。completeへの再送は許可しません。

## QAは検出・再確認・隔離を分ける

空欄、schema、原文範囲、文字化け、原語漏れ、長すぎる見出し、文状見出し、不自然な反復、数字不一致、宗教語の脱落、URL/HTML、参照、confidence、曖昧さを検査します。分類prefixは `SOURCE_QUALITY_ISSUE / TRANSLATION_ISSUE / PIPELINE_ISSUE / SEMANTIC_AMBIGUITY` です。

閾値はschema 0.1%、言語異常0.5%、ID不一致0%、空訳0.2%、疑わしい訳2%。schema・空訳・ID不一致は1件でもそのbatch全体を拒否するため、閾値より厳しく停止します。言語・疑わしい訳はbatch内の対象ID数を分母にします。異なる原文5件以上が同一訳になるbatchも停止します。これは誤訳確定ではなく、調査開始の条件です。

停止解除には調査・修正内容を保存します。理由なしの解除や、同じ異常のまま大量継続をしないでください。

```powershell
python -X utf8 scripts/dictionary_pipeline.py qa
python -X utf8 scripts/dictionary_pipeline.py acknowledge-halt --reason "調査した原文、原因、修正内容と影響範囲を具体的に記録"
python -X utf8 scripts/dictionary_codex_runner.py --work .codex/dictionary-work --model gpt-6-luna --pass-kind review --limit 4 --max-batches 1 --usage-checked
python -X utf8 scripts/dictionary_pipeline.py random-audit --per-language 25
```

reviewは問題がある項目だけが対象です。`keep` は元のsenseを一切変えず、理由を必要とします。原文由来の問題は忠実な訳を維持して承認できますが、翻訳・pipeline・曖昧さのflagが残る場合は隔離します。

random auditはseed固定・言語別です。抽出しただけでは合格になりません。`random-audit.json` の `review_status` と `error_rate` は未確認を表します。Phase 2では原文と訳を読み、結果を別の監査記録へ保存してください。系統的な誤りがあれば検出規則を追加し、同系統を再確認します。

## 最終ビルドは別ディレクトリへ出す

```powershell
python -X utf8 scripts/dictionary_pipeline.py master
python -X utf8 scripts/dictionary_pipeline.py build --out .codex/staging-final-1 --shards 128
```

未処理・未確認・停止中のデータがあればビルドは失敗します。unresolvedはmasterへ残し、runtimeから除外します。runtimeは既存の6要素terms indexとmeaning shardsを保ち、source reference・tone全文類型・sense IDなどを追加します。異なる出典のsenseを潰しません。訳の文字数やsense数を切りません。

現行frontendには旧辞書専用の手書き補正があります。新しい `master_schema` を検出した場合だけ、その上書きを無効にします。旧URL・外観・旧辞書の動作は維持します。大規模な日本語生成アルゴリズムの変更は今回のPhase 1に含めません。

**確認済みの未完了事項:** 現行 `meaningClaims` は旧校閲profileと限定文型に依存しています。新しい訳を検索できても、診断文へ採用できず保留表示になる例があります。Phase 2のsite integrationでは、原典に忠実な訳を診断へ渡す接続を実装し、`node scripts/test_dictionary_runtime.cjs <staging> --require-diagnosis` を通す必要があります。loaderテストの成功だけではproductionへ進めません。

Phase 2の配信前には、stagingを実ブラウザでPC・スマホから検査し、読み込み、検索、診断、出典、console、性能を確認します。その後に `data/ja` を版単位で置換してGit commitを作り、公開と配信確認へ進みます。Phase 1でこの置換・公開は行いません。rollback基点は `650ffb950c875318d27085b5f48567c550b98165` です。

## テストと監査を再現する

```powershell
python -X utf8 scripts/audit_dictionary.py
python -X utf8 -m unittest discover -s scripts -p test_dictionary_pipeline.py -q
python -X utf8 scripts/dictionary_sample.py
python -X utf8 scripts/evaluate_dictionary_sample.py --out .codex/staging-sample-new
node scripts/test_dictionary_runtime.cjs .codex/staging-sample-new
node scripts/test_reading_report.cjs
node scripts/test_interpretation.cjs
```

`sample-work` は評価用checkpointです。実原典50件に、重複と不正データのfixtureを加えています。runtimeテストの35語という期待値はこの固定sample専用で、全量用の合格件数ではありません。

詳細監査は `.codex/artifacts/legacy-audit.json`、重大度と比較は `DICTIONARY_AUDIT.md` を参照してください。解決できない問題はstateの未解決事項へ原典IDと理由を記録し、次の担当者に引き継ぎます。

用語: masterは検査用の完全データ、runtimeはブラウザ配信用データ、senseは語義、meaning unitは出典範囲に対応する意味単位、checkpointは再開状態です。
