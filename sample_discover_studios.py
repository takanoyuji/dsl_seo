#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Google Places API (New) Text Search でダンススタジオを探索するサンプル
現在のマスタCSVと比較し、未収録スタジオを確認する。

使い方:
  python3 sample_discover_studios.py
"""

import os, time, json, unicodedata, re
import requests
import pandas as pd
from pathlib import Path

# .env 読み込み
_env = Path(".env")
if _env.exists():
    for line in _env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

API_KEY    = os.environ.get("GOOGLE_PLACES_API_KEY", "")
MASTER_CSV = "data/master/studio_with_tags_dslurl_address_filled.csv"

# ── サンプルエリア（緯度経度 + 検索半径m）──────────────────────────────
SAMPLE_AREAS = [
    {"name": "新宿",   "lat": 35.6896, "lng": 139.7006, "radius": 1000},
    {"name": "渋谷",   "lat": 35.6584, "lng": 139.7022, "radius": 1000},
    {"name": "池袋",   "lat": 35.7295, "lng": 139.7109, "radius": 1000},
    {"name": "上野",   "lat": 35.7141, "lng": 139.7774, "radius": 1000},
    {"name": "吉祥寺", "lat": 35.7018, "lng": 139.5796, "radius": 1000},
]

# 検索クエリ（複数でより広くヒット）
QUERIES = [
    "レンタルダンススタジオ",
    "ダンス レンタルスタジオ",
    "rental dance studio",
]


def normalize(s: str) -> str:
    """比較用正規化"""
    s = unicodedata.normalize("NFKC", str(s or "")).lower()
    s = re.sub(r"[\s\u3000　\-_・。、]", "", s)
    return s


def text_search(query: str, lat: float, lng: float, radius: int) -> list[dict]:
    """Places API (New) Text Search"""
    url = "https://places.googleapis.com/v1/places:searchText"
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": API_KEY,
        "X-Goog-FieldMask": "places.displayName,places.formattedAddress,places.rating,places.userRatingCount,places.websiteUri,places.id",
    }
    body = {
        "textQuery": query,
        "locationBias": {
            "circle": {
                "center": {"latitude": lat, "longitude": lng},
                "radius": float(radius),
            }
        },
        "languageCode": "ja",
        "maxResultCount": 20,
    }
    resp = requests.post(url, headers=headers, json=body, timeout=15)
    if resp.status_code != 200:
        print(f"  API Error {resp.status_code}: {resp.text[:200]}")
        return []
    return resp.json().get("places", [])


def main():
    if not API_KEY:
        print("ERROR: GOOGLE_PLACES_API_KEY が設定されていません")
        return

    # マスタCSV読み込み
    df_master = pd.read_csv(MASTER_CSV)
    master_names = {normalize(n) for n in df_master["タイトル"].dropna()}
    master_addrs = {normalize(a) for a in df_master["住所"].dropna()}
    print(f"現在のマスタ: {len(df_master)}件\n")

    all_found: dict[str, dict] = {}  # place_id → 情報

    for area in SAMPLE_AREAS:
        print(f"=== {area['name']} ===")
        area_new = 0
        for query in QUERIES:
            places = text_search(query, area["lat"], area["lng"], area["radius"])
            time.sleep(0.3)
            for p in places:
                pid   = p.get("id", "")
                name  = p.get("displayName", {}).get("text", "")
                addr  = p.get("formattedAddress", "")
                rating = p.get("rating")
                rc     = p.get("userRatingCount")
                web    = p.get("websiteUri", "")

                if pid in all_found:
                    continue
                all_found[pid] = {
                    "name": name, "address": addr,
                    "rating": rating, "review_count": rc,
                    "website": web, "area": area["name"],
                }

                # マスタに存在するか判定
                n_norm = normalize(name)
                a_norm = normalize(addr)
                in_master = (n_norm in master_names or
                             any(n_norm in m or m in n_norm for m in master_names if len(m) > 4) or
                             a_norm in master_addrs)
                status = "既存" if in_master else "★未収録"
                if not in_master:
                    area_new += 1
                print(f"  [{status}] {name} / {addr[:30]} "
                      f"★{rating or '-'}({rc or 0}件)")

        print(f"  → このエリアで未収録: {area_new}件\n")

    # 集計
    new_places = [v for v in all_found.values()
                  if not any(normalize(v["name"]) in m or m in normalize(v["name"])
                             for m in master_names if len(m) > 4)]

    print("=" * 60)
    print(f"サンプル{len(SAMPLE_AREAS)}エリア合計")
    print(f"  APIで発見: {len(all_found)}件")
    print(f"  未収録推定: {len(new_places)}件")
    print(f"  既存カバー率: {(len(all_found)-len(new_places))/max(len(all_found),1)*100:.0f}%")

    if new_places:
        print("\n未収録スタジオ（上位10件）:")
        for p in sorted(new_places, key=lambda x: -(x["rating"] or 0))[:10]:
            print(f"  {p['name']} / {p['area']} / ★{p['rating'] or '-'}({p['review_count'] or 0}件)")
            print(f"    {p['address']}")


if __name__ == "__main__":
    main()
