# Dream Interpretation Dataset

夢占い用の「夢の単語」と「意味」を対応させるJSONデータセットです。

公開アプリ: https://hib3.github.io/Dream-Interpretation/

現時点では、再利用条件を確認できた Project Gutenberg の夢占い古典4冊、Hugging Face 4件、公開ダウンロード可能な Kaggle 2件、GitHub JSON/CSV 4件、Wikisource `周公解夢` を抽出元にしています。

現行サイトや商用APIは `data/source_registry.json` に候補として記録します。独占的な著作権表記や `All rights reserved` がある本文は取得対象外にし、表記がないものは `no_license_unverified` として別扱いにします。

## Build

```powershell
python scripts/build_dream_terms.py
```

生成物:

- `data/dream_terms.json`: term単位に統合した meanings / sources 付きJSON
- `data/dream_terms.min.json`: GitHub Pages用の同内容minified JSON
- `data/raw/gutenberg_926_ten_thousand_dreams_interpreted.txt`: 取得した原文キャッシュ
- `data/raw/hf_samvlad_dream_decoder_dataset_rows.json`: 取得したHugging Faceデータキャッシュ
- `data/raw/hf_teragron_dream_interpretation_rows.json`: 取得したHugging Faceデータキャッシュ
- `data/raw/hf_n3rd0_dreambook_guanaco_rows.json`: 取得したHugging Faceデータキャッシュ
- `data/raw/hf_tolgadev_ruyatabirleri_ruya.csv`: 取得したHugging Faceデータキャッシュ
- `data/raw/github_ljt_one_dream_symbols_dataset.json`: 取得したGitHub JSONキャッシュ
- `data/raw/github_akmm_dev_dream_dictionary.json`: 取得したGitHub JSONキャッシュ
- `data/raw/github_sannlynnhtun_blazor_dream_dictionary_detail.json`: 取得したGitHub JSONキャッシュ
- `data/raw/github_makalin_somniumsage_dream_dataset.csv`: 取得したGitHub CSVキャッシュ
- `data/raw/kaggle_yuvrajsanghai_dream_dictionary.zip`: 取得したKaggleデータキャッシュ
- `data/raw/kaggle_manswad_dictionary_of_dreams.zip`: 取得したKaggleデータキャッシュ
- `data/raw/wikisource_zhougong_jiemeng_raw.txt`: 取得したWikisource原文キャッシュ
- `data/raw/gutenberg_54774_fortunes_and_dreams.txt`: 取得したProject Gutenberg原文キャッシュ
- `data/raw/gutenberg_53879_witches_dream_book.txt`: 取得したProject Gutenberg原文キャッシュ
- `data/raw/gutenberg_60045_golden_wheel_dream_book.txt`: 取得したProject Gutenberg原文キャッシュ

## Current Coverage

- implemented sources: 15
- entries: 22150
- duplicate `term_normalized`: 0
- languages: `en`, `tr`, `zh-Hant`, `my`

## Notes

- 「全世界の夢占いDB」の完全な範囲は不明です。
- 独占的な著作権表記があるDBは本文取得しません。
- ライセンス表記がないDBはオープンソースとは断定せず、`no_license_unverified` として記録します。
- 重複は `term_normalized` と正規化済み意味テキストで排除し、同じ単語の複数出典は1項目に統合します。
- `周公解夢` は古典の短句形式なので、誤分解を避けるため原句を `zh-Hant` の term / meaning として保存します。
