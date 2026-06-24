# DSL SEO — レンタルダンススタジオ記事生成パイプライン

東京近郊のレンタルダンススタジオCSVから、各エリア・駅ごとのSEO記事（HTML）を自動生成するパイプラインです。

---

## ディレクトリ構成

```
dslseo/
├── README.md                        # このファイル
├── .env                             # APIキー（要設定）
│
├── data/
│   ├── master/                      # 入力マスタ（手動管理）
│   │   ├── studio_with_tags_dslurl_address_filled.csv  # スタジオマスタ（1250件）
│   │   ├── DSL記事一覧.xlsx                              # 記事一覧（48記事）
│   │   └── m_station.csv                               # 駅データ
│   │
│   └── pipeline/                    # スクリプトが自動生成する中間・出力ファイル
│       ├── studios_partner_flagged.csv      # ① 提携フラグ付きスタジオ全件
│       ├── article_candidates.csv           # ② 記事×候補スタジオ（~2800件）
│       ├── article_candidate_summary.csv    # ② 記事ごと候補数サマリ
│       ├── places_api_targets.csv           # ③ Places API取得対象（最大500件）
│       ├── article_top15_plan.csv           # ③ 仮TOP15（Places API前）
│       ├── places_api_results.csv           # ④ Google評価・place_id取得結果
│       ├── article_top15_final.csv          # ⑤ Google評価込みTOP15（563件）
│       ├── studio_descriptions.csv          # ⑥ 口コミ由来の紹介文
│       ├── article_generation_summary.csv   # ⑦ 記事生成サマリ
│       └── article_selected_studios.csv     # ⑦ 選定スタジオ一覧
│
├── checkpoints/                     # 途中再開用チェックポイント（自動生成）
│   ├── fill_geo_checkpoint.json     # fill_studio_geo.py 用
│   ├── places_api_checkpoint.json   # fetch_places_api.py 用
│   └── reviews_checkpoint.json      # fetch_reviews_and_describe.py 用
│
├── articles/                        # 生成されたHTMLファイル（40記事）
│   └── *.html
│
└── old/                             # 不要ファイル（参照不要）
```

---

## スクリプト一覧・実行順序

### ① `pipeline_top15.py` — 候補抽出・仮TOP15生成

```bash
python3 pipeline_top15.py
```

**入力:** `data/master/studio_with_tags_dslurl_address_filled.csv`, `data/master/DSL記事一覧.xlsx`
**出力:** `data/pipeline/` 以下の①〜③ファイル群

---

### ② `fill_studio_geo.py` — スタジオのジオデータ補完

住所からlat/lon・最寄り駅・エリアを補完する。スタジオマスタに直接書き戻す。

```bash
python3 fill_studio_geo.py                # 全スタジオ対象
python3 fill_studio_geo.py --partners-only  # 提携スタジオのみ
```

**入力/出力:** `data/master/studio_with_tags_dslurl_address_filled.csv`（上書き）
**チェックポイント:** `checkpoints/fill_geo_checkpoint.json`（途中再開対応）

---

### ③ `fetch_places_api.py` — Google Places APIでGoogle評価取得

```bash
python3 fetch_places_api.py
```

**入力:** `data/pipeline/places_api_targets.csv`
**出力:** `data/pipeline/places_api_results.csv`
**チェックポイント:** `checkpoints/places_api_checkpoint.json`

---

### ④ `rebuild_top15_with_ratings.py` — Google評価込みTOP15再生成

```bash
python3 rebuild_top15_with_ratings.py
```

**入力:** `data/pipeline/article_candidates.csv`, `data/pipeline/places_api_results.csv`
**出力:** `data/pipeline/article_top15_final.csv`

---

### ⑤ `fetch_reviews_and_describe.py` — 口コミ取得・紹介文生成（任意）

Google Maps口コミをClaude Haikuで要約した紹介文を生成する。実行済みなら再実行不要。

```bash
python3 fetch_reviews_and_describe.py
```

**入力:** `data/pipeline/article_top15_final.csv`, `data/pipeline/article_candidates.csv`
**出力:** `data/pipeline/studio_descriptions.csv`
**チェックポイント:** `checkpoints/reviews_checkpoint.json`

---

### ⑥ `generate_articles.py` — HTML記事生成（メイン）

```bash
python3 generate_articles.py --article "新宿のレンタルダンススタジオおすすめ"  # 1記事
python3 generate_articles.py --all                                            # 全40記事
```

**入力:** `data/pipeline/article_top15_final.csv`, `data/master/studio_with_tags_dslurl_address_filled.csv`, `data/pipeline/studio_descriptions.csv`, `data/pipeline/places_api_results.csv`
**出力:** `articles/*.html`, `data/pipeline/article_generation_summary.csv`, `data/pipeline/article_selected_studios.csv`

---

## .env の設定

```
GOOGLE_PLACES_API_KEY=AIza...
ANTHROPIC_API_KEY=sk-ant-...
```

---

## スタジオマスタ（data/master/studio_with_tags_dslurl_address_filled.csv）の主要列

| 列名 | 内容 |
|------|------|
| タイトル | スタジオ名 |
| 住所 | 住所 |
| dslurl | DSLサイトURL（提携スタジオのみ） |
| longitude / latitude | 経度・緯度 |
| primary_station | 最寄り駅 |
| distance_to_primary_station_m | 最寄り駅までの距離(m) |
| candidate_stations | 候補駅（`\|`区切り） |
| primary_area | 主エリア |
| article_tags | タグ（`\|`区切り） |
| is_partner | 提携フラグ（dslurl有=1） |

---

## 記事スコアリング概要

| 要素 | 点数 |
|------|------|
| 提携スタジオ（dslurl有） | +3 |
| 名称にレンタルダンス/ダンススタジオ等 | +2 |
| 名称にスクール/教室/アカデミー等 | -4 |
| 名称に音楽スタジオ/ジム/ヨガ等（非ダンス） | -3〜-5 |

スコアが閾値（3）未満かつ非提携のスタジオは記事から除外されます。
