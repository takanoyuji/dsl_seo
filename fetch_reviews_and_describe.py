#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
① Places API (Place Details) で口コミテキストを取得
② Claude Haiku で口コミを要約して紹介文を生成
③ studio_descriptions.csv に保存

使い方:
  python3 fetch_reviews_and_describe.py
"""

import os
import re
import time
import json
import logging
import requests
import anthropic
import pandas as pd
from pathlib import Path
from tqdm import tqdm

# ============================================================
# .env 読み込み
# ============================================================
_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    for _line in _env_path.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

# ============================================================
# 設定
# ============================================================
GOOGLE_API_KEY   = os.environ.get("GOOGLE_PLACES_API_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

TOP15_CSV        = "data/pipeline/article_top15_final.csv"
OUTPUT_CSV       = "data/pipeline/studio_descriptions.csv"
REVIEW_CHECKPOINT = "checkpoints/reviews_checkpoint.json"

CLAUDE_MODEL     = "claude-haiku-4-5-20251001"
QPS_LIMIT        = 5    # Google API レート
BATCH_SAVE       = 10   # チェックポイント保存間隔

# ============================================================
# ログ
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ============================================================
# Places API: Place Details で口コミ取得
# ============================================================
PLACE_DETAILS_URL = "https://places.googleapis.com/v1/places/{place_id}"
REVIEW_FIELDS     = "reviews,displayName,rating,userRatingCount"


def fetch_reviews(place_id: str) -> dict:
    """Place Details API で口コミテキストを取得する"""
    url = PLACE_DETAILS_URL.format(place_id=place_id)
    headers = {
        "X-Goog-Api-Key": GOOGLE_API_KEY,
        "X-Goog-FieldMask": REVIEW_FIELDS,
    }
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        return {"status": "ERROR", "error": str(e)}

    reviews_raw = data.get("reviews", [])
    reviews = []
    for r in reviews_raw:
        text = r.get("text", {}).get("text", "")
        if text:
            reviews.append({
                "text":   text,
                "rating": r.get("rating"),
                "date":   r.get("relativePublishTimeDescription", ""),
            })

    return {
        "status":       "OK",
        "place_id":     place_id,
        "reviews":      reviews,
        "review_count": len(reviews),
    }


# ============================================================
# Claude Haiku: 口コミ → 紹介文生成
# ============================================================
SYSTEM_PROMPT = """あなたは現役ダンサーとして東京のレンタルスタジオを使い続けてきた経験を持つライターです。
Googleマップの口コミをもとに、ダンサー目線でそのスタジオの魅力と使い勝手をリアルに伝える紹介文を書いてください。

ルール:
- 3〜4文、120〜180字程度（短すぎる文は却下）
- 「床が滑りにくい」「鏡の枚数が多い」「音響が本格的」「天井が高くて跳べる」など練習環境の具体的な特徴を入れる
- ダンサーが気にするポイント（床材・鏡・音響・広さ・予約のしやすさなど）を中心に書く
- 「練習のテンションが上がる」「集中できる」など気持ちが伝わる言葉を自然に使う
- 「口コミによると」「ユーザーからは」などの前置きは不要。事実として書く
- 体言止めや箇条書きは使わず、文章で書く
- 「です・ます」調で統一する
- 「〜の立地にあるレンタルダンススタジオです」のような無個性な冒頭は避ける
- 紹介文だけをそのまま出力する。見出し・文字数・マークダウン記法（#・**など）は一切使わない"""


def generate_description(
    studio_name: str,
    area: str,
    station: str,
    dist_min: str | None,
    rating: float | None,
    review_count: int | None,
    reviews: list[dict],
    client: anthropic.Anthropic,
) -> str:
    """口コミテキストをもとにClaude Haikuで紹介文を生成する"""

    # 口コミがない場合はフォールバック
    if not reviews:
        return _fallback_description(studio_name, area, station, dist_min, rating, review_count)

    # 口コミテキストを整形（最大5件・各200文字まで）
    review_lines = []
    for i, r in enumerate(reviews[:5], 1):
        text = r["text"][:200].replace("\n", " ")
        star = f"★{r['rating']}" if r.get("rating") else ""
        review_lines.append(f"{i}. {star} {text}")
    reviews_text = "\n".join(review_lines)

    # アクセス情報
    access = f"{station}{dist_min}" if station and dist_min else ""
    rating_str = f"Google評価{rating:.1f}点（{review_count}件）" if rating else ""

    user_message = f"""スタジオ名: {studio_name}
エリア: {area}
アクセス: {access}
{rating_str}

【Googleの口コミ（抜粋）】
{reviews_text}

上記の口コミをもとに、このスタジオの紹介文を2〜3文で書いてください。"""

    try:
        msg = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=200,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )
        return msg.content[0].text.strip()
    except Exception as e:
        log.warning(f"Claude APIエラー ({studio_name}): {e}")
        return _fallback_description(studio_name, area, station, dist_min, rating, review_count)


def _fallback_description(
    name, area, station, dist_min, rating, review_count
) -> str:
    """口コミなし・APIエラー時のフォールバック（ダンサー目線）"""
    parts = []

    # アクセス
    if station and dist_min:
        parts.append(
            f"{station}{dist_min}にあり、練習前後に時間を使わずに済む立地が魅力です。"
        )
    elif area:
        parts.append(
            f"{area}エリアで練習場所を探しているダンサーに選ばれているスタジオです。"
        )

    # 評価
    if rating and review_count:
        if rating >= 4.5 and review_count >= 30:
            parts.append(
                f"Google評価{rating:.1f}点（{review_count}件）と高評価が続いており、"
                f"実際に使ったダンサーたちからの信頼を集めています。"
            )
        elif rating >= 4.0 and review_count >= 5:
            parts.append(
                f"Google評価{rating:.1f}点（{review_count}件）を獲得しており、"
                f"リピーターが多いスタジオとして知られています。"
            )
        elif rating >= 4.0:
            parts.append(f"Google評価{rating:.1f}点と利用者から好評を得ています。")

    # 締め
    parts.append(
        "床材・鏡・音響など練習に必要な設備が整っており、"
        "個人練習からグループ練習まで集中して取り組める環境です。"
    )

    return "".join(parts[:3])


# ============================================================
# メイン
# ============================================================
def main():
    if not GOOGLE_API_KEY:
        log.error("GOOGLE_PLACES_API_KEY が未設定です")
        return
    if not ANTHROPIC_API_KEY:
        log.error("ANTHROPIC_API_KEY が未設定です")
        return

    # --- データ読み込み ---
    log.info("TOP15データ読み込み...")
    df = pd.read_csv(TOP15_CSV)

    # TOP15に出ているユニークスタジオのみ対象
    # article_candidates.csv から primary_area を補完
    df_cand = pd.read_csv("data/pipeline/article_candidates.csv")
    area_map = df_cand.drop_duplicates("studio_title").set_index("studio_title")["primary_area"]
    df["primary_area"] = df["studio_title"].map(area_map)

    targets = (
        df[df["place_id"].notna() & (df["place_id"] != "")]
        [["studio_title", "place_id", "primary_area", "primary_station",
          "distance_to_primary_station_m", "rating", "review_count"]]
        .drop_duplicates("studio_title")
        .reset_index(drop=True)
    )
    log.info(f"  対象スタジオ: {len(targets)}件（place_id取得済み）")

    # --- チェックポイント読み込み ---
    cp_path = Path(REVIEW_CHECKPOINT)
    checkpoint = {}
    if cp_path.exists():
        checkpoint = json.loads(cp_path.read_text(encoding="utf-8"))
        log.info(f"  チェックポイント: {len(checkpoint)}件処理済み")

    todo = targets[~targets["studio_title"].isin(checkpoint)].copy()
    log.info(f"  未処理: {len(todo)}件")

    # --- Claude クライアント初期化 ---
    claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    # --- 処理ループ ---
    interval = 1.0 / QPS_LIMIT
    last_call = 0.0

    for i, (_, row) in enumerate(
        tqdm(todo.iterrows(), total=len(todo), desc="口コミ取得→要約"),
        start=1,
    ):
        # Google API レート制御
        elapsed = time.time() - last_call
        wait = interval - elapsed
        if wait > 0:
            time.sleep(wait)
        last_call = time.time()

        studio_name = row["studio_title"]
        place_id    = row["place_id"]

        # ① 口コミ取得
        review_result = fetch_reviews(place_id)

        if review_result["status"] != "OK":
            log.warning(f"口コミ取得失敗: {studio_name} → {review_result.get('error')}")
            reviews = []
        else:
            reviews = review_result["reviews"]

        # ② 距離 → 徒歩分数
        try:
            minutes = str(round(float(row["distance_to_primary_station_m"]) / 80))
            dist_min = f"から徒歩{minutes}分"
        except (TypeError, ValueError):
            dist_min = None

        # ③ Claude で紹介文生成
        desc = generate_description(
            studio_name  = studio_name,
            area         = str(row["primary_area"]) if pd.notna(row["primary_area"]) else "",
            station      = str(row["primary_station"]) if pd.notna(row["primary_station"]) else "",
            dist_min     = dist_min,
            rating       = float(row["rating"])       if pd.notna(row["rating"])       else None,
            review_count = int(float(row["review_count"])) if pd.notna(row["review_count"]) else None,
            reviews      = reviews,
            client       = claude,
        )

        checkpoint[studio_name] = {
            "description":  desc,
            "reviews":      reviews,
            "review_count": len(reviews),
        }

        log.debug(f"✓ {studio_name[:25]:25}  口コミ{len(reviews)}件  → {desc[:40]}...")

        # 定期保存
        if i % BATCH_SAVE == 0:
            cp_path.write_text(
                json.dumps(checkpoint, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    # 最終保存
    cp_path.write_text(
        json.dumps(checkpoint, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    log.info(f"チェックポイント保存: {REVIEW_CHECKPOINT}")

    # --- CSV出力 ---
    rows = []
    for name, data in checkpoint.items():
        rows.append({
            "studio_title": name,
            "description":  data["description"],
            "review_count_fetched": data["review_count"],
            "reviews_text": " / ".join(r["text"][:100] for r in data["reviews"][:3]),
        })

    df_desc = pd.DataFrame(rows)
    df_desc.to_csv(OUTPUT_CSV, index=False, encoding="utf-8-sig")
    log.info(f"出力完了: {OUTPUT_CSV} ({len(df_desc)}件)")

    # --- サンプル表示 ---
    print("\n" + "="*70)
    print("■ 生成された紹介文サンプル（上位5件）")
    print("="*70)
    for _, r in df_desc.head(5).iterrows():
        print(f"\n【{r['studio_title']}】（口コミ{r['review_count_fetched']}件使用）")
        print(f"  {r['description']}")
    print("="*70)


if __name__ == "__main__":
    main()
