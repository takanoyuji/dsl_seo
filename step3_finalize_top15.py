#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Step 3: TOP15確定 (v2)

スコアリング方針:
  1. 閉業スタジオを除外 (businessStatus)
  2. レンタルダンス適合度 (rental_fit_score) を最優先
  3. Google口コミ数・評価を強く反映
  4. 提携スタジオは加点するが上位固定しない (partner bonus を適正化)
  5. 駅距離ボーナス
  6. 低評価・口コミ少数は減点または除外

入力:
  data/pipeline/article_candidates.csv
  data/pipeline/places_api_results.csv          (Google評価・口コミ数)
  data/pipeline/business_status_excluded_studios.csv  (閉業チェック済み)
  data/master/studio_with_tags_dslurl_address_filled.csv
  data/master/studio_group_aliases.csv

出力:
  data/pipeline/shinjuku_station_selected_studios.csv
  data/pipeline/article_top15_plan.csv  (更新)
  data/pipeline/group_key_debug.csv
  data/pipeline/shinjuku_selection_score_debug.csv  (全候補スコア内訳)

使い方:
  python3 step3_finalize_top15.py
  python3 step3_finalize_top15.py --article "新宿駅のレンタルダンススタジオおすすめ"
"""

import argparse
import re
import sys
import logging
import unicodedata
import pandas as pd
from pathlib import Path

# ============================================================
# 設定
# ============================================================
CANDIDATES_CSV      = "data/pipeline/article_candidates.csv"
PLACES_CSV          = "data/pipeline/places_api_results.csv"
EXCLUDED_CSV        = "data/pipeline/business_status_excluded_studios.csv"
STUDIO_CSV          = "data/master/studio_with_tags_dslurl_address_filled.csv"
GROUP_ALIASES_CSV   = "data/master/studio_group_aliases.csv"
OUTPUT_DIR          = Path("data/pipeline")

# デフォルト記事 (--article で上書き可)
DEFAULT_ARTICLE     = "新宿駅のレンタルダンススタジオおすすめ"

MAX_GROUPS = 15

# rental_fit_score 除外閾値
# 提携スタジオ: fit >= 0 で掲載可 (fit < 0 は除外)
# 非提携スタジオ: fit >= 20 で掲載可
RENT_FIT_EXCLUDE_NONPARTNER = 20
RENT_FIT_EXCLUDE_PARTNER    = 0

# 低評価除外: 候補が十分 (>= 候補閾値) なら除外する
LOW_RATING_EXCLUDE_THRESHOLD = 20  # 候補がこれ以上あれば低評価を除外
LOW_RATING_MAX   = 3.0
LOW_RATING_MIN_RC = 3              # google_review_count <= この値 かつ LOW_RATING_MAX 未満は除外

# 問題スタジオ警告キーワード
WARN_KW = ["ピラティス", "フォトスタジオ", "スクール", "音楽スタジオ", "サウンドスタジオ", "写真館"]

# バレエ施設 internal_note
BALLET_INTERNAL_NOTE = "名称上はバレエ施設に見えるため、レンタル用途の表示文に注意"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ============================================================
# ユーティリティ
# ============================================================
def safe_float(val):
    try:
        f = float(val)
        import math
        return None if math.isnan(f) else f
    except (TypeError, ValueError):
        return None


def safe_int(val):
    f = safe_float(val)
    return int(f) if f is not None else None


def safe_str(val) -> str:
    if val is None:
        return ""
    try:
        import pandas as _pd
        if _pd.isna(val):
            return ""
    except (TypeError, ValueError):
        pass
    s = str(val).strip()
    return "" if s.lower() == "nan" else s


# ============================================================
# グループエイリアス読み込み
# ============================================================
def load_group_aliases(csv_path: str = GROUP_ALIASES_CSV) -> dict:
    p = Path(csv_path)
    if not p.exists():
        log.warning(f"グループエイリアスCSV未発見: {csv_path}")
        return {}
    df = pd.read_csv(p)
    result = {}
    for _, row in df.iterrows():
        alias = str(row.get("alias_name", "")).strip()
        group = str(row.get("group_name", "")).strip()
        if alias and group:
            result[alias] = group
    log.info(f"グループエイリアス読み込み: {len(result)}件")
    return result


# ============================================================
# スタジオ名正規化 (全国記事共通ロジック)
# ============================================================
_JP_LOCATIONS = [
    "新宿", "西新宿", "東新宿", "南新宿", "渋谷", "代々木",
    "大久保", "新大久保", "四谷", "初台", "参宮橋", "北参道",
    "4丁目", "ハウス", "コンシェルジュ", "アネックス",
]
_EN_LOCATIONS = ["shinjuku", "okubo", "yoyogi", "shibuya", "gyoen"]
_BRAND_SEP_PAT = re.compile(
    r'\s*[-－–—|｜/]\s*(?=[\u3040-\u9fff\u4e00-\u9fff])'
)


def normalize_studio_name(title: str) -> str:
    s = str(title or "").strip()
    s = unicodedata.normalize('NFKC', s)
    s = re.sub(r'[（(【\[].+?[）)】\]]', '', s).strip()
    s = re.sub(r'\s+\S+[店校]\s*$', '', s).strip()
    s = re.sub(r'[\u4e00-\u9fff\u30a0-\u30ff\u3040-\u309f]{1,6}[店校]\s*$', '', s).strip()
    s = re.sub(r'\s+[\u4e00-\u9fff\u30a0-\u30ff\u3040-\u309f]+スタジオ\s*$', '', s).strip()
    s = re.sub(r'\s+(レンタルダンススタジオ|レンタルスタジオ|ダンススタジオ|スタジオ)\s*$', '', s).strip()
    tokens = s.split()
    while tokens and any(tokens[-1].lower().startswith(loc) for loc in _EN_LOCATIONS):
        tokens.pop()
    if tokens:
        s = " ".join(tokens)
    for loc in sorted(_JP_LOCATIONS, key=len, reverse=True):
        if s.endswith(loc):
            s = s[:-len(loc)].strip()
            break
    s = re.sub(r'\s+\S*丁目\s*$', '', s).strip()
    for loc in sorted(_JP_LOCATIONS, key=len, reverse=True):
        if s.endswith(loc):
            s = s[:-len(loc)].strip()
            break
    m = _BRAND_SEP_PAT.search(s)
    if m and m.start() > 0:
        candidate = s[:m.start()].strip()
        has_ascii     = any(c.isascii() and c.isalnum() for c in candidate)
        has_studio_kw = any(kw in candidate for kw in ["STUDIO", "Studio", "studio", "スタジオ"])
        if candidate and (has_ascii or has_studio_kw):
            s = candidate
    if " " in s:
        s = re.sub(r'^(レンタルダンススタジオ|レンタルスタジオ|ダンススタジオ)\s+', '', s).strip()
    return s or str(title or "").strip()


# ============================================================
# グループキー決定 (3段階)
# ============================================================
def get_group_key(title: str, aliases: dict) -> tuple:
    raw        = str(title or "").strip()
    normalized = normalize_studio_name(raw)
    for lookup in [raw, normalized]:
        if lookup in aliases:
            gk = aliases[lookup]
            return gk, normalized, "alias", f"alias: '{lookup}' -> '{gk}'"
    return normalized, normalized, "normalized", "normalized_name"


# ============================================================
# 選定スコア計算 (v2 - スコア内訳付き)
# ============================================================
def calc_score_breakdown(row) -> dict:
    """
    スコア内訳を dict で返す。
    business_status は列 "business_status" を参照。
    """
    is_partner      = int(row.get("is_partner", 0) or 0) == 1
    fit             = safe_float(row.get("rental_fit_score")) or 0.0
    dist            = safe_float(row.get("distance_to_article_station_m")) or 9999.0
    rating          = safe_float(row.get("google_rating"))
    rc_raw          = safe_float(row.get("google_review_count"))
    rc              = int(rc_raw) if rc_raw is not None else 0
    bstatus         = safe_str(row.get("business_status", "UNKNOWN"))

    # ── 1. rental_fit ──────────────────────────────────────────
    if   fit >= 60: score_rental_fit = 50
    elif fit >= 40: score_rental_fit = 35
    elif fit >= 20: score_rental_fit = 15
    else:           score_rental_fit = 0

    # ── 2. partner ─────────────────────────────────────────────
    score_partner = 0
    if is_partner:
        score_partner = 15
        if rc >= 3:    score_partner += 5   # 口コミ確認済み
        if fit >= 50:  score_partner += 5   # 高適合提携

    # ── 3. distance ────────────────────────────────────────────
    if   dist <=  300: score_distance = 15
    elif dist <=  500: score_distance = 12
    elif dist <=  800: score_distance = 8
    elif dist <= 1200: score_distance = 4
    elif dist <= 1500: score_distance = -5
    else:              score_distance = -20

    # ── 4. review count ────────────────────────────────────────
    if   rc >= 100: score_review_count = 25
    elif rc >= 50:  score_review_count = 20
    elif rc >= 30:  score_review_count = 15
    elif rc >= 10:  score_review_count = 10
    elif rc >= 5:   score_review_count = 6
    elif rc >= 3:   score_review_count = 3
    else:           score_review_count = 0

    # ── 5. rating ──────────────────────────────────────────────
    score_rating = 0
    if rating is not None:
        if rc <= 1:
            # 口コミ1件以下: 最大+2 (5.0/1件を過剰評価しない)
            score_rating = min(2, max(0, round((rating - 3.0))))
        elif rating >= 4.7 and rc >= 3:  score_rating = 12
        elif rating >= 4.5 and rc >= 5:  score_rating = 10
        elif rating >= 4.3 and rc >= 10: score_rating = 8
        elif rating >= 4.3 and rc >= 3:  score_rating = 6
        elif rating >= 4.0 and rc >= 10: score_rating = 5
        elif rating >= 4.0 and rc >= 5:  score_rating = 3

    # ── 6. business status ─────────────────────────────────────
    score_business_status = 0
    if bstatus in ("CLOSED_PERMANENTLY", "CLOSED_TEMPORARILY"):
        score_business_status = -9999

    # ── 7. MEO (現状は未使用・補助要素として将来拡張) ──────────
    score_meo = 0

    # ── 8. penalty ─────────────────────────────────────────────
    score_penalty = 0
    if rating is not None:
        if   rating < 3.0 and rc <= 3: score_penalty = -30
        elif rating < 3.5 and rc <= 5: score_penalty = -15
    if rc == 0:
        score_penalty = min(score_penalty, -5)
    # 非提携 + 口コミ1件: 5.0/1件のような統計的信頼性のない高評価を抑制
    if not is_partner and rc == 1:
        score_penalty -= 8

    total = (
        score_rental_fit + score_partner + score_distance +
        score_review_count + score_rating +
        score_business_status + score_meo + score_penalty
    )

    return {
        "score_rental_fit":      score_rental_fit,
        "score_partner":         score_partner,
        "score_distance":        score_distance,
        "score_review_count":    score_review_count,
        "score_rating":          score_rating,
        "score_business_status": score_business_status,
        "score_meo":             score_meo,
        "score_penalty":         score_penalty,
        "selection_score":       round(total, 1),
    }


# ============================================================
# 選定理由テキスト
# ============================================================
def build_selection_reason(row) -> str:
    parts = []
    if int(row.get("is_partner", 0) or 0) == 1:
        parts.append(f"提携(+{int(row.get('score_partner',0))})")
    fit  = safe_float(row.get("rental_fit_score"))
    if fit is not None:
        parts.append(f"fit={fit:.0f}(+{int(row.get('score_rental_fit',0))})")
    dist = safe_float(row.get("distance_to_article_station_m"))
    if dist is not None:
        parts.append(f"{dist:.0f}m(+{int(row.get('score_distance',0))})")
    rc = safe_int(row.get("google_review_count"))
    if rc:
        parts.append(f"口コミ{rc}件(+{int(row.get('score_review_count',0))})")
    r = safe_float(row.get("google_rating"))
    if r is not None:
        parts.append(f"評価{r:.1f}(+{int(row.get('score_rating',0))})")
    penalty = int(row.get("score_penalty", 0))
    if penalty < 0:
        parts.append(f"低評価ペナルティ({penalty})")
    return " | ".join(parts) if parts else "候補"


# ============================================================
# データ読み込み
# ============================================================
def load_places_map() -> dict:
    """places_api_results.csv → {studio_title: {rating, review_count}}"""
    p = Path(PLACES_CSV)
    if not p.exists():
        log.warning(f"Places API結果CSV未発見: {PLACES_CSV} → 評価なしで処理します")
        return {}
    df = pd.read_csv(p)
    result = {}
    for _, row in df.iterrows():
        t = safe_str(row.get("studio_title", ""))
        if not t:
            continue
        r  = safe_float(row.get("rating"))
        rc = safe_int(row.get("review_count"))
        # api_status != OK のものは信頼しない
        if safe_str(row.get("api_status", "OK")) not in ("OK", ""):
            continue
        # 既にある場合は口コミ数が多い方を優先
        existing = result.get(t)
        if existing and (existing.get("review_count") or 0) >= (rc or 0):
            continue
        result[t] = {"rating": r, "review_count": rc}
    log.info(f"Places API評価読み込み: {len(result)}件 ({PLACES_CSV})")
    return result


def load_excluded_titles() -> set:
    """business_status_excluded_studios.csv → 除外スタジオタイトル集合"""
    p = Path(EXCLUDED_CSV)
    if not p.exists():
        log.info(f"閉業除外CSV未発見: {EXCLUDED_CSV} → 閉業フィルタなしで処理します")
        return set()
    df = pd.read_csv(p)
    titles = set()
    for _, row in df.iterrows():
        t = safe_str(row.get("studio_title", ""))
        if t:
            titles.add(t)
    if titles:
        log.warning(f"閉業除外: {len(titles)}件 → {titles}")
    return titles


# ============================================================
# メイン
# ============================================================
def main():
    parser = argparse.ArgumentParser(description="TOP15確定 (v2)")
    parser.add_argument(
        "--article",
        default=DEFAULT_ARTICLE,
        help=f"対象記事タイトル (default: {DEFAULT_ARTICLE!r})",
    )
    args = parser.parse_args()
    target_article = args.article

    # 出力パス (記事名が変わっても対応できるよう動的生成)
    safe_slug = re.sub(r'[^\w\u3000-\u9fff]', '_', target_article)[:40]
    # 新宿記事は既存ファイル名を維持
    if "新宿" in target_article:
        out_selected_csv = str(OUTPUT_DIR / "shinjuku_station_selected_studios.csv")
        out_debug_csv    = str(OUTPUT_DIR / "shinjuku_selection_score_debug.csv")
    else:
        out_selected_csv = str(OUTPUT_DIR / f"{safe_slug}_selected_studios.csv")
        out_debug_csv    = str(OUTPUT_DIR / f"{safe_slug}_selection_score_debug.csv")
    top15_csv        = str(OUTPUT_DIR / "article_top15_plan.csv")
    group_debug_csv  = str(OUTPUT_DIR / "group_key_debug.csv")

    # ── データ読み込み ──────────────────────────────────────────
    log.info(f"候補CSV読み込み: {CANDIDATES_CSV}")
    df_cand = pd.read_csv(CANDIDATES_CSV)
    df = df_cand[df_cand["article_title"] == target_article].copy()
    log.info(f"  {target_article}: {len(df)}件")

    if df.empty:
        log.error("対象記事の候補が0件です。pipeline_top15.py を先に実行してください。")
        sys.exit(1)

    # スタジオマスタから Webサイト・電話番号を補完
    master = pd.read_csv(STUDIO_CSV)
    master_info = master[["タイトル", "Webサイト", "電話番号"]].rename(columns={
        "タイトル":  "studio_title",
        "Webサイト": "website",
        "電話番号":  "phone",
    })
    df = df.merge(master_info, on="studio_title", how="left")

    # Places API評価を結合 (places_api_results.csv)
    places_map = load_places_map()
    df["google_rating"]       = df["studio_title"].map(lambda t: places_map.get(t, {}).get("rating"))
    df["google_review_count"] = df["studio_title"].map(lambda t: places_map.get(t, {}).get("review_count"))

    # 閉業スタジオのステータスを列に追加
    excluded_titles = load_excluded_titles()
    df["business_status"] = df["studio_title"].map(
        lambda t: "CLOSED" if t in excluded_titles else "OPERATIONAL"
    )

    # ── 除外フィルタ ────────────────────────────────────────────
    n_before = len(df)

    # 1. 閉業スタジオを除外
    df_closed = df[df["business_status"] == "CLOSED"]
    if not df_closed.empty:
        log.warning(f"  閉業除外: {len(df_closed)}件 → {df_closed['studio_title'].tolist()}")
    df = df[df["business_status"] != "CLOSED"].copy()

    # 2. rental_fit_score による除外
    excl_fit = []
    keep_rows = []
    for _, row in df.iterrows():
        fit        = safe_float(row.get("rental_fit_score")) or 0.0
        is_partner = int(row.get("is_partner", 0) or 0) == 1
        if is_partner:
            ok = fit >= RENT_FIT_EXCLUDE_PARTNER
        else:
            ok = fit >= RENT_FIT_EXCLUDE_NONPARTNER
        if ok:
            keep_rows.append(row)
        else:
            excl_fit.append(row["studio_title"])
    if excl_fit:
        log.info(f"  rental_fit除外: {len(excl_fit)}件 → {excl_fit[:10]}")
    df = pd.DataFrame(keep_rows).reset_index(drop=True) if keep_rows else df.iloc[0:0]

    log.info(f"  フィルタ後: {n_before}件 → {len(df)}件")

    # ── Places API評価をスコアに使うために列を確認 ─────────────
    if "google_rating" not in df.columns:
        df["google_rating"]       = float("nan")
    if "google_review_count" not in df.columns:
        df["google_review_count"] = float("nan")

    # ── 低評価除外 (候補が十分ある場合) ──────────────────────────
    if len(df) >= LOW_RATING_EXCLUDE_THRESHOLD:
        def _is_low_rating(row):
            r  = safe_float(row.get("google_rating"))
            rc = safe_int(row.get("google_review_count")) or 0
            return r is not None and r < LOW_RATING_MAX and rc <= LOW_RATING_MIN_RC
        low_mask   = df.apply(_is_low_rating, axis=1)
        excl_low   = df[low_mask]["studio_title"].tolist()
        if excl_low:
            log.info(f"  低評価除外 (候補{len(df)}件あり): {len(excl_low)}件 → {excl_low}")
        df = df[~low_mask].copy()

    # ── グループエイリアス読み込み ──────────────────────────────
    aliases   = load_group_aliases()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # ── group_key 決定 ─────────────────────────────────────────
    debug_rows = []
    gkeys, gmethods, greasons = [], [], []
    for _, row in df.iterrows():
        title                     = str(row.get("studio_title", ""))
        gk, norm, method, reason  = get_group_key(title, aliases)
        gkeys.append(gk)
        gmethods.append(method)
        greasons.append(reason)
        debug_rows.append({
            "studio_title":    title,
            "normalized_name": norm,
            "group_key":       gk,
            "group_method":    method,
            "group_reason":    reason,
        })
    df["group_key"]     = gkeys
    df["_group_method"] = gmethods
    df["_group_reason"] = greasons

    pd.DataFrame(debug_rows).to_csv(group_debug_csv, index=False, encoding="utf-8-sig")
    log.info(f"  グループデバッグCSV → {group_debug_csv}")

    # ── 全候補スコア計算 ───────────────────────────────────────
    score_cols = [
        "score_rental_fit", "score_partner", "score_distance",
        "score_review_count", "score_rating", "score_business_status",
        "score_meo", "score_penalty", "selection_score",
    ]
    scores = df.apply(calc_score_breakdown, axis=1, result_type="expand")
    for col in score_cols:
        df[col] = scores[col]

    df["selection_reason"] = df.apply(build_selection_reason, axis=1)
    df["official_url"]     = df.get("website", pd.Series("", index=df.index)).where(
        df.get("website", pd.Series("", index=df.index)).notna(), ""
    )

    # ── debug CSV 出力 (全候補・スコア内訳) ────────────────────
    debug_score_cols = [
        "studio_title", "group_key", "is_partner", "business_status",
        "google_rating", "google_review_count",
        "rental_fit_score", "distance_to_article_station_m",
    ] + score_cols + ["selection_reason"]
    debug_score_cols = [c for c in debug_score_cols if c in df.columns]

    df_debug_score = df[debug_score_cols].copy()
    df_debug_score.insert(0, "article_title", target_article)
    df_debug_score["selected"] = False  # 後でTrue に更新

    # ── グループ化 ────────────────────────────────────────────
    df_sorted = df.sort_values("selection_score", ascending=False)
    group_reps = []
    log.info("\n=== グループキー確認 ===")

    for gkey, gdf in df_sorted.groupby("group_key", sort=False):
        gdf_s = gdf.sort_values("selection_score", ascending=False)
        rep   = gdf_s.iloc[0].copy()
        branches = gdf_s.iloc[1:]["studio_title"].tolist() if len(gdf_s) > 1 else []
        rep["branches"] = "|".join(branches)

        # internal_note
        internal_note = ""
        sname = str(rep.get("studio_title", ""))
        if "バレエ" in sname and "バレエスクール" not in sname and "スクール" not in sname:
            internal_note = BALLET_INTERNAL_NOTE
        rc  = safe_int(rep.get("google_review_count"))
        r   = safe_float(rep.get("google_rating"))
        if r is not None and r < 3.0 and (rc or 0) <= 3:
            internal_note = ("low_rating_low_reviews | " + internal_note).strip(" |")
        rep["internal_note"] = internal_note

        group_reps.append(rep)
        gm_tag = " [alias]" if rep.get("_group_method") == "alias" else ""
        log.info(
            f"  [{gkey:25s}]{gm_tag} "
            f"代表={str(rep['studio_title'])[:28]:28s} "
            f"score={rep['selection_score']:6.1f} "
            f"partner={int(rep.get('is_partner', 0) or 0)} "
            f"fit={rep.get('rental_fit_score', 0):.0f} "
            f"rc={rc or 0} r={f'{r:.1f}' if r is not None else '—'}"
            + (f"  branches={branches}" if branches else "")
        )

    df_groups = (
        pd.DataFrame(group_reps)
        .sort_values("selection_score", ascending=False)
        .reset_index(drop=True)
    )
    log.info(f"\n  グループ総数: {len(df_groups)}")

    # ── TOP15 選定 ───────────────────────────────────────────
    selected = df_groups.head(MAX_GROUPS).copy()
    selected.insert(0, "rank", range(1, len(selected) + 1))

    # debug CSV の selected 列を更新
    selected_titles = set(selected["studio_title"].tolist())
    df_debug_score["selected"] = df_debug_score["studio_title"].isin(selected_titles)
    df_debug_score.to_csv(out_debug_csv, index=False, encoding="utf-8-sig")
    log.info(f"  スコアデバッグCSV → {out_debug_csv} ({len(df_debug_score)}件)")

    # ── 問題スタジオチェック ──────────────────────────────────
    log.info("\n=== 問題スタジオチェック ===")
    found_problem = False
    for _, sr in selected.iterrows():
        sname = str(sr.get("studio_title", ""))
        for kw in WARN_KW:
            if kw in sname:
                log.warning(f"  [警告] TOP15に問題スタジオ混入: {sname} ({kw})")
                found_problem = True
    if not found_problem:
        log.info("  問題スタジオなし ✓")

    if len(selected) < 10:
        log.warning(f"  [WARNING] 掲載主体数が10件未満: {len(selected)}件")

    # ── TOP15 ログ ────────────────────────────────────────────
    n_partner = int((selected["is_partner"] == 1).sum())
    log.info(f"\n=== TOP15確定 ({target_article}) ===")
    log.info(f"  主体数: {len(selected)}  提携: {n_partner}  非提携: {len(selected) - n_partner}")
    log.info("")

    for _, r in selected.iterrows():
        dist   = r.get("distance_to_article_station_m")
        dist_s = f" {dist:.0f}m" if pd.notna(dist) else ""
        br     = str(r.get("branches", ""))
        br_s   = f"\n          branches=[{br}]" if br else ""
        note_s = f"\n          ⚠ {r.get('internal_note', '')}" if r.get("internal_note") else ""
        gm_s   = " [alias]" if r.get("_group_method") == "alias" else ""
        rc_val = safe_int(r.get("google_review_count"))
        r_val  = safe_float(r.get("google_rating"))
        log.info(
            f"  [{int(r['rank']):>2}] [P={int(r.get('is_partner',0) or 0)}] "
            f"{str(r['studio_title'])[:38]}{dist_s} "
            f"score={r['selection_score']:.1f} "
            f"fit={r.get('rental_fit_score',0):.0f} "
            f"rc={rc_val or 0} r={f'{r_val:.1f}' if r_val else '—'} "
            f"({r.get('selection_reason', '')})"
            f"{gm_s}{br_s}{note_s}"
        )

    # ── 出力列整理 ────────────────────────────────────────────
    out_cols = [
        "rank", "studio_title", "group_key", "is_partner", "dslurl",
        "address", "primary_station", "candidate_stations",
        "article_station", "distance_to_article_station_m", "article_match_method",
        "rental_fit_score", "rental_fit_reason",
        "selection_score", "selection_reason",
        "score_rental_fit", "score_partner", "score_distance",
        "score_review_count", "score_rating", "score_business_status",
        "score_meo", "score_penalty",
        "google_rating", "google_review_count",
        "official_url", "website",
        "branches", "internal_note",
    ]
    out_cols = [c for c in out_cols if c in selected.columns]
    selected[out_cols].to_csv(out_selected_csv, index=False, encoding="utf-8-sig")
    log.info(f"\n  出力: {out_selected_csv} ({len(selected)}件)")

    # ── article_top15_plan.csv 更新 ───────────────────────────
    top15_rows = []
    for _, r in selected.iterrows():
        top15_rows.append({
            "article_title":                 target_article,
            "article_type":                  "駅",
            "article_priority":              "S",
            "rank":                          int(r["rank"]),
            "studio_title":                  r["studio_title"],
            "group_key":                     r.get("group_key", ""),
            "address":                       r.get("address", ""),
            "is_partner":                    int(r.get("is_partner", 0) or 0),
            "dslurl":                        r.get("dslurl", ""),
            "primary_station":               r.get("primary_station", ""),
            "distance_to_primary_station_m": r.get("distance_to_primary_station_m", ""),
            "article_station":               r.get("article_station", ""),
            "distance_to_article_station_m": r.get("distance_to_article_station_m", ""),
            "selection_score":               r["selection_score"],
            "selection_reason":              r.get("selection_reason", ""),
            "branches":                      r.get("branches", ""),
            "internal_note":                 r.get("internal_note", ""),
        })
    pd.DataFrame(top15_rows).to_csv(top15_csv, index=False, encoding="utf-8-sig")
    log.info(f"  更新: {top15_csv} ({len(top15_rows)}件)")

    log.info("\n=== Step 3 完了 ===")


if __name__ == "__main__":
    main()
