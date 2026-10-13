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

## 出典データを再取得・差し替える

トルコ語の追加元はsitemap上の57,993ページです。`scripts/extend_dream_terms.py --fetch-only --jobs 6` はページ本文を `data/raw/ruya_tabiri_pages.jsonl` に保存し、パーサーv2はページ内の非空段落を文字数で切らずに保持します。確認済みcacheは57,993/57,993ページ、289,965段落です。再取得後は、slug・URL・空本文を検査してから候補を作ります。

既存の15出典から作った基礎候補は `.codex/artifacts/dream_terms.base-v2.json`、トルコ語sourceを差し替えた候補は `.codex/artifacts/dream_terms.rebuilt-v2.json.gz` です。後者は79,222見出し・86,078意味文で、現行 `data/dream_terms.json.gz` とは別ファイルです。再生成時も入力と出力を必ず分けてください。

2026-10-13の構造比較では、現行79,114見出しはすべてv2に残り、v2に108見出しが追加されています。意味文の完全一致は旧86,073件/新86,078件中27,976件です。この差を直ちに欠落と解釈しないでください。トルコ語source `Rüya Tabiri GitHub Pages Dataset` は旧・新とも57,993件ですが、取得済み原文snapshotが更新されたため旧文との一致は0件でした。一方、v2の当該57,993 URLすべてを `data/raw/ruya_tabiri_pages.jsonl` と照合し、raw URL欠落0件・正規化後本文不一致0件を確認しています。これは新snapshotへの抽出忠実性を示すもので、翻訳品質や旧snapshotとの意味同等性を証明しません。その他出典の旧のみ104件・新のみ109件の差分は7出典に集中しており、処理前に個別照合します。

更新版checkpointのQAで、トルコ語の `bir türlü` / `bir süre`、`bir dediğini iki etmek` の自然な意訳、および日本語の否定表現「何一つ不足を感じず」が数値不一致と誤検出される実例を確認しました。原文の特定語法と訳文パターンが同時に一致する場合だけ除外し、閾値は変更していません。`scripts/test_dictionary_pipeline.py` は243件PASS。`ikinci bir şans` を「もう一度機会」とした別項目は数対応の疑義が残るため、二次レビュー後もunresolvedに隔離しています。

```powershell
python -B -X utf8 scripts/extend_dream_terms.py --fetch-only --jobs 6
python -B -X utf8 scripts/extend_dream_terms.py --input .codex/artifacts/dream_terms.base-v2.json --output .codex/artifacts/dream_terms.rebuilt-next.json.gz --replace-source
python -B -X utf8 scripts/migrate_dictionary_checkpoint.py --source .codex/artifacts/dream_terms.rebuilt-next.json.gz --from-work .codex/dictionary-work --to-work .codex/artifacts/dictionary-v3-work
```

checkpoint移行は新しい空の出力先へ行い、既存DBは残します。検証済みv2の再基準化DB `.codex/artifacts/dictionary-v2-work-rebased` は総数86,078、pending58,102です。これは未処理の作業候補であり、翻訳完了・production・公開済みではありません。旧完了verifierは86,073件を固定値としているため、source更新版の処理をproduction認定する前に、検証条件と実source snapshotを同じ版へ整合させる必要があります。基準件数を黙って合わせたり、未完了項目を除外してREADYにしたりしないでください。

`scripts/build_dream_terms.py` は `--help` を実装しておらず、起動すると既定で `data/dream_terms.json` と `data/dream_terms.min.json` を生成します。出力を確認してから実行し、現在のgzip辞書へ上書きしないでください。

## 原文の欠落を拒否する

master schemaは `dream-master-1`、現在のtransport task schemaは `dream-task-5`、promptは `faithful-ja-12` です。1 entryのsource meaning全体を一つの不変source segmentとして渡し、意味を保つ必要があれば複数sense/unitへ分けられます。batch内の輸送IDは `i0001` のような短い連番で、内部の64桁cache keyは非公開の `bindings.cache_key` に保持します。応答はこの短IDで照合し、順序・近似一致には頼りません。従来の保存済みbatch（長いID）も個別の内部cache keyがない場合は後方互換で読めます。トルコ語namazは礼拝と訳します。文化的な語彙・食物名・参照見出しも、日本語の定着語またはカタカナ転写で出力し、原語綴りを日本語文中に残しません。応答には日本語見出し、同義語、吉凶、曖昧さ、二種類のconfidence、flags、意味単位を含めますが、provenance IDや原文quoteは要求しません。Pythonがentry IDで対象を特定し、すべてのunitへ当該source meaning全文を付与してmasterの `source_quote`, `ja`, `kind` を生成します。entry間のsource quote対応は決定的に検証し、意味単位の欠落や空訳は拒否します。

以前は1 meaningを約320文字ごとに機械分割していましたが、モデルが後続segmentの内容を先頭unitへまとめ、末尾unitを空にする事例を2件確認しました。現在は固定文字数で意味を割らず、18,000文字超の原文は全文のまま隔離します。該当する旧分割の訳は `requeue-legacy-segmented` で再処理待ちにし、試行上限のentryは旧訳をcacheに保持してunresolvedへ隔離します。

source coverageはentry IDに結び付けてローカルで構築し、モデルが別entryのunit IDを返す形の誤結合を防ぎます。entry全体のsource segmentが正規化後の原文全体を覆う必要があります。翻訳batchだけでなくreview batchも、保存応答からID単位でschemaと内容を個別検査し、有効項目だけ確定、無効項目だけ元の状態へ戻せます。IDが欠落・重複した場合も、完全一致で一意に対応する応答だけを救済し、欠落または重複のentryは再queueします。余分なIDは監査記録へ残して無視します。`status=unresolved`、理由あり、空sensesの応答では、冗長な `action` 欄だけをPythonが `unresolved` に正規化します。senseが残る・理由が空など内容不整合は引き続き拒否します。意味scope自体に疑義があればbatch全体を `requeue-batch` でpendingへ戻します。部分救済・再queueの理由とID内訳はDBへcommitします。IDの近似一致・並び順対応はしません。原文範囲が揃っていても訳の忠実性は別途QAします。

決定的QAは複数unitを原文全体の対訳として数値・宗教語確認し、unitごとの分割で欠落と誤認しないようにします。トルコ語の時間名詞は語境界を限定した形態パターンで照合します。cross-reference内の引用原題は、通常の説明文に混じる原語漏れとは区別します。日本語見出しの文状判定では `ます` 終止だけを根拠にせず、「ことを意味」などの文構造と長さを確認し、`励ます` のような動詞見出しを誤って止めない回帰テストを設けます。

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
python -X utf8 scripts/dictionary_codex_runner.py --work .codex/dictionary-work --model gpt-6-luna --limit 80 --max-chars 18000 --max-batches 20 --workers 4 --usage-checked
```

最初の約100件、続いて500件まで進め、各ゲートで失敗率と実際の文章を確認します。`--max-chars` は1 batchの直列化入力文字数を制限し、既定値・production上限とも18,000です。36,000では46件中17件しか応答されず、個別IDで17件を救済、残り29件を再queueしたため、出力完備性を優先して18,000へ戻しました。逐次runnerが遅かったため、独立したcheckpoint予約を使う `--workers 4` を採用しました。計測した3〜4並列の範囲で、4並列が最も高い投入量でした。SQLiteの予約は直列化し、品質停止後の新規予約を禁止します。停止時にすでに実行中の他batchは完了を待ち、その応答も個別QAされます。最初の停止理由を保持し、停止に遭遇したrunnerは非0終了します。各batchはID・空unit・言語・疑わしい訳のquality gateを通過してから新規投入します。QAで第三の目など日本語の序数を落としていたため数値認識を追加し、回帰テストで確認しました。各起動前に共有利用枠を確認し、4%を保持します。モデルはLuna固定、推論設定はmediumです。認証はChatGPTに限定し、APIキーを環境から除外します。

pipeline/prompt migration後は5件でsource-to-Japanese scopeを検証してから100件gateへ拡大します。各batchはID・空unit・言語・疑わしい訳のquality gateを通過してから次へ進みます。

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

処理未成立のschema/source-unit不一致は保存済み応答をIDごとに検査し、有効項目だけ確定、無効項目だけ再キューします。再送は最大3試行、意味QAは原則2passまでです。上限後は隔離します。completeへの再送は許可しません。

一つのresponse unitへ複数source範囲が混ざったなど、segment scope自体が不適切な場合は部分salvageしません。保存応答全体を追跡可能にしてから該当batchをrequeueし、影響する旧訳もprompt migrationで再生成します。試行上限に達した旧訳はproductionから除外してunresolvedへ移します。

部分salvageのcache keyは短いtransport IDでなくbinding内のcontent-addressed keyを使います。旧salvage batchに限る誤キー修復は`repair-salvaged-cache --batch <id>`で行い、salvage監査イベント、payload ID、master schema、checksumを確認してからtransaction内で移します。

## QAは検出・再確認・隔離を分ける

空欄、schema、原文範囲、文字化け、原語漏れ、長すぎる見出し、文状見出し、不自然な反復、数字不一致、宗教語の脱落、URL/HTML、参照、confidence、曖昧さを検査します。分類prefixは `SOURCE_QUALITY_ISSUE / TRANSLATION_ISSUE / PIPELINE_ISSUE / SEMANTIC_AMBIGUITY` です。

閾値はschema 0.1%、言語異常0.5%、ID不一致0%、空訳0.2%、疑わしい訳2%。schema・空訳・ID不一致は1件でもそのbatch全体を拒否するため、閾値より厳しく停止します。言語・疑わしい訳はbatch内の対象ID数を分母にします。統計の `schema_failure_rate` はschema異常を含む拒否batch内の処理対象数の割合であり、個々の不正応答unitの割合とは異なります。拒否batchは部分救済後も履歴に残るため、全量完了時は拒否batch数と現在のentry状態を併記します。意味確信度不足は誤訳率とは分けて要レビューへ送り、それだけでは全体停止としません。異なる原文5件以上が同一訳になるbatchも停止します。これは誤訳確定ではなく、調査開始の条件です。

停止解除には調査・修正内容を保存します。理由なしの解除や、同じ異常のまま大量継続をしないでください。

```powershell
python -X utf8 scripts/dictionary_pipeline.py qa
python -X utf8 scripts/dictionary_pipeline.py acknowledge-halt --reason "調査した原文、原因、修正内容と影響範囲を具体的に記録"
python -X utf8 scripts/dictionary_codex_runner.py --work .codex/dictionary-work --model gpt-6-luna --pass-kind review --limit 4 --max-batches 1 --usage-checked
python -X utf8 scripts/dictionary_pipeline.py random-audit --per-language 25
```

reviewは問題がある項目だけが対象です。`keep` は元のsenseを一切変えず、理由を必要とします。原文由来の問題は忠実な訳を維持して承認できますが、翻訳・pipeline・曖昧さのflagが残る場合は隔離します。

数字QAは出現回数ではなく数値集合を照合し、アラビア数字の先頭ゼロ（`078` / `78`）を正規化します。英語の数量単位句 `a day`（1日）と日本語の「一日」も対応づけます。日本語の漢数字＋助数表現（「二種類」「四十日」「二通り」など）と、トルコ語の「iki katına」「dört veçhe」「iki vech」「iki âlim erkeğe」「iki elbise」「iki parçaya」「iki memleket」「bir sene」、明示的な数詞列挙、`-larından/-lerinden biri/birini`（〜のうち一つ）と、`iki çeşit ... biri ... diğeri`（二種類の一方ともう一方）を扱います。日本語の「いったん」はトルコ語 `bir kere`（一度）と照合し、「いつか」は `bir gün` の「いつの日か」という用法に対応させ、数量の「一つ」と誤認しません。トルコ語 `tekrar`（再び）だけで、日本語の対応表現「もう一度」を数値の1として扱いません。日本語の「十分に／十分な」（十分量・充分さ）は「10分」と誤認せず、時間単位「十分間」は数値として照合します。トルコ語原文が `yalnız` / `tek başına`（一人で、単独で）を表す場合、日本語の対応表現「一人で／一人きり」を数量「1」と誤判定しません。「katına」（倍）など数量用法は扱いますが、「katkı」（貢献）を数量のkatと誤認しません。出典見出し語の逐語引用や、「原文の末尾は『…』で途切れている」という説明内の原文引用は漏出と区別しますが、意味曖昧性・低信頼・原文不完全文のフラグは残します。「dört elle（懸命に）」「bir husus（ある事柄）」や単独の冠詞「bir」は数値として数えません。トルコ語の「ayı」は熊の対格形とも一致するため月の数詞形態から除外し、曖昧な格変化を一律に数えません。文末句読点に付く番号、`[203] See ...` と `[119] For ...` の引用番号を除外しますが、本文中の `[203] coins` は数値として維持します。原典に数値がない場合、日本語の漢数字だけで数値不一致を起こさず、参照番号を除去した後に残るアラビア数字を検出します。数値flagは原典数詞と訳文の対応を示す警告で、second passで原文照合します。

英語の解釈文で複数の文境界に反復する `1.` は列挙ラベルとして数値QAから除外します。単独の数量表現（例: `1 apple`）は引き続き数値照合します。

英語の解釈文で複数の文境界に繰り返す `1.` は列挙ラベルとして数値QAから除外します。単独の数量表現（例: `1 apple`）は引き続き数値照合します。原文の不完全文について「原文の末尾は…で途切れている」「原文は最後の…で途切れている」「原文はここで…とだけ述べ、その先の関係が途切れている」と説明する定型引用は、source-language leakage と区別します。内容の欠落・曖昧性フラグは保持します。

日本語の反復語QAは部分文字列一致でなく語句境界を確認します（例: 「善と善行」は自然な複合語のため、「善と善」という断片一致だけで停止しません）。

second passで翻訳・pipeline・意味曖昧性の問題が解消され、`SOURCE_QUALITY_ISSUE`だけが残る場合、忠実な訳を維持できます。原典が矛盾・断片的で意味を安全に解釈できない場合はunresolvedのままです。

random auditはseed固定・言語別です。抽出しただけでは合格になりません。`random-audit.json` の `review_status` と `error_rate` は未確認を表します。Phase 2では原文と訳を読み、結果を別の監査記録へ保存してください。系統的な誤りがあれば検出規則を追加し、同系統を再確認します。

2026-10-13の固定seed監査40件の目視結果は `.codex/artifacts/dictionary-final-20261013-reviewed/random-audit-review.md` に記録しています。生成元JSONは監査処理がreview結果を記録しないため `not_reviewed` のままです。監査済みの根拠にはsidecarを使用し、生成JSONを書き換えて状態を偽装しないでください。

## 2026-10-13 QA・ステージング結果

決定的QAの追加後、Luna reviewをQA対象だけに実施しました。`を主る` を日本語QAで検出し、修正できたものだけ採用、判断不能なものはunresolvedへ隔離します。最新状態・完全な数値は `.codex/dream-dictionary-state.md` が正本です。

再現コマンド:

```powershell
python -B -X utf8 scripts/dictionary_pipeline.py --work .codex/dictionary-work qa
python -B -X utf8 scripts/dictionary_codex_runner.py --work .codex/dictionary-work --model gpt-6-luna --limit 16 --max-chars 18000 --max-batches 20 --workers 4 --pass-kind review --usage-checked
python -B -X utf8 scripts/dictionary_pipeline.py --work .codex/dictionary-work build --out .codex/artifacts/dictionary-final-20261013-reviewed --shards 128
node scripts/test_dictionary_runtime.cjs .codex/artifacts/dictionary-final-20261013-reviewed --full --require-diagnosis
```

上記は初回ステージング時点の履歴です。2026-10-13時点では検証済みruntime辞書をローカルの `data/ja` へ統合し、旧ファイル一式を `.codex/artifacts/production-ja-backup-20261013` に保存しました。GitHub Pagesへのpush/deployは未実施です。未解決12,876件はruntimeから除外しています。公開判断ではstate記載のsource headword/meaning coverageと未解決率を確認してください。

## 最終ビルドは別ディレクトリへ出す

```powershell
python -X utf8 scripts/dictionary_pipeline.py master
python -X utf8 scripts/dictionary_pipeline.py build --out .codex/staging-final-1 --shards 128
```

未処理・未確認・停止中のデータがあればビルドは失敗します。unresolvedはmasterへ残し、runtimeから除外します。runtimeは既存の6要素terms indexとmeaning shardsを保ち、source reference・tone全文類型・sense IDなどを追加します。異なる出典のsenseを潰しません。訳の文字数やsense数を切りません。

意味プロファイルの元定義 `data/ja/reading-semantics.json` と生成runtime `data/ja/reading-semantics-runtime.json` は分離します。buildはruntimeファイルを生成し、旧runtime URL向けの `reading-semantics.json` もstaging内には出力します。frontendはruntimeファイルを優先し、404の場合に限って旧URLへfallbackします。production統合では元定義ファイルを上書きせず、runtimeファイルだけを追加します。

現行frontendには旧辞書専用の手書き補正があります。新しい `master_schema` を検出した場合だけ、その上書きを無効にします。旧URL・外観・旧辞書の動作は維持します。大規模な日本語生成アルゴリズムの変更は今回のPhase 1に含めません。

**確認済みの未完了事項:** 現行 `meaningClaims` は旧校閲profileと限定文型に依存しています。新しい訳を検索できても、診断文へ採用できず保留表示になる例があります。Phase 2のsite integrationでは、原典に忠実な訳を診断へ渡す接続を実装し、`node scripts/test_dictionary_runtime.cjs <staging> --require-diagnosis` を通す必要があります。loaderテストの成功だけではproductionへ進めません。

Phase 2の配信前には、stagingを実ブラウザでPC・スマホから検査し、読み込み、検索、診断、出典、console、性能を確認します。その後に `data/ja` を版単位で置換してGit commitを作り、公開と配信確認へ進みます。Phase 1でこの置換・公開は行いません。rollback基点は `650ffb950c875318d27085b5f48567c550b98165` です。

## 2026-10-13 最終ローカル統合

最終候補 `.codex/artifacts/dictionary-final-20261013-release`（build `48a841d2077cd346`）を `data/ja` に統合しました。57,321 runtime terms、128 shards、79,114 source entries、73,538 sensesです。元の意味プロファイル定義は維持し、生成runtimeを別ファイルで配信しています。旧runtime形式へのfallbackも検証しました。

固定seed監査は100件（各言語25件）。zh-Hantで不自然な逐語訳1件を修正し、与え手・受け手の逆転が疑われた1件は原典照合後も訳を確定せず隔離しました。観測された事前修正問題率2%はこの標本内の値で、全体誤訳率の推定ではありません。QAフラグ率53.66%には原典品質・意味曖昧性も含まれるため、翻訳誤り率とは区別します。

確認: Python 256 tests、実辞書診断18シナリオ、report 17ケース/28 semantic profiles、production runtime loader/search/diagnosis PASS。実ブラウザで例文の5候補診断、390px幅の横overflowなし、console error 0を確認済みです。未解決12,876件はruntimeから除外し、隔離理由を保持しています。2026-10-13にGitHub Pagesへ公開し、index/app/runtime profile/shardのHTTP 200と辞書件数を確認しました。初回artifactがリポジトリ全体を含み、`.codex`や開発用sourceも公開されていたため、workflowをサイト必須ファイルのみのallowlist artifactへ修正して再公開中です。再公開後、内部パスが404になることを確認するまでセキュリティ是正は未完了です。

## テストと監査を再現する

全量指標は `.codex/dictionary-work/metrics.json`、検査用masterは `master.jsonl`、隔離項目は `unresolved.jsonl` にあります。最終未解決12,876件の言語内訳はtr 11,132 / en 1,353 / zh-Hant 208 / my 183で、9,727件は `Rüya Tabiri GitHub Pages Dataset` に集中しています。flagsは重複計上されるため各分類を足して未解決件数と比較しないでください。未解決JSONLは公開runtimeには含みません。

```powershell
python -X utf8 scripts/audit_dictionary.py
python -X utf8 -m unittest discover -s scripts -p 'test_*.py' -q
python -X utf8 scripts/dictionary_sample.py
python -X utf8 scripts/evaluate_dictionary_sample.py --out .codex/staging-sample-new
$env:DICTIONARY_DATA_DIR = 'data/ja'
node scripts/test_dictionary_runtime.cjs .codex/staging-sample-new
node scripts/test_dictionary_runtime.cjs data/ja --full --require-diagnosis
node scripts/test_reading_report.cjs
node scripts/test_interpretation.cjs
```

`sample-work` は評価用checkpointです。実原典50件に、重複と不正データのfixtureを加えています。runtimeテストの35語という期待値はこの固定sample専用で、全量用の合格件数ではありません。最後の `--full` テストは生成済み本番辞書を明示して、全語義・読解profile・診断例を検査します。PowerShellではstagingテストより先に `DICTIONARY_DATA_DIR` を設定してください。

詳細監査は `.codex/artifacts/legacy-audit.json`、重大度と比較は `DICTIONARY_AUDIT.md` を参照してください。解決できない問題はstateの未解決事項へ原典IDと理由を記録し、次の担当者に引き継ぎます。

用語: masterは検査用の完全データ、runtimeはブラウザ配信用データ、senseは語義、meaning unitは出典範囲に対応する意味単位、checkpointは再開状態です。
