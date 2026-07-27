# DSL SEO パイプライン — Claude 作業指示

## プロジェクト概要
Dance Studio Lab (DSL) の東京レンタルダンススタジオ SEO記事生成パイプライン。
スタジオCSV → TOP15選定 → HTML記事生成。

## パイプライン実行順
```
1. python3 pipeline_top15.py           # 候補抽出・仮TOP15
2. python3 fill_studio_geo.py          # ジオデータ補完
3. python3 fetch_places_api.py         # Google Places API（チェックポイント再開可）
4. python3 rebuild_top15_with_ratings.py
5. python3 fetch_reviews_and_describe.py  # 口コミ紹介文生成（任意）
6. python3 generate_articles.py --all  # HTML記事生成
```

## ディレクトリ構成
```
dslseo/
├── data/master/      # 手動管理の入力データ
├── data/pipeline/    # スクリプト出力の中間CSV
├── checkpoints/      # API途中再開用JSON
├── articles/         # 生成HTML
└── *.py              # スクリプト群
```

## generate_articles.py を変更する際のルール

### 絶対に変えてはいけない設計判断
- **ペルソナ「だん」**: Dance Cover Lab所属・28歳・ダンス専門カメラマン
  - CSS class: `.dan-comment`, `.dan-sign`
  - 一人称視点のコメントを各スタジオカードに付与
- **提携スタジオ (is_partner)**: dslurl の有無で判定。最大8件まで優先掲載
- **掲載数 ≤1 → 記事生成スキップ**（WARNINGログ）
- **RENTAL_FIT_THRESHOLD = 3**: 非提携で閾値未満のスタジオは除外

### CTAボタンのテキスト（変更禁止）
| 用途 | テキスト |
|------|---------|
| 提携スタジオ予約 | 「空き状況を確認する」 |
| 比較表リンク | 「空き状況」 |
| 非提携スタジオ外部リンク | 「公式サイトを見る」 |

❌ 「詳細を見る」「詳細・予約」「予約する」は使わない

### NG表現（禁止フレーズ）
記事本文・紹介文に含めてはいけない表現：
- 「本格的な音響設備を備え」
- 「ダンサーの動きに配慮した素材」
- 「ターンやジャンプも安心」
- 「最適な環境が整っています」
- 「大人気」「リピーター多数」
- 「誰にでもおすすめ」「絶対におすすめ」
- スクール/レッスン受講を示す表現（「レッスンを受けられ」「講師から」「クラスを開講」「通える」等）

### SEOに必要な構造要素（全記事必須）
- canonical / OGP / Twitter Card / robots meta
- Google Tag Manager（`GTM_HEAD` / `GTM_BODY`）
  - 2026-07-27 まで生成スクリプトが出力しておらず、生成後に手作業で注入されていた。
    そのため再生成して `media/` にコピーすると**全記事の計測が黙って止まる**状態だった。
    現在は `build_html()` が出力するので消えない。**外さないこと。**
- JSON-LD: Article, FAQPage, BreadcrumbList, ItemList（= LocalBusiness/SportsActivityLocation を内包）

#### ⚠️ ItemList は1ブロックのみ。aggregateRating を ListItem に直付けしない

`ListItem` はレビュースニペットの対象タイプではないため、`aggregateRating` を直接付けると
Search Console で **「項目 `<parent_node>` のオブジェクトタイプが無効です」** が出る。
必ず `ListItem > item > SportsActivityLocation` に付けること。

```jsonc
// ❌ NG
{ "@type": "ListItem", "position": 1, "name": "○○スタジオ",
  "aggregateRating": { "@type": "AggregateRating", "ratingValue": 3.9, "ratingCount": 30 } }

// ✅ OK（make_local_business_jsonld() が出力する形）
{ "@type": "ListItem", "position": 1,
  "item": { "@type": "SportsActivityLocation", "name": "○○スタジオ",
            "aggregateRating": { "@type": "AggregateRating", "ratingValue": 3.9, "ratingCount": 30 } } }
```

経緯: 2026-07-27 まで `make_itemlist_jsonld()` と `make_local_business_jsonld()` が
**同じ内容の ItemList を2ブロック**出力しており、前者が ListItem 直付けだった。
本番211記事・計1,890箇所でエラーが出たため、前者を廃止して後者1ブロックに統合した。
ItemList を増やすときは、この重複を復活させないこと。
- パンくずリスト HTML（h1直前）
- ランキング根拠タグ（DSL提携/Google★/口コミ件数/駅近）
- 著者ボックス（記事末尾・editorial_noteの後）
- ヒーロー画像（h1直下・最初の提携スタジオ画像）

### 施設タイプ分類（usage_type）
- `dance`: ダンス主用途
- `multi`: 多目的（ダンスOK）
- `photo`: 撮影スタジオ
- `pilates`: ピラティス・ヨガ
- `music`: 音楽スタジオ
- `unknown`: 判定不能

## データの落とし穴（過去に遭遇）
- `longitude`/`latitude` は元CSVでラベルが入れ替わっていた → コード内で修正済み
- `is_partner` は元CSVでNaN → `dslurl` 有無から再計算する
- `feature:midnight` は `feature:24h` のエイリアスとしてpipelineで動的付与

## Python環境
- python3 (3.12.3) / pip: `python3 -m pip --break-system-packages`
- 依存: pandas, openpyxl, tqdm, anthropic, requests
