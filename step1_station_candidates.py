#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Step 1: 駅記事 — 候補スタジオ抽出

指定した駅からhaversine距離を計算し、
rental=1 かつ 距離<=DISTANCE_THRESHOLD のスタジオを抽出する。

出力: data/pipeline/step1_{slug}_candidates.csv

使い方:
  python3 step1_station_candidates.py              # デフォルト: 新宿駅
  python3 step1_station_candidates.py 渋谷駅
"""

import sys
import math
import logging
import pandas as pd
from pathlib import Path

# ============================================================
# 設定
# ============================================================
STUDIO_CSV      = "data/master/studio_with_tags_dslurl_address_filled.csv"
STATION_CSV     = "data/master/m_station.csv"
OUTPUT_DIR      = Path("data/pipeline")
DISTANCE_THRESHOLD = 1200  # メートル

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ============================================================
# ユーティリティ
# ============================================================
def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """2点間のhaversine距離(メートル)を返す"""
    R = 6_371_000  # 地球半径(m)
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def station_name_to_slug(name: str) -> str:
    """'新宿駅' → 'shinjuku' のようなファイル名用スラグに変換"""
    # ASCII化は行わず、日本語のまま使う(ファイル名は日本語でも可)
    # ここでは単純に「駅」を除いて小文字化
    return name.replace("駅", "").strip()


# ============================================================
# メイン
# ============================================================
def main(target_station_name: str = "新宿駅") -> None:
    # 「新宿駅」→「新宿」に正規化
    station_key = target_station_name.replace("駅", "").strip()

    # ---- 駅マスタから対象駅のlat/lon取得 ----
    ms = pd.read_csv(STATION_CSV)
    matched = ms[ms["station_name"] == station_key]
    if matched.empty:
        # 部分一致フォールバック
        matched = ms[ms["station_name"].str.contains(station_key, na=False)]
    if matched.empty:
        log.error(f"駅マスタに '{station_key}' が見つかりません")
        sys.exit(1)
    if len(matched) > 1:
        log.warning(f"'{station_key}' が複数ヒット({len(matched)}件) → importance_score上位を使用")
        matched = matched.sort_values("importance_score", ascending=False)

    station_row = matched.iloc[0]
    st_lat = float(station_row["latitude"])
    st_lon = float(station_row["longitude"])
    log.info(f"対象駅: {station_row['station_name']} ({station_row['lines'].split('|')[0]}...)")
    log.info(f"  lat={st_lat:.6f}, lon={st_lon:.6f}")

    # ---- スタジオCSV読み込み ----
    df = pd.read_csv(STUDIO_CSV)
    log.info(f"スタジオ総数: {len(df)}")

    # rental フィルタ
    df_rental = df[df["rental"] == 1].copy()
    log.info(f"rental=1: {len(df_rental)}件")

    # lat/lon が欠損しているスタジオを除外
    before = len(df_rental)
    df_rental = df_rental.dropna(subset=["latitude", "longitude"])
    if len(df_rental) < before:
        log.warning(f"lat/lon欠損を除外: {before - len(df_rental)}件")

    # ---- haversine距離計算 ----
    df_rental["dist_to_station_m"] = df_rental.apply(
        lambda r: haversine_m(r["latitude"], r["longitude"], st_lat, st_lon),
        axis=1,
    ).round(1)

    # ---- 距離フィルタ ----
    df_candidates = df_rental[df_rental["dist_to_station_m"] <= DISTANCE_THRESHOLD].copy()
    df_candidates = df_candidates.sort_values("dist_to_station_m")
    log.info(f"距離{DISTANCE_THRESHOLD}m以内: {len(df_candidates)}件")

    # ---- partner フラグ再計算 ----
    df_candidates["is_partner"] = df_candidates["dslurl"].notna().astype(int)

    # ---- 検証用サマリ ----
    partner_count = df_candidates["is_partner"].sum()
    log.info(f"  うち提携スタジオ: {partner_count}件")
    log.info(f"  うち非提携:       {len(df_candidates) - partner_count}件")

    # ---- 出力 ----
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    slug = station_key
    out_path = OUTPUT_DIR / f"step1_{slug}_candidates.csv"

    # 出力列を整理
    out_cols = [
        "タイトル", "住所", "dslurl", "is_partner",
        "dist_to_station_m",
        "latitude", "longitude",
        "primary_station", "distance_to_primary_station_m",
        "candidate_stations", "ward", "primary_area",
        "rental", "article_tags",
        "Webサイト", "電話番号", "営業時間",
    ]
    out_cols = [c for c in out_cols if c in df_candidates.columns]
    df_out = df_candidates[out_cols].reset_index(drop=True)

    df_out.to_csv(out_path, index=False, encoding="utf-8-sig")
    log.info(f"出力: {out_path}  ({len(df_out)}件)")

    # ---- 上位10件をコンソール表示 ----
    print("\n=== 距離順 上位10件 ===")
    display_cols = ["タイトル", "dist_to_station_m", "is_partner", "primary_station", "ward"]
    display_cols = [c for c in display_cols if c in df_out.columns]
    print(df_out[display_cols].head(10).to_string(index=False))
    print(f"\n合計 {len(df_out)} 件 → {out_path}")


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "新宿駅"
    main(target)
