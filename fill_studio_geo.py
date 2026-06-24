#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fill_studio_geo.py
住所から H列以降の空欄を補完する

補完対象:
  longitude, latitude         ← Google Geocoding API
  primary_station             ← Overpass API (周辺駅)
  distance_to_primary_station_m
  candidate_stations
  station_count
  ward                        ← 住所パース
  primary_area                ← ward → area マッピング
  candidate_areas             ← primary_area を代入

使い方:
  python3 fill_studio_geo.py                   # 全空欄を補完
  python3 fill_studio_geo.py --partners-only   # 提携スタジオのみ
"""

import os
import re
import json
import math
import time
import logging
import argparse
import requests
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
GOOGLE_API_KEY    = os.environ.get("GOOGLE_PLACES_API_KEY", "")
STUDIO_CSV        = "data/master/studio_with_tags_dslurl_address_filled.csv"
CHECKPOINT_JSON   = "checkpoints/fill_geo_checkpoint.json"
STATION_RADIUS    = 2000   # 周辺駅検索半径 (m)
MAX_CANDIDATES    = 10     # candidate_stations の最大件数
GEOCODE_QPS       = 2      # Google Geocoding API QPS
PLACES_QPS        = 5      # Google Places API QPS
BATCH_SAVE        = 10     # チェックポイント保存間隔

# ============================================================
# 区 → primary_area マッピング（既存データから生成）
# ============================================================
WARD_TO_AREA: dict[str, str] = {
    "さいたま市大宮区":  "大宮",
    "三鷹市":           "三鷹",
    "世田谷区":         "下北沢",
    "中央区":           "銀座",
    "中野区":           "中野",
    "八王子市":         "八王子",
    "北区":             "赤羽",
    "千代田区":         "秋葉原",
    "台東区":           "上野",
    "品川区":           "品川",
    "国分寺市":         "国分寺",
    "国立市":           "国立",
    "墨田区":           "錦糸町",
    "多摩市":           "多摩センター",
    "大田区":           "蒲田",
    "小平市":           "小平",
    "小金井市":         "武蔵小金井",
    "川崎市川崎区":     "川崎",
    "川崎市幸区":       "川崎",
    "府中市":           "府中",
    "文京区":           "後楽園",
    "新宿区":           "新宿",
    "日野市":           "日野",
    "杉並区":           "高円寺",
    "東久留米市":       "東久留米",
    "東村山市":         "東村山",
    "板橋区":           "成増",
    "横浜市保土ヶ谷区": "保土ケ谷",
    "横浜市西区":       "横浜",
    "横浜市中区":       "横浜",
    "武蔵村山市":       "武蔵村山",
    "武蔵野市":         "吉祥寺",
    "江戸川区":         "西葛西",
    "江東区":           "豊洲",
    "清瀬市":           "清瀬",
    "渋谷区":           "渋谷",
    "狛江市":           "狛江",
    "町田市":           "町田",
    "目黒区":           "中目黒",
    "福岡市中央区":     "天神南",
    "福生市":           "福生",
    "稲城市":           "稲城",
    "立川市":           "立川",
    "練馬区":           "練馬",
    "荒川区":           "日暮里",
    "葛飾区":           "新小岩",
    "西東京市":         "ひばりヶ丘",
    "調布市":           "調布",
    "豊島区":           "池袋",
    "足立区":           "北千住",
    "青梅市":           "青梅",
    # 港区は最寄り駅で判断（後述）
}

# 港区の場合：最寄り駅名に含まれるキーワード → エリア
MINATO_STATION_AREA: list[tuple[str, str]] = [
    ("六本木", "六本木"),
    ("麻布",   "六本木"),
    ("赤羽橋", "六本木"),
    ("広尾",   "六本木"),
    ("白金",   "六本木"),
    ("浜松町", "浜松町"),
    ("大門",   "浜松町"),
    ("汐留",   "浜松町"),
    ("三田",   "浜松町"),
    ("田町",   "品川"),
]

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
# ユーティリティ
# ============================================================
def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> int:
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi   = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return round(2 * R * math.asin(math.sqrt(a)))


def parse_ward(address: str) -> str | None:
    """住所文字列から区/市区を抽出"""
    if not address or address == "nan":
        return None
    # 〇〇市△△区 (政令指定都市)
    m = re.search(r"([^\s都道府県]+?市[^\s都道府県]+?区)", address)
    if m:
        return m.group(1)
    # 東京都□□区
    m = re.search(r"東京都([^\s]+?区)", address)
    if m:
        return m.group(1)
    # 〇〇区
    m = re.search(r"([^\s都道府県]+?区)", address)
    if m:
        return m.group(1)
    # 〇〇市
    m = re.search(r"([^\s都道府県]+?市)", address)
    if m:
        return m.group(1)
    return None


def ward_to_area(ward: str | None, primary_station: str | None = None) -> str | None:
    if not ward:
        return None
    if ward == "港区":
        # 最寄り駅名で判断
        if primary_station:
            for kw, area in MINATO_STATION_AREA:
                if kw in primary_station:
                    return area
        return "六本木"  # デフォルト
    return WARD_TO_AREA.get(ward)


# ============================================================
# Google Geocoding API
# ============================================================
def geocode(address: str, api_key: str) -> tuple[float | None, float | None]:
    url = "https://maps.googleapis.com/maps/api/geocode/json"
    try:
        r = requests.get(
            url,
            params={"address": address, "key": api_key},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        if data.get("status") == "OK" and data["results"]:
            loc = data["results"][0]["geometry"]["location"]
            return float(loc["lat"]), float(loc["lng"])
    except Exception as e:
        log.warning(f"    Geocoding例外: {e}")
    return None, None


# ============================================================
# Overpass API（周辺駅検索）
# ============================================================
PLACES_NEARBY_URL = "https://places.googleapis.com/v1/places:searchNearby"


def find_nearby_stations(lat: float, lon: float, api_key: str, radius: int = STATION_RADIUS) -> list[dict]:
    """
    Google Places API (New/v1) Nearby Search で周辺の鉄道・地下鉄駅を取得し距離順に返す。
    """
    body = {
        "includedTypes": ["train_station", "subway_station", "light_rail_station"],
        "maxResultCount": 20,
        "locationRestriction": {
            "circle": {
                "center": {"latitude": lat, "longitude": lon},
                "radius": float(radius),
            }
        },
        "languageCode": "ja",
    }
    headers = {
        "X-Goog-Api-Key":  api_key,
        "X-Goog-FieldMask": "places.displayName,places.location",
        "Content-Type":    "application/json",
    }
    try:
        r = requests.post(PLACES_NEARBY_URL, json=body, headers=headers, timeout=10)
        r.raise_for_status()
        stations: list[dict] = []
        seen: set[str] = set()
        for place in r.json().get("places", []):
            name = place.get("displayName", {}).get("text", "")
            if not name:
                continue
            if "駅" not in name:
                name += "駅"
            if name in seen:
                continue
            seen.add(name)
            loc  = place.get("location", {})
            plat = float(loc.get("latitude",  0))
            plon = float(loc.get("longitude", 0))
            dist = haversine(lat, lon, plat, plon)
            stations.append({"name": name, "dist": dist})
        return sorted(stations, key=lambda x: x["dist"])
    except Exception as e:
        log.warning(f"    Places Nearby Search例外: {e}")
        return []


# ============================================================
# メイン処理
# ============================================================
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--partners-only", action="store_true",
                        help="提携スタジオ（dslurl有）のみ処理")
    args = parser.parse_args()

    if not GOOGLE_API_KEY:
        log.error("GOOGLE_PLACES_API_KEY が未設定です（.env を確認してください）")
        return

    # --- データ読み込み ---
    df = pd.read_csv(STUDIO_CSV)
    log.info(f"スタジオ総数: {len(df)}件")

    # 処理対象: lat/lon 空 OR primary_station 空
    need_latlon  = df["longitude"].isna()
    need_station = df["primary_station"].isna() | (df["primary_station"].astype(str) == "なし")
    todo_mask    = need_latlon | need_station

    if args.partners_only:
        is_partner = df["dslurl"].notna() & (df["dslurl"].astype(str).str.strip() != "")
        todo_mask  = todo_mask & is_partner

    todo = df[todo_mask].copy()
    log.info(
        f"処理対象: {len(todo)}件  "
        f"(lat/lon空:{need_latlon.sum()}, station空:{need_station.sum()})"
    )

    # --- チェックポイント ---
    cp_path    = Path(CHECKPOINT_JSON)
    checkpoint: dict = {}
    if cp_path.exists():
        checkpoint = json.loads(cp_path.read_text(encoding="utf-8"))
        log.info(f"チェックポイント: {len(checkpoint)}件処理済み")

    todo = todo[~todo["タイトル"].isin(checkpoint)].copy()
    log.info(f"未処理: {len(todo)}件")

    if todo.empty:
        log.info("処理対象なし → CSV更新のみ実行")
    else:
        # --- 処理ループ ---
        geocode_interval = 1.0 / GEOCODE_QPS
        last_geocode = 0.0

        for i, (_, row) in enumerate(
            tqdm(todo.iterrows(), total=len(todo), desc="ジオコーディング＋駅検索"),
            start=1,
        ):
            name    = str(row["タイトル"])
            address = str(row.get("住所", "") or "")
            result: dict = {}

            # --- ① lat/lon が空 → Geocoding ---
            lat = row.get("latitude")
            lon = row.get("longitude")
            if pd.isna(lat) or lat is None:
                if not address or address == "nan":
                    log.warning(f"  [{name}] 住所なしのためスキップ")
                    checkpoint[name] = result
                    continue

                elapsed = time.time() - last_geocode
                wait    = geocode_interval - elapsed
                if wait > 0:
                    time.sleep(wait)
                last_geocode = time.time()

                lat, lon = geocode(address, GOOGLE_API_KEY)
                if lat is None:
                    log.warning(f"  [{name}] ジオコーディング失敗")
                    checkpoint[name] = result
                    continue

                result["latitude"]  = lat
                result["longitude"] = lon
            else:
                lat = float(lat)
                lon = float(row["longitude"])

            # --- ② station が空 → Google Places Nearby Search ---
            _s = row.get("primary_station")
            station_already = "" if pd.isna(_s) else str(_s).strip()
            if not station_already or station_already == "なし":
                stations = find_nearby_stations(lat, lon, GOOGLE_API_KEY)
                if stations:
                    primary    = stations[0]
                    candidates = stations[:MAX_CANDIDATES]
                    result["primary_station"]                = primary["name"]
                    result["distance_to_primary_station_m"]  = primary["dist"]
                    result["candidate_stations"]             = "|".join(s["name"] for s in candidates)
                    result["station_count"]                  = len(candidates)
                else:
                    result["primary_station"] = "なし"
                    result["station_count"]   = 0
                    log.warning(f"  [{name}] 周辺{STATION_RADIUS}m以内に駅なし")
            else:
                result["primary_station"] = station_already

            # --- ③ ward が空 → 住所パース ---
            _w = row.get("ward")
            ward_now = "" if pd.isna(_w) else str(_w).strip()
            if not ward_now:
                ward = parse_ward(address)
                if ward:
                    result["ward"] = ward
                    ward_now = ward

            # --- ④ primary_area が空 → ward→area ---
            _a = row.get("primary_area")
            area_now = "" if pd.isna(_a) else str(_a).strip()
            if not area_now:
                area = ward_to_area(ward_now, result.get("primary_station"))
                if area:
                    result["primary_area"]    = area
                    result["candidate_areas"] = area  # 最低限、primary_area を代入

            checkpoint[name] = result

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
        log.info(f"チェックポイント保存: {CHECKPOINT_JSON}")

    # --- CSV更新 ---
    update_cols = [
        "longitude", "latitude",
        "primary_station", "distance_to_primary_station_m",
        "candidate_stations", "station_count",
        "ward", "primary_area", "candidate_areas",
    ]

    updated_count = 0
    for name, data in checkpoint.items():
        idx = df[df["タイトル"] == name].index
        if idx.empty:
            continue
        i0 = idx[0]
        changed = False
        for col in update_cols:
            val = data.get(col)
            if val is None:
                continue
            if isinstance(val, float) and math.isnan(val):
                continue
            if pd.isna(df.at[i0, col]) or str(df.at[i0, col]) in ("", "nan", "なし"):
                df.at[i0, col] = val
                changed = True
        if changed:
            updated_count += 1

    df.to_csv(STUDIO_CSV, index=False, encoding="utf-8-sig")
    log.info(f"CSV更新完了: {updated_count}件更新 → {STUDIO_CSV}")

    # --- サマリ ---
    df2 = pd.read_csv(STUDIO_CSV)
    print("\n" + "=" * 60)
    print("■ 補完後の空欄状況")
    print("=" * 60)
    for col in update_cols:
        empty = df2[col].isna().sum()
        print(f"  {col:42}: {empty:3}件空欄")
    print("=" * 60)


if __name__ == "__main__":
    main()
