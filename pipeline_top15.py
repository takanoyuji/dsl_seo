#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Dance Studio Lab SEO パイプライン
記事ごとのTOP15スタジオ選定 (Places API取得前の仮選定)

出力ファイル:
  1. studios_partner_flagged.csv
  2. article_candidates.csv
  3. article_candidate_summary.csv
  4. places_api_targets.csv
  5. article_top15_plan.csv
"""

import math
import re
import sys
import argparse
import logging
import unicodedata
import pandas as pd
from pathlib import Path
from tqdm import tqdm

# ============================================================
# 設定 (ファイル名をここで変更)
# ============================================================
STUDIO_CSV         = "data/master/studio_with_tags_dslurl_address_filled.csv"
ARTICLE_XLSX       = "data/master/DSL記事一覧.xlsx"
STATION_CSV        = "data/master/m_station.csv"
ROOM_CSV           = "data/master/studio_room_all_columns_excel.csv"
OUTPUT_DIR         = Path("data/pipeline")
MAX_API_TARGETS    = 1500
DEBUG_SHINJUKU_CSV = str(OUTPUT_DIR / "debug_shinjuku_station_candidates.csv")

# ── 軽量版レンタル適合スコア用キーワード (後方互換用, 現在は未使用) ─
_NON_DANCE_KW       = ["音楽スタジオ", "サウンドスタジオ", "ホットヨガ", "ゴールドジム"]
_NON_DANCE_LIGHT_KW = ["ヨガ", "ピラティス", "yoga", "pilates", "gym", "fitness", "フィットネス"]
_RENTAL_LOW_KW      = ["スクール", "アカデミー", "school", "academy"]
_RENTAL_HIGH_KW     = ["レンタル", "rental", "ダンス", "dance"]

# ── 候補品質フィルタ定数 ─────────────────────────────────────────
RENTAL_FIT_EXCLUDE_THRESHOLD    = 20  # 非提携スタジオの除外スコア下限
RENTAL_FIT_PARTNER_EXCLUDE_MIN  = 0   # 提携スタジオの除外スコア下限
# 提携(dslurl有)でも必ず除外するキーワード (レンタルダンス名称でも優先)
ABSOLUTE_EXCLUDE_KW = ["ピラティス", "フォトスタジオ", "写真館"]
DEBUG_SHINJUKU_EXCLUDED_CSV = str(OUTPUT_DIR / "debug_shinjuku_excluded_candidates.csv")

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
def first_col(df: pd.DataFrame, candidates: list, default: str | None = None) -> str | None:
    """列名の揺れを吸収して最初にマッチした列名を返す"""
    for c in candidates:
        if c in df.columns:
            return c
    return default


def pipe_contains(series: pd.Series, value: str) -> pd.Series:
    """パイプ区切りの列に value が含まれるか判定する (大文字小文字一致)"""
    return series.fillna("").apply(lambda s: value in s.split("|"))


def safe_float(val) -> float | None:
    """数値変換失敗時に None を返す"""
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _calc_fit_score(studio_title: str, dslurl: str = "") -> int:
    """レンタルダンス適合スコア簡易版 (debug CSV 用)"""
    score = 5
    name_l = studio_title.lower()
    if dslurl and str(dslurl).strip():
        score += 3
    if any(kw.lower() in name_l for kw in _RENTAL_HIGH_KW):
        score += 2
    if any(kw.lower() in name_l for kw in _RENTAL_LOW_KW):
        score -= 4
    if any(kw.lower() in name_l for kw in _NON_DANCE_KW):
        score -= 5
    if any(kw.lower() in name_l for kw in _NON_DANCE_LIGHT_KW):
        score -= 3
    return max(0, min(10, score))


# ============================================================
# haversine距離 & 駅マスタ
# ============================================================
def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """2点間のhaversine距離(メートル)を返す"""
    R = 6_371_000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def load_station_master(path: str) -> pd.DataFrame:
    """m_station.csv を読み込み、列名を正規化して返す"""
    log.info(f"駅マスタ読み込み: {path}")
    df = pd.read_csv(path)
    rename = {}
    for col in df.columns:
        col_l = col.lower().strip()
        if col_l in ("station_name", "駅名", "name"):
            rename[col] = "station_name"
        elif col_l in ("latitude", "lat", "緯度"):
            rename[col] = "latitude"
        elif col_l in ("longitude", "lon", "lng", "経度"):
            rename[col] = "longitude"
        elif col_l == "importance_score":
            rename[col] = "importance_score"
        elif col_l == "importance_rank":
            rename[col] = "importance_rank"
        elif col_l in ("prefecture", "都道府県"):
            rename[col] = "prefecture"
        elif col_l in ("city", "市区町村"):
            rename[col] = "city"
    df = df.rename(columns=rename)
    log.info(f"  駅数: {len(df)}")
    return df


def find_station_coords(
    station_master: pd.DataFrame, station_name: str
) -> tuple[float, float] | None:
    """
    '新宿駅' または '新宿' → (lat, lon) を返す。
    複数ヒット時は importance_score 上位を優先。
    見つからない場合は None を返す。
    """
    key = station_name.replace("駅", "").strip()
    matched = station_master[station_master["station_name"] == key]
    if matched.empty:
        matched = station_master[station_master["station_name"].str.contains(key, na=False)]
    if matched.empty:
        return None
    if len(matched) > 1 and "importance_score" in matched.columns:
        matched = matched.sort_values("importance_score", ascending=False)
    row = matched.iloc[0]
    lat = safe_float(row.get("latitude"))
    lon = safe_float(row.get("longitude"))
    if lat is None or lon is None:
        return None
    return (lat, lon)


# ============================================================
# 候補品質フィルタ
# ============================================================
def calc_rental_fit_score_full(
    studio_title: str,
    dslurl:       str = "",
    article_tags: str = "",
    website:      str = "",
    rental:       int = 0,
) -> tuple[int, str]:
    """
    レンタルダンス適合スコア (v2 / pipeline_top15.py 用)
    Returns: (score, reason_str)
    """
    score  = 0
    rsns   = []
    name   = str(studio_title or "")
    name_l = name.lower()
    tags_l = str(article_tags or "").lower()
    web_l  = str(website or "").lower()

    # ── プラス要素 ──────────────────────────────────────────
    if dslurl and str(dslurl).strip():
        score += 30; rsns.append("+30(dslurl)")
    if rental == 1:
        score += 20; rsns.append("+20(rental=1)")
    if "レンタルダンススタジオ" in name or "rental dance studio" in name_l:
        score += 40; rsns.append("+40(レンタルダンス)")
    elif "レンタルスタジオ" in name or "rental studio" in name_l:
        score += 30; rsns.append("+30(レンタルスタジオ)")
    elif "ダンススタジオ" in name or "dance studio" in name_l:
        score += 25; rsns.append("+25(ダンススタジオ)")
    if any(kw in web_l for kw in ["rental", "レンタル", "貸しスタジオ", "ダンス練習", "個人練習"]):
        score += 20; rsns.append("+20(web)")
    if "type:レンタル" in tags_l:
        score += 20; rsns.append("+20(tag)")

    # ── マイナス要素 ────────────────────────────────────────
    if "ピラティス" in name or "pilates" in name_l:
        score -= 80; rsns.append("-80(ピラティス)")
    if "フォトスタジオ" in name:
        score -= 80; rsns.append("-80(フォトスタジオ)")
    if "写真" in name:
        score -= 60; rsns.append("-60(写真)")
    if "バレエスクール" in name:
        score -= 60; rsns.append("-60(バレエスクール)")
    if "スクール" in name:
        score -= 50; rsns.append("-50(スクール)")
    if "アカデミー" in name or "academy" in name_l:
        score -= 50; rsns.append("-50(アカデミー)")
    if "教室" in name:
        score -= 40; rsns.append("-40(教室)")
    if "音楽スタジオ" in name or "music" in name_l:
        score -= 40; rsns.append("-40(音楽系)")
    if "サウンドスタジオ" in name or "sound studio" in name_l:
        score -= 40; rsns.append("-40(サウンド系)")

    return score, "|".join(rsns)


def should_exclude_by_fit(
    studio_title:     str,
    dslurl:           str,
    rental_fit_score: int,
) -> tuple[bool, str]:
    """rental_fit_score に基づく除外判定。Returns: (exclude, reason)"""
    name    = str(studio_title or "")
    name_l  = name.lower()
    is_part = bool(dslurl and str(dslurl).strip())

    # 絶対除外キーワード (レンタルダンス名称でも優先)
    for kw in ABSOLUTE_EXCLUDE_KW:
        if kw in name:
            return True, f"絶対除外({kw})"
    if "sound studio" in name_l:
        return True, "絶対除外(sound studio)"

    # レンタルダンススタジオ名称は (絶対除外以外) 残す
    if "レンタルダンススタジオ" in name:
        return False, ""

    # 提携: score < 0 は除外
    if is_part and rental_fit_score < RENTAL_FIT_PARTNER_EXCLUDE_MIN:
        return True, f"提携だがscore={rental_fit_score}<{RENTAL_FIT_PARTNER_EXCLUDE_MIN}"

    # 非提携: score < threshold は除外
    if not is_part and rental_fit_score < RENTAL_FIT_EXCLUDE_THRESHOLD:
        return True, f"非提携でscore={rental_fit_score}<{RENTAL_FIT_EXCLUDE_THRESHOLD}"

    return False, ""


def check_suspicious_location(
    row:           pd.Series,
    article_station: str,
    station_master: pd.DataFrame,
) -> tuple[int, str]:
    """
    primary_station と article_station の距離が大幅に離れているのに
    haversine 距離が近い場合を lat/lon 異常として検出する。
    Returns: (flag: 0|1, reason: str)
    """
    dist_to_art = safe_float(row.get("distance_to_article_station_m"))
    if dist_to_art is None or dist_to_art > 2000:
        return 0, ""

    primary_st = str(row.get("primary_station", "") or "").replace("駅", "").strip()
    art_st     = article_station.replace("駅", "").strip()

    if not primary_st or primary_st == art_st:
        return 0, ""

    art_coords = find_station_coords(station_master, art_st)
    pri_coords = find_station_coords(station_master, primary_st)
    if art_coords is None or pri_coords is None:
        return 0, ""

    st_dist = haversine_m(art_coords[0], art_coords[1], pri_coords[0], pri_coords[1])

    STATION_FAR  = 5000   # primary_station が 5km 以上離れている
    STUDIO_CLOSE = 1500   # なのに studio が 1500m 以内 → 異常

    if st_dist > STATION_FAR and dist_to_art < STUDIO_CLOSE:
        return 1, (
            f"primary_st={primary_st} は {art_st} から {st_dist:.0f}m 離れているが "
            f"haversine 距離 {dist_to_art:.0f}m は近すぎる (lat/lon 異常疑い)"
        )
    return 0, ""


def apply_candidate_filters(
    matched:        pd.DataFrame,
    article_type:   str,
    article_station: str,
    station_master: pd.DataFrame,
    title_col:      str,
    web_col:        str,
) -> tuple[pd.DataFrame, dict]:
    """
    候補スタジオに品質フィルタを適用し、除外フラグを付与する。

    追加列: rental_fit_score, rental_fit_reason,
            suspicious_location, suspicious_reason,
            fallback_allowed, fallback_reason,
            excluded_by_fit, exclude_reason

    Returns: (df_with_flags, stats_dict)
    """
    if matched.empty:
        empty = matched.copy()
        for col in ["rental_fit_score", "rental_fit_reason",
                    "suspicious_location", "suspicious_reason",
                    "fallback_allowed", "fallback_reason",
                    "excluded_by_fit", "exclude_reason"]:
            empty[col] = []
        return empty, {
            "initial": 0, "suspicious_excluded": 0,
            "fallback_excluded": 0, "fit_excluded": 0, "valid": 0,
        }

    df = matched.copy()

    # ── rental_fit_score ──────────────────────────────────
    def _s(val) -> str:
        """pandas NaN / None を安全に空文字列へ変換 (nan文字列化を防ぐ)"""
        if val is None:
            return ""
        try:
            if pd.isna(val):
                return ""
        except (TypeError, ValueError):
            pass
        return str(val).strip()

    fit_results = df.apply(
        lambda r: calc_rental_fit_score_full(
            _s(r.get(title_col, "")),
            _s(r.get("dslurl", "")),
            _s(r.get("article_tags", "")),
            _s(r.get(web_col, "")),
            int(r.get("rental", 0) or 0),
        ), axis=1,
    )
    df["rental_fit_score"]  = [v[0] for v in fit_results]
    df["rental_fit_reason"] = [v[1] for v in fit_results]

    # ── suspicious_location (駅記事のみ) ──────────────────
    df["suspicious_location"] = 0
    df["suspicious_reason"]   = ""
    if article_type == "駅" and article_station:
        for idx, row in df.iterrows():
            flag, reason = check_suspicious_location(row, article_station, station_master)
            df.at[idx, "suspicious_location"] = flag
            df.at[idx, "suspicious_reason"]   = reason

    # ── fallback_allowed (駅記事のみ) ─────────────────────
    df["fallback_allowed"] = 1
    df["fallback_reason"]  = ""
    if article_type == "駅":
        for idx, row in df.iterrows():
            meth = str(row.get("article_match_method", ""))
            dist = safe_float(row.get("distance_to_article_station_m"))
            if "fallback" not in meth or dist is None:
                continue
            if dist <= 1200:
                df.at[idx, "fallback_reason"] = f"fallback:dist={dist:.0f}m≤1200m"
            elif dist <= 1500:
                fit  = row.get("rental_fit_score", 0)
                is_p = bool(_s(row.get("dslurl", "")))
                if is_p and fit >= RENTAL_FIT_EXCLUDE_THRESHOLD:
                    df.at[idx, "fallback_reason"] = (
                        f"fallback:1200<{dist:.0f}m≤1500m+提携+fit={fit}"
                    )
                else:
                    df.at[idx, "fallback_allowed"] = 0
                    df.at[idx, "fallback_reason"]  = (
                        f"fallback:dist={dist:.0f}m>1200m 非提携or低fit"
                    )
            else:
                df.at[idx, "fallback_allowed"] = 0
                df.at[idx, "fallback_reason"]  = f"fallback:dist={dist:.0f}m>1500m"

    # ── excluded_by_fit ───────────────────────────────────
    df["excluded_by_fit"] = 0
    df["exclude_reason"]  = ""

    n_susp = n_fb = n_fit = 0
    for idx, row in df.iterrows():
        if row.get("suspicious_location", 0) == 1:
            df.at[idx, "excluded_by_fit"] = 1
            df.at[idx, "exclude_reason"]  = f"suspicious: {row.get('suspicious_reason', '')}"
            n_susp += 1
            continue
        if row.get("fallback_allowed", 1) == 0:
            df.at[idx, "excluded_by_fit"] = 1
            df.at[idx, "exclude_reason"]  = row.get("fallback_reason", "fallback距離超過")
            n_fb += 1
            continue
        exc, reason = should_exclude_by_fit(
            _s(row.get(title_col, "")),
            _s(row.get("dslurl", "")),
            int(row.get("rental_fit_score", 0)),
        )
        if exc:
            df.at[idx, "excluded_by_fit"] = 1
            df.at[idx, "exclude_reason"]  = reason
            n_fit += 1

    n_valid = int((df["excluded_by_fit"] == 0).sum())
    stats = {
        "initial":            len(df),
        "suspicious_excluded": n_susp,
        "fallback_excluded":   n_fb,
        "fit_excluded":        n_fit,
        "valid":               n_valid,
    }
    return df, stats


# ============================================================
# 1. スタジオ読み込み & partner フラグ付与
# ============================================================
def load_studios(path: str) -> pd.DataFrame:
    log.info(f"スタジオCSV読み込み: {path}")
    df = pd.read_csv(path)

    # 必須列チェック
    if "dslurl" not in df.columns:
        raise ValueError("studio CSV に dslurl 列が見つかりません")

    # 同住所エントリ間で dslurl を伝播
    # （DSL掲載エントリと外部スクレイプエントリが同住所で別名の場合に対応）
    addr_col = next((c for c in ["住所", "address"] if c in df.columns), None)
    if addr_col:
        has_url = df["dslurl"].notna() & (df["dslurl"].astype(str).str.strip() != "")
        addr_dslurl_map = (
            df[has_url & df[addr_col].notna()]
            .groupby(addr_col)["dslurl"]
            .first()
            .to_dict()
        )
        propagate_mask = (
            ~has_url
            & df[addr_col].notna()
            & df[addr_col].isin(addr_dslurl_map)
        )
        n_propagated = int(propagate_mask.sum())
        if n_propagated:
            df.loc[propagate_mask, "dslurl"] = df.loc[propagate_mask, addr_col].map(addr_dslurl_map)
            log.info(f"  同住所からの dslurl 伝播: {n_propagated}件")

    # partner フラグを上書き生成
    df["is_partner"]      = df["dslurl"].notna().astype(int)
    df["partner_link"]    = df["dslurl"].fillna("")
    df["partner_priority"] = df["is_partner"].map({1: 100, 0: 0})

    log.info(
        f"  スタジオ総数={len(df)}  提携={df['is_partner'].sum()}  "
        f"非提携={(df['is_partner']==0).sum()}"
    )
    return df


# ============================================================
# 1b. feature タグ補完 (ルームデータ + 名称ベース)
# ============================================================
def enrich_feature_tags(studios: pd.DataFrame) -> pd.DataFrame:
    """
    ルームデータ・スタジオ名称から feature タグを in-memory で補完する。

    追加タグ:
      feature:shooting   … room_shoot_flg='t' または名称に「撮影」含む
      feature:mirror     … room_mirror >= 1
      feature:soundproof … 名称に「防音」含む
      feature:midnight   … feature:24h と同義エイリアス (深夜利用OK記事用)
    """
    tags_col = "article_tags"
    if tags_col not in studios.columns:
        studios[tags_col] = ""

    shoot_urls:  set = set()
    mirror_urls: set = set()

    def add_tag(mask: pd.Series, tag: str):
        def _add(x):
            x = str(x) if pd.notna(x) else ""
            existing = [t.strip() for t in x.split("|") if t.strip()]
            if tag not in existing:
                existing.append(tag)
            return "|".join(existing)
        studios.loc[mask, tags_col] = studios.loc[mask, tags_col].apply(_add)

    # ── ルームデータ ────────────────────────────────────────────
    room_path = Path(ROOM_CSV)
    if room_path.exists():
        try:
            room_df = pd.read_csv(room_path)
            shoot_urls = set(
                room_df[room_df["room_shoot_flg"] == "t"]["dslurl"].dropna().tolist()
            )
            mirror_num = pd.to_numeric(room_df["room_mirror"], errors="coerce").fillna(0)
            mirror_urls = set(
                room_df[mirror_num >= 1]["dslurl"].dropna().tolist()
            )
            shoot_mask  = studios["dslurl"].isin(shoot_urls)
            mirror_mask = studios["dslurl"].isin(mirror_urls)
            add_tag(shoot_mask,  "feature:shooting")
            add_tag(mirror_mask, "feature:mirror")
            log.info(
                f"  [enrich] feature:shooting={int(shoot_mask.sum())}件 "
                f"feature:mirror={int(mirror_mask.sum())}件 (ルームデータ)"
            )
        except Exception as e:
            log.warning(f"  [enrich] ルームCSV読み込みエラー: {e}")
    else:
        log.warning(f"  [enrich] ルームCSV 見つからず: {ROOM_CSV}")

    # ── 名称ベース ───────────────────────────────────────────────
    title_col = next((c for c in ["タイトル", "title", "name"] if c in studios.columns), None)
    if title_col:
        title_s = studios[title_col].fillna("")
        # 撮影: ルームデータ未取得の非提携スタジオのみ
        shoot_name_mask = (
            title_s.str.contains("撮影", na=False) &
            ~studios["dslurl"].isin(shoot_urls)
        )
        if int(shoot_name_mask.sum()) > 0:
            add_tag(shoot_name_mask, "feature:shooting")
            log.info(f"  [enrich] feature:shooting={int(shoot_name_mask.sum())}件 (名称)")
        # 防音
        soundproof_mask = title_s.str.contains("防音", na=False)
        if int(soundproof_mask.sum()) > 0:
            add_tag(soundproof_mask, "feature:soundproof")
            log.info(f"  [enrich] feature:soundproof={int(soundproof_mask.sum())}件 (名称)")

    # ── feature:24h → feature:midnight エイリアス ────────────────
    midnight_src = studios[tags_col].fillna("").str.contains("feature:24h", regex=False)
    add_tag(midnight_src, "feature:midnight")
    log.info(f"  [enrich] feature:midnight エイリアス={int(midnight_src.sum())}件")

    return studios


# ============================================================
# 2. 記事一覧読み込み
# ============================================================
def load_articles(path: str) -> pd.DataFrame:
    log.info(f"記事一覧読み込み: {path}")
    df = pd.read_excel(path)

    rename_map = {
        first_col(df, ["記事タイトル", "article_title"]):       "article_title",
        first_col(df, ["記事タイプ",   "article_type"]):        "article_type",
        first_col(df, ["作り方（対象スタジオ選定）", "条件", "condition"]): "condition",
        first_col(df, ["優先度",       "priority"]):             "article_priority",
    }
    rename_map = {k: v for k, v in rename_map.items() if k is not None}
    df = df.rename(columns=rename_map)

    log.info(
        f"  記事数={len(df)}  "
        f"タイプ={df['article_type'].value_counts().to_dict()}  "
        f"優先度={df['article_priority'].value_counts().to_dict()}"
    )
    return df


# ============================================================
# 3. 記事条件パーサー
# ============================================================
def parse_condition(cond_str) -> dict:
    """
    条件文字列を解析して辞書を返す。
    対応パターン:
      primary_area=<area>
      distance_to_<駅名><=<dist>
      distance_to_primary_station<=<dist>
      feature_<name>=1
      capacity_<name>=1
      <other>=1   (price_low など)
      rental=1
    """
    result = {
        "primary_area":       None,   # エリア名
        "station":            None,   # 駅名
        "station_dist_max":   None,   # 駅からの最大距離(m)
        "features":           [],     # タグ条件 [feature:xxx, ...]
        "distance_primary_max": None, # primary_station距離上限
        "rental_required":    True,
    }

    if pd.isna(cond_str):
        return result

    for part in cond_str.split("AND"):
        part = part.strip()

        # rental=1
        if re.fullmatch(r"rental=1", part):
            result["rental_required"] = True
            continue

        # primary_area=<value>
        m = re.fullmatch(r"primary_area=(.+)", part)
        if m:
            result["primary_area"] = m.group(1).strip()
            continue

        # distance_to_<駅名><=<dist>
        m = re.fullmatch(r"distance_to_(.+駅)<=(\d+)", part)
        if m:
            result["station"]          = m.group(1).strip()
            result["station_dist_max"] = int(m.group(2))
            continue

        # distance_to_primary_station<=<dist>
        m = re.fullmatch(r"distance_to_primary_station<=(\d+)", part)
        if m:
            result["distance_primary_max"] = int(m.group(1))
            continue

        # feature_<name>=1 → tag: feature:<name>
        m = re.fullmatch(r"feature_(.+)=1", part)
        if m:
            result["features"].append(f"feature:{m.group(1)}")
            continue

        # capacity_<name>=1 → tag: feature:capacity_<name>
        m = re.fullmatch(r"capacity_(.+)=1", part)
        if m:
            result["features"].append(f"feature:capacity_{m.group(1)}")
            continue

        # <other>=1 (price_low など) → tag: feature:<other>
        m = re.fullmatch(r"(\w+)=1", part)
        if m and m.group(1) != "rental":
            result["features"].append(f"feature:{m.group(1)}")
            continue

    return result


# ============================================================
# 4. 1記事分のスタジオマッチング
# ============================================================
def match_studios(
    studios:        pd.DataFrame,
    art:            pd.Series,
    station_master: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """記事条件に合うスタジオを抽出し article_match_reason 列を付与して返す"""
    cond = parse_condition(art["condition"])
    n = len(studios)

    mask    = pd.Series([True]  * n, index=studios.index)
    reasons = pd.Series([""] * n, index=studios.index)

    # 駅記事用トラッキング列
    article_station_col     = pd.Series([""]          * n, index=studios.index, dtype=str)
    dist_to_article_station = pd.Series([float("nan")] * n, index=studios.index, dtype=float)
    match_method_col        = pd.Series([""]          * n, index=studios.index, dtype=str)

    def add_reason(sub_mask, text):
        reasons[sub_mask] = reasons[sub_mask].apply(
            lambda r: (r + " " + text).strip()
        )

    # rental フィルタ
    if cond["rental_required"] and "rental" in studios.columns:
        mask &= studios["rental"].fillna(0) == 1

    # ── エリア記事 ────────────────────────────────────────────
    if cond["primary_area"]:
        area = cond["primary_area"]
        m1 = studios["primary_area"].fillna("") == area
        m2 = pipe_contains(studios["candidate_areas"], area)
        m3 = pipe_contains(studios["candidate_stations"], f"{area}駅")
        m4 = studios["article_tags"].fillna("").str.contains(
                f"area:{re.escape(area)}", regex=True)
        m5 = studios["article_tags"].fillna("").str.contains(
                f"station:{re.escape(area)}駅", regex=True)

        area_mask = m1 | m2 | m3 | m4 | m5
        mask &= area_mask
        add_reason(m1 & area_mask, f"primary_area={area}")
        add_reason(~m1 & m2 & area_mask, f"candidate_area:{area}")
        add_reason(~m1 & ~m2 & (m3 | m4 | m5) & area_mask, f"tag/station:{area}")

    # ── 駅記事 ───────────────────────────────────────────────
    if cond["station"] and cond["station_dist_max"]:
        station  = cond["station"]
        max_dist = cond["station_dist_max"]

        article_station_col[:] = station

        # m_station.csv から対象駅の座標を取得
        coords = None
        if station_master is not None:
            coords = find_station_coords(station_master, station)
            if coords is None:
                log.warning(f"  [{station}] 駅座標が m_station.csv に見つかりません → fallback使用")

        if coords is not None:
            # ── haversine距離ベースのマッチング ──────────────
            st_lat, st_lon = coords
            log.info(f"  [{station}] 座標取得: lat={st_lat:.6f}, lon={st_lon:.6f}")

            def _dist(row):
                lat = safe_float(row.get("latitude"))
                lon = safe_float(row.get("longitude"))
                if lat is None or lon is None:
                    return float("nan")
                return haversine_m(lat, lon, st_lat, st_lon)

            dist_series = studios.apply(_dist, axis=1)
            dist_to_article_station[:] = dist_series.round(1)

            # プライマリ: haversine距離 <= max_dist
            md = dist_series.fillna(9999) <= max_dist
            # fallback: candidate_stations / article_tags 文字列マッチ
            mc = pipe_contains(studios["candidate_stations"], station)
            mt = studios["article_tags"].fillna("").str.contains(
                    f"station:{re.escape(station)}", regex=True)

            station_mask = md | mc | mt
            mask &= station_mask

            match_method_col[md  & station_mask]            = "station_distance"
            match_method_col[~md & mc  & station_mask]      = "candidate_stations_fallback"
            match_method_col[~md & ~mc & mt & station_mask] = "article_tags_fallback"

            add_reason(md & station_mask,
                       f"haversine:{station}(≤{max_dist}m)")
            add_reason(~md & mc & station_mask,
                       f"candidate_station_fallback:{station}")
            add_reason(~md & ~mc & mt & station_mask,
                       f"tag_fallback:station:{station}")

            n_dist     = int((md & station_mask).sum())
            n_fallback = int((~md & station_mask).sum())
            log.info(f"  [{station}] haversine≤{max_dist}m: {n_dist}件 / fallback: {n_fallback}件")

        else:
            # ── 旧ロジック: primary_station距離 + 文字列マッチ ──
            dist_vals = studios["distance_to_primary_station_m"].apply(safe_float)
            mp = (studios["primary_station"].fillna("") == station) & (dist_vals.fillna(9999) <= max_dist)
            mc = pipe_contains(studios["candidate_stations"], station)
            mt = studios["article_tags"].fillna("").str.contains(
                    f"station:{re.escape(station)}", regex=True)

            station_mask = mp | mc | mt
            mask &= station_mask

            match_method_col[mp  & station_mask]            = "primary_station_dist"
            match_method_col[~mp & mc  & station_mask]      = "candidate_stations_fallback"
            match_method_col[~mp & ~mc & mt & station_mask] = "article_tags_fallback"

            add_reason(mp, f"primary_station:{station}(≤{max_dist}m)")
            add_reason(~mp & mc & station_mask, f"candidate_station:{station}")
            add_reason(~mp & ~mc & mt & station_mask, f"tag:station:{station}")

    # ── 条件記事: primary_station 距離 ────────────────────────
    if cond["distance_primary_max"] is not None:
        dmax = cond["distance_primary_max"]
        dist_vals = studios["distance_to_primary_station_m"].apply(safe_float)
        dm = dist_vals.fillna(9999) <= dmax
        mask &= dm
        add_reason(dm, f"駅近(≤{dmax}m)")

    # ── 条件記事: feature タグ ───────────────────────────────
    for feat in cond["features"]:
        fm = studios["article_tags"].fillna("").str.contains(
                re.escape(feat), regex=True)
        mask &= fm
        add_reason(fm, f"tag:{feat}")

    matched = studios[mask].copy()
    matched["article_match_reason"]          = reasons[mask].values
    matched["article_station"]               = article_station_col[mask].values
    matched["distance_to_article_station_m"] = dist_to_article_station[mask].values
    matched["article_match_method"]          = match_method_col[mask].values
    return matched


# ============================================================
# 5. メインパイプライン
# ============================================================
def main():
    parser = argparse.ArgumentParser(description="DSL SEO パイプライン TOP15選定")
    parser.add_argument("--article", help="処理する記事タイトル（省略時は全記事）")
    args = parser.parse_args()

    # ── データロード ─────────────────────────────────────────
    studios        = load_studios(STUDIO_CSV)
    studios        = enrich_feature_tags(studios)   # feature タグ補完
    articles       = load_articles(ARTICLE_XLSX)
    station_master = load_station_master(STATION_CSV)

    if args.article:
        articles = articles[articles["article_title"] == args.article].copy()
        if articles.empty:
            log.error(f"記事が見つかりません: {args.article}")
            all_titles = load_articles(ARTICLE_XLSX)["article_title"].tolist()
            log.info(f"利用可能な記事タイトル例: {all_titles[:5]}")
            sys.exit(1)
        log.info(f"単一記事モード: {args.article}")

    title_col = first_col(studios, ["タイトル", "title", "name"])
    addr_col  = first_col(studios, ["住所", "address"])
    web_col   = first_col(studios, ["Webサイト", "website"])
    phone_col = first_col(studios, ["電話番号", "phone"])

    # ── 1. studios_partner_flagged.csv ───────────────────────
    out_path = OUTPUT_DIR / "studios_partner_flagged.csv"
    studios.to_csv(out_path, index=False, encoding="utf-8-sig")
    log.info(f"[1/5] {out_path} 出力完了 ({len(studios)}件)")

    # ── 2 & 3. article_candidates / summary ──────────────────
    log.info("[2/5] 記事ごとの候補スタジオ抽出開始...")

    cand_rows           = []
    summary_rows        = []
    excluded_debug_rows = []

    for _, art in tqdm(articles.iterrows(), total=len(articles), desc="マッチング"):
        matched = match_studios(studios, art, station_master)

        # article_station を matched から取得 (品質フィルタ用)
        article_station_name = ""
        if not matched.empty and "article_station" in matched.columns:
            vals = matched["article_station"].dropna().unique()
            if len(vals) > 0:
                article_station_name = str(vals[0])

        # 品質フィルタ適用
        filtered, stats = apply_candidate_filters(
            matched,
            str(art.get("article_type", "")),
            article_station_name,
            station_master,
            title_col or "タイトル",
            web_col   or "Webサイト",
        )
        valid_matched    = filtered[filtered["excluded_by_fit"] == 0]
        excluded_matched = filtered[filtered["excluded_by_fit"] == 1]

        # フィルタ統計ログ (対象記事のみ)
        is_debug_art = (
            art["article_title"] == "新宿駅のレンタルダンススタジオおすすめ"
            or (args.article is not None and art["article_title"] == args.article)
        )
        if is_debug_art:
            log.info(
                f"  [フィルタ] 初期候補={stats['initial']} "
                f"suspicious除外={stats['suspicious_excluded']} "
                f"fallback除外={stats['fallback_excluded']} "
                f"fit除外={stats['fit_excluded']} "
                f"有効={stats['valid']}"
            )
            # 除外デバッグ行を収集 (新宿駅記事のみ)
            if art["article_title"] == "新宿駅のレンタルダンススタジオおすすめ":
                for _, row in excluded_matched.iterrows():
                    excluded_debug_rows.append({
                        "studio_title":               row[title_col] if title_col else "",
                        "address":                    row[addr_col]  if addr_col  else "",
                        "dslurl":                     row.get("dslurl", ""),
                        "is_partner":                 row["is_partner"],
                        "distance_to_article_station_m": row.get("distance_to_article_station_m"),
                        "article_match_method":       row.get("article_match_method", ""),
                        "rental_fit_score":           row.get("rental_fit_score", ""),
                        "rental_fit_reason":          row.get("rental_fit_reason", ""),
                        "suspicious_location":        row.get("suspicious_location", 0),
                        "suspicious_reason":          row.get("suspicious_reason", ""),
                        "fallback_allowed":           row.get("fallback_allowed", 1),
                        "fallback_reason":            row.get("fallback_reason", ""),
                        "excluded_by_fit":            row.get("excluded_by_fit", 0),
                        "exclude_reason":             row.get("exclude_reason", ""),
                    })

        # valid candidates に追加
        for _, row in valid_matched.iterrows():
            cand_rows.append({
                "article_title":              art["article_title"],
                "article_type":               art["article_type"],
                "article_priority":           art["article_priority"],
                "studio_title":               row[title_col] if title_col else "",
                "address":                    row[addr_col]  if addr_col  else "",
                "dslurl":                     row.get("dslurl", ""),
                "is_partner":                 row["is_partner"],
                "primary_area":               row.get("primary_area", ""),
                "candidate_areas":            row.get("candidate_areas", ""),
                "primary_station":            row.get("primary_station", ""),
                "candidate_stations":         row.get("candidate_stations", ""),
                "distance_to_primary_station_m":  row.get("distance_to_primary_station_m", ""),
                "article_station":               row.get("article_station", ""),
                "distance_to_article_station_m": row.get("distance_to_article_station_m"),
                "article_match_method":          row.get("article_match_method", ""),
                "article_match_reason":          row["article_match_reason"],
                "rental_fit_score":              row.get("rental_fit_score", ""),
                "rental_fit_reason":             row.get("rental_fit_reason", ""),
                "suspicious_location":           row.get("suspicious_location", 0),
                "fallback_allowed":              row.get("fallback_allowed", 1),
            })

        n_all     = len(valid_matched)
        n_partner = int(valid_matched["is_partner"].sum()) if n_all > 0 else 0
        sample    = "|".join(
            valid_matched[title_col].head(3).tolist()
        ) if n_all > 0 and title_col else ""

        status = "OK" if n_all >= 15 else ("少ない" if n_all >= 5 else "要修正")

        summary_rows.append({
            "article_title":    art["article_title"],
            "article_type":     art["article_type"],
            "article_priority": art["article_priority"],
            "candidate_count":  n_all,
            "partner_count":    n_partner,
            "non_partner_count": n_all - n_partner,
            "sample_studios":   sample,
            "status":           status,
        })

    df_cand    = pd.DataFrame(cand_rows)
    df_summary = pd.DataFrame(summary_rows)

    # 除外候補CSV (新宿駅記事)
    if excluded_debug_rows:
        df_excl = pd.DataFrame(excluded_debug_rows)
        df_excl.to_csv(DEBUG_SHINJUKU_EXCLUDED_CSV, index=False, encoding="utf-8-sig")
        log.info(f"除外候補CSV → {DEBUG_SHINJUKU_EXCLUDED_CSV} ({len(df_excl)}件)")

    df_cand.to_csv(OUTPUT_DIR / "article_candidates.csv", index=False, encoding="utf-8-sig")
    log.info(f"[2/5] article_candidates.csv 出力完了 ({len(df_cand)}レコード)")

    df_summary.to_csv(OUTPUT_DIR / "article_candidate_summary.csv", index=False, encoding="utf-8-sig")
    log.info(f"[3/5] article_candidate_summary.csv 出力完了")

    # ── サマリー表示 ─────────────────────────────────────────
    print("\n" + "="*70)
    print("■ 記事候補サマリー")
    print("="*70)
    for _, r in df_summary.iterrows():
        flag = "⚠" if r["status"] != "OK" else " "
        print(
            f"{flag} [{r['article_priority']}] {r['article_title'][:25]:<25} "
            f"候補:{r['candidate_count']:>4}  提携:{r['partner_count']:>3}  "
            f"状態:{r['status']}"
        )
    print("="*70 + "\n")

    # ── 4. places_api_targets.csv ────────────────────────────
    log.info("[4/5] Places API対象スタジオ選定...")

    if len(df_cand) == 0:
        log.warning("候補スタジオが0件のため places_api_targets.csv をスキップ")
    else:
        # 候補少記事に出るスタジオを特定
        few_articles = df_summary[df_summary["candidate_count"] < 15]["article_title"].tolist()
        few_studios  = set(
            df_cand[df_cand["article_title"].isin(few_articles)]["studio_title"].unique()
        )

        # スタジオ単位で集計
        grp = df_cand.groupby("studio_title")
        stats = grp.agg(
            candidate_article_count=("article_title", "nunique"),
            s_article_count=("article_priority", lambda x: (x == "S").sum()),
            is_partner=("is_partner", "max"),
            dslurl=("dslurl", "first"),
            address=("address", "first"),
        ).reset_index()

        stats["in_few_candidate_article"] = stats["studio_title"].isin(few_studios).astype(int)

        # API優先スコア
        stats["api_priority_score"] = (
            stats["is_partner"]                 * 50 +
            stats["s_article_count"]            * 20 +
            stats["candidate_article_count"]    *  5 +
            stats["in_few_candidate_article"]   * 15
        )

        def build_reason(row):
            parts = []
            if row["is_partner"]:                    parts.append("提携スタジオ")
            if row["s_article_count"] > 0:           parts.append(f"S優先度記事×{int(row['s_article_count'])}")
            if row["candidate_article_count"] > 1:   parts.append(f"複数記事候補×{int(row['candidate_article_count'])}")
            if row["in_few_candidate_article"]:      parts.append("候補少記事に出現")
            return "|".join(parts) or "一般候補"

        stats["api_target_reason"] = stats.apply(build_reason, axis=1)

        # ウェブサイト・電話を付与
        info = studios[[title_col, web_col, phone_col]].rename(columns={
            title_col: "studio_title",
            web_col:   "website",
            phone_col: "phone",
        }) if web_col and phone_col else pd.DataFrame(columns=["studio_title","website","phone"])

        stats = stats.merge(info, on="studio_title", how="left")

        api_targets = (
            stats
            .sort_values("api_priority_score", ascending=False)
            .head(MAX_API_TARGETS)
            [[
                "studio_title", "address", "website", "phone", "dslurl",
                "is_partner", "candidate_article_count", "s_article_count",
                "api_priority_score", "api_target_reason",
            ]]
        )
        api_targets.to_csv(
            OUTPUT_DIR / "places_api_targets.csv", index=False, encoding="utf-8-sig"
        )
        log.info(f"[4/5] places_api_targets.csv 出力完了 ({len(api_targets)}件)")

    # ── 5. article_top15_plan.csv ────────────────────────────
    log.info("[5/5] 仮TOP15選定...")

    if len(df_cand) == 0:
        log.warning("候補スタジオが0件のため article_top15_plan.csv をスキップ")
    else:
        # スタジオ単位の掲載記事数 (スコア加点用)
        multi_map = (
            df_cand.groupby("studio_title")["article_title"]
            .nunique()
            .rename("candidate_article_count")
        )
        df_cand = df_cand.join(multi_map, on="studio_title")

        top15_rows = []

        for _, art in tqdm(articles.iterrows(), total=len(articles), desc="TOP15選定"):
            art_cand = df_cand[df_cand["article_title"] == art["article_title"]].copy()
            if art_cand.empty:
                continue

            # 重複排除 (同一スタジオが複数マッチしている場合)
            art_cand = art_cand.drop_duplicates("studio_title")

            # 同一住所の重複排除（提携優先・高スコア優先で1件に絞る）
            def _norm_addr(a: str) -> str:
                """住所正規化（全角→半角・丁目変換・番地部分のみ抽出）"""
                s = unicodedata.normalize('NFKC', str(a or ''))
                s = re.sub(r'[\u2010-\u2015\u2212\uff0d]', '-', s)
                s = re.sub(r'〒?\s*\d{3}-\d{4}\s*', '', s)
                s = s.lower()
                s = re.sub(r'(\d+)丁目', r'\1-', s)
                s = re.sub(r'(\d+)番地?', r'\1-', s)
                ms = re.search(r'(\d+(?:-\d+)+)', s)
                if ms:
                    prefix = re.sub(r'[\s\u3000]', '', s[:ms.start()])
                    s = prefix + ms.group(1)
                else:
                    s = re.sub(r'[\s\u3000]', '', s)
                return s.rstrip('-_.,')
            _addr = art_cand["address"].apply(_norm_addr)
            art_cand = art_cand.assign(_addr_norm=_addr)
            _has_addr = art_cand["_addr_norm"].str.len() > 5  # 空・極短は除外判定対象外
            _dedup = (
                art_cand[_has_addr]
                .sort_values(["is_partner", "rental_fit_score"], ascending=False)
                .drop_duplicates("_addr_norm", keep="first")
            )
            art_cand = pd.concat([art_cand[~_has_addr], _dedup]).drop(columns=["_addr_norm"])

            prio_score = {"S": 20, "A": 10, "B": 0}.get(art["article_priority"], 0)

            def calc_score(row) -> float:
                sc = 0
                if row["is_partner"] == 1:
                    sc += 100
                sc += prio_score
                d = safe_float(row.get("distance_to_primary_station_m"))
                if d is not None:
                    sc += 15 if d <= 500 else (8 if d <= 1000 else 0)
                sc += min(int(row.get("candidate_article_count", 1)) - 1, 5) * 2
                return sc

            art_cand["selection_score"] = art_cand.apply(calc_score, axis=1)
            art_cand = art_cand.sort_values("selection_score", ascending=False)

            # ── 検証対象かどうかを判定 ────────────────────────
            is_target = (
                art["article_title"] == "新宿駅のレンタルダンススタジオおすすめ"
                or (args.article is not None and art["article_title"] == args.article)
            )

            # ── 検証ログ ─────────────────────────────────────
            if is_target:
                n_p_cand = int((art_cand["is_partner"] == 1).sum())
                log.info(f"  === 検証: {art['article_title']} ===")
                log.info(f"  最終候補: {len(art_cand)}件 / dslurl有: {n_p_cand}件")
                log.info(f"  候補スコア順 上位20件:")
                for _, r in art_cand.head(20).iterrows():
                    meth = r.get("article_match_method", "")
                    dist = r.get("distance_to_article_station_m")
                    dist_str = f" {dist:.0f}m" if pd.notna(dist) else ""
                    log.info(
                        f"    [partner={r['is_partner']}] {r['studio_title']}"
                        f" score={r['selection_score']:.1f}{dist_str} ({meth})"
                    )

            # ── debug CSV 出力 (新宿駅記事) ──────────────────
            if art["article_title"] == "新宿駅のレンタルダンススタジオおすすめ":
                debug_cand = art_cand.copy()
                st_latlon = studios[["タイトル", "latitude", "longitude"]].rename(
                    columns={"タイトル": "studio_title"}
                )
                debug_cand = debug_cand.merge(st_latlon, on="studio_title", how="left")
                debug_cols = [
                    "article_title", "studio_title", "address", "dslurl", "is_partner",
                    "latitude", "longitude",
                    "primary_station", "candidate_stations",
                    "article_station", "distance_to_article_station_m", "article_match_method",
                    "rental_fit_score", "rental_fit_reason",
                    "suspicious_location", "suspicious_reason",
                    "fallback_allowed", "fallback_reason",
                    "selection_score",
                ]
                debug_cols = [c for c in debug_cols if c in debug_cand.columns]
                debug_cand[debug_cols].to_csv(
                    DEBUG_SHINJUKU_CSV, index=False, encoding="utf-8-sig"
                )
                log.info(f"  debug CSV → {DEBUG_SHINJUKU_CSV} ({len(debug_cand)}件)")

            # 提携: 最大8件、残りを非提携で埋める
            partners     = art_cand[art_cand["is_partner"] == 1]
            non_partners = art_cand[art_cand["is_partner"] == 0]

            n_p   = min(len(partners), 8)
            n_np  = min(len(non_partners), 15 - n_p)

            selected = (
                pd.concat([partners.head(n_p), non_partners.head(n_np)])
                .drop_duplicates("studio_title")
                .sort_values("selection_score", ascending=False)
                .head(15)
            )

            # ── 問題スタジオ警告 ─────────────────────────────
            WARN_KW = ["ピラティス", "スクール", "音楽スタジオ", "サウンドスタジオ", "フォトスタジオ"]
            for _, sr in selected.iterrows():
                sname = str(sr.get("studio_title", ""))
                for kw in WARN_KW:
                    if kw in sname:
                        log.warning(f"  [問題スタジオ] TOP15混入: {sname} ({kw})")
                        break

            for rank, (_, row) in enumerate(selected.iterrows(), 1):
                reason_parts = []
                if row["is_partner"] == 1:
                    reason_parts.append("提携")
                d = safe_float(row.get("distance_to_primary_station_m"))
                if d is not None:
                    reason_parts.append(
                        "駅近500m以内" if d <= 500 else ("駅近1000m以内" if d <= 1000 else "")
                    )
                if int(row.get("candidate_article_count", 1)) > 1:
                    reason_parts.append("複数記事候補")
                reason_parts.append(row.get("article_match_reason", ""))
                reason_parts = [r for r in reason_parts if r]

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
                    "selection_score":             row["selection_score"],
                    "selection_reason":            " | ".join(reason_parts),
                })

        df_top15 = pd.DataFrame(top15_rows)
        df_top15.to_csv(
            OUTPUT_DIR / "article_top15_plan.csv", index=False, encoding="utf-8-sig"
        )
        log.info(f"[5/5] article_top15_plan.csv 出力完了 ({len(df_top15)}件)")

    log.info("=== 全処理完了 ===")


if __name__ == "__main__":
    main()
