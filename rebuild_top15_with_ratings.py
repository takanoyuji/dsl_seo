#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Places API 取得結果を反映して article_top15_plan.csv を再生成する

入力:
  - article_candidates.csv     (pipeline_top15.py の出力)
  - places_api_results.csv     (fetch_places_api.py の出力)

出力:
  - article_top15_final.csv    (Google評価込みTOP15)
"""

import re
import unicodedata
import pandas as pd
import logging
from pathlib import Path
from tqdm import tqdm


def _norm_addr(a: str) -> str:
    """住所正規化（全角→半角・丁目変換・番地部分のみ抽出）"""
    s = unicodedata.normalize('NFKC', str(a or ''))
    s = re.sub(r'[\u2010-\u2015\u2212\uff0d]', '-', s)
    s = re.sub(r'〒?\s*\d{3}-\d{4}\s*', '', s)
    s = s.lower()
    s = re.sub(r'(\d+)丁目', r'\1-', s)
    s = re.sub(r'(\d+)番地?', r'\1-', s)
    m = re.search(r'(\d+(?:-\d+)+)', s)
    if m:
        prefix = re.sub(r'[\s\u3000]', '', s[:m.start()])
        s = prefix + m.group(1)
    else:
        s = re.sub(r'[\s\u3000]', '', s)
    return s.rstrip('-_.,')


# ============================================================
# 設定
# ============================================================
CANDIDATES_CSV   = "data/pipeline/article_candidates.csv"
API_RESULTS_CSV  = "data/pipeline/places_api_results.csv"
OUTPUT_CSV       = "data/pipeline/article_top15_final.csv"

# --- スコア重み ---
WEIGHT_PARTNER      = 100   # 提携スタジオ
WEIGHT_RATING       = 20    # ★5.0 換算の最大加点 (rating * WEIGHT_RATING / 5.0)
WEIGHT_REVIEWS      = 15    # 口コミ数 300件以上で満点 (min(count,300)/300 * WEIGHT_REVIEWS)
WEIGHT_DIST_500     = 15    # 駅から500m以内
WEIGHT_DIST_1000    = 8     # 駅から1000m以内
WEIGHT_MULTI_ARTICLE = 2    # 候補記事数ごとの加点 (最大5記事分)

# --- 優先度ボーナス ---
PRIORITY_BONUS = {"S": 20, "A": 10, "B": 0}

# --- TOP15 の提携スタジオ上限 ---
MAX_PARTNERS = 8

# ============================================================
# ログ設定
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ============================================================
# ユーティリティ
# ============================================================
def safe_float(val) -> float | None:
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def calc_score(row: pd.Series, priority_bonus: int) -> tuple[float, list[str]]:
    """スコアと理由リストを返す"""
    score = 0
    reasons = []

    if row["is_partner"] == 1:
        score += WEIGHT_PARTNER
        reasons.append("提携")

    score += priority_bonus

    # Google評価
    rating = safe_float(row.get("rating"))
    if rating is not None:
        pts = rating / 5.0 * WEIGHT_RATING
        score += pts
        reasons.append(f"★{rating:.1f}(+{pts:.0f}pt)")

    # 口コミ数 (最大300件で満点)
    reviews = safe_float(row.get("review_count"))
    if reviews is not None and not (reviews != reviews):  # NaN check
        pts = min(reviews, 300) / 300 * WEIGHT_REVIEWS
        score += pts
        reasons.append(f"口コミ{int(reviews)}件(+{pts:.0f}pt)")

    # 駅距離
    dist = safe_float(row.get("distance_to_primary_station_m"))
    if dist is not None:
        if dist <= 500:
            score += WEIGHT_DIST_500
            reasons.append("駅近500m以内")
        elif dist <= 1000:
            score += WEIGHT_DIST_1000
            reasons.append("駅近1000m以内")

    # 複数記事候補
    multi = safe_float(row.get("candidate_article_count"))
    if multi is not None and multi > 1:
        pts = min(int(multi) - 1, 5) * WEIGHT_MULTI_ARTICLE
        score += pts
        reasons.append(f"複数記事候補×{int(multi)}")

    return round(score, 2), reasons


# ============================================================
# メイン
# ============================================================
def main():
    # --- データ読み込み ---
    log.info(f"候補CSV読み込み: {CANDIDATES_CSV}")
    df_cand = pd.read_csv(CANDIDATES_CSV)
    log.info(f"  {len(df_cand)} レコード")

    if not Path(API_RESULTS_CSV).exists():
        log.error(
            f"{API_RESULTS_CSV} が見つかりません。\n"
            "先に fetch_places_api.py を実行してください:\n"
            "  export GOOGLE_PLACES_API_KEY='AIza...'\n"
            "  python3 fetch_places_api.py"
        )
        return

    log.info(f"API結果CSV読み込み: {API_RESULTS_CSV}")
    df_api = pd.read_csv(API_RESULTS_CSV, usecols=[
        "studio_title", "place_id", "google_name",
        "rating", "review_count", "match_confidence", "api_status",
    ])
    log.info(
        f"  {len(df_api)} 件  "
        f"取得OK={(df_api['api_status']=='OK').sum()}  "
        f"NOT_FOUND={(df_api['api_status']=='NOT_FOUND').sum()}"
    )

    # --- マージ ---
    df = df_cand.merge(df_api, on="studio_title", how="left")

    # 複数記事候補数をスタジオ単位で付与
    multi_map = (
        df.groupby("studio_title")["article_title"]
        .nunique()
        .rename("candidate_article_count")
    )
    df = df.join(multi_map, on="studio_title")

    # 重複排除 (同一記事内で同一スタジオが複数マッチ行を持つ場合)
    df = df.drop_duplicates(["article_title", "studio_title"])

    # --- 記事ごとに TOP15 選定 ---
    articles = df[["article_title", "article_type", "article_priority"]].drop_duplicates()
    top15_rows = []

    for _, art in tqdm(articles.iterrows(), total=len(articles), desc="TOP15選定"):
        art_cand = df[df["article_title"] == art["article_title"]].copy()
        if art_cand.empty:
            continue

        p_bonus = PRIORITY_BONUS.get(art["article_priority"], 0)

        scores, reason_lists = zip(*[
            calc_score(row, p_bonus) for _, row in art_cand.iterrows()
        ])
        art_cand = art_cand.copy()
        art_cand["selection_score"] = list(scores)
        art_cand["_reasons"]        = list(reason_lists)

        art_cand = art_cand.sort_values("selection_score", ascending=False)

        # 提携: 最大 MAX_PARTNERS 件、残りは非提携で埋める
        partners     = art_cand[art_cand["is_partner"] == 1]
        non_partners = art_cand[art_cand["is_partner"] == 0]

        n_p  = min(len(partners), MAX_PARTNERS)
        n_np = min(len(non_partners), 15 - n_p)

        combined = (
            pd.concat([partners.head(n_p), non_partners.head(n_np)])
            .drop_duplicates("studio_title")
            .sort_values("selection_score", ascending=False)
        )
        # 同一住所の重複排除（提携優先・高スコア優先で既に sort 済み）
        combined["_addr_norm"] = combined["address"].apply(_norm_addr)
        has_addr = combined["_addr_norm"].str.len() > 5
        selected = pd.concat([
            combined[~has_addr],
            combined[has_addr].drop_duplicates("_addr_norm", keep="first"),
        ]).drop(columns=["_addr_norm"]).sort_values("selection_score", ascending=False).head(15)

        for rank, (_, row) in enumerate(selected.iterrows(), 1):
            reasons = row["_reasons"]
            reasons.append(row.get("article_match_reason", ""))
            reasons = [r for r in reasons if r]

            top15_rows.append({
                "article_title":               art["article_title"],
                "article_type":                art["article_type"],
                "article_priority":            art["article_priority"],
                "rank":                        rank,
                "studio_title":                row["studio_title"],
                "address":                     row.get("address", ""),
                "is_partner":                  row["is_partner"],
                "dslurl":                      row.get("dslurl", ""),
                "primary_station":             row.get("primary_station", ""),
                "distance_to_primary_station_m": row.get("distance_to_primary_station_m", ""),
                "place_id":                    row.get("place_id", ""),
                "google_name":                 row.get("google_name", ""),
                "rating":                      row.get("rating"),
                "review_count":                row.get("review_count"),
                "match_confidence":            row.get("match_confidence"),
                "api_status":                  row.get("api_status", ""),
                "selection_score":             row["selection_score"],
                "selection_reason":            " | ".join(reasons),
            })

    df_top15 = pd.DataFrame(top15_rows)
    df_top15.to_csv(OUTPUT_CSV, index=False, encoding="utf-8-sig")
    log.info(f"出力完了: {OUTPUT_CSV} ({len(df_top15)} 件, {df_top15['article_title'].nunique()} 記事)")

    # --- サマリー表示 ---
    print("\n" + "="*70)
    print("■ TOP15 再生成完了サマリー (Google評価込み)")
    print("="*70)
    grp = df_top15.groupby(["article_title", "article_priority"]).agg(
        count=("rank", "count"),
        partner_count=("is_partner", "sum"),
        avg_rating=("rating", "mean"),
        avg_reviews=("review_count", "mean"),
    )
    for (title, prio), r in grp.iterrows():
        rating_str = f"★{r['avg_rating']:.2f}" if pd.notna(r['avg_rating']) else "★---"
        review_str = f"{int(r['avg_reviews'])}件" if pd.notna(r['avg_reviews']) else "---件"
        print(
            f"  [{prio}] {title[:30]:30}  "
            f"TOP{int(r['count']):2}  提携:{int(r['partner_count'])}  "
            f"平均{rating_str} 口コミ平均{review_str}"
        )
    print("="*70)


if __name__ == "__main__":
    main()
