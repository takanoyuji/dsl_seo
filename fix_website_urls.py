#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
空になった Webサイト列を Google Places API で補完するスクリプト。

対象: studio_with_tags_dslurl_address_filled.csv の Webサイトが空のスタジオ
処理:
  1. Places API (v1) Text Search で各スタジオを検索
  2. websiteUri が取れたら URL チェック
  3. OK なら master CSV の Webサイト列を更新

使い方:
  python3 fix_website_urls.py          # 全件（再開可能）
  python3 fix_website_urls.py --limit 20
"""

import os
import re
import time
import json
import logging
import argparse
import requests
import pandas as pd
from pathlib import Path
from difflib import SequenceMatcher

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
API_KEY       = os.environ.get("GOOGLE_PLACES_API_KEY", "")
MASTER_CSV    = "data/master/studio_with_tags_dslurl_address_filled.csv"
RESULTS_CSV   = "data/pipeline/fix_website_urls_results.csv"
CHECKPOINT    = "checkpoints/fix_website_urls_checkpoint.json"

QPS_LIMIT     = 5      # 1秒あたり最大リクエスト数
RETRY_MAX     = 3
RETRY_WAIT    = 2.0
BATCH_SAVE    = 10

URL_TIMEOUT   = 10     # URL疎通確認タイムアウト（秒）
MATCH_THRESH  = 0.45   # 名前類似度の最低ライン

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
}

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
def normalize(s: str) -> str:
    if not s:
        return ""
    s = s.lower()
    s = s.translate(str.maketrans(
        "ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺ"
        "ａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚ"
        "０１２３４５６７８９",
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789",
    ))
    s = re.sub(r"[\s\-_・・　]+", "", s)
    return s


def name_similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, normalize(a), normalize(b)).ratio()


def build_query(title: str, address: str) -> str:
    addr_clean = re.sub(r"〒\d{3}-\d{4}\s*", "", str(address) if pd.notna(address) else "")
    addr_short = re.sub(r"(\d+丁目.*)$", "", addr_clean).strip()
    if not addr_short:
        addr_short = "東京"
    return f"{title} {addr_short}"


# ============================================================
# Places API (v1)
# ============================================================
PLACES_ENDPOINT = "https://places.googleapis.com/v1/places:searchText"
FIELD_MASK = (
    "places.id,"
    "places.displayName,"
    "places.formattedAddress,"
    "places.websiteUri,"
    "places.rating,"
    "places.userRatingCount"
)


def search_place(title: str, address: str) -> dict:
    """Places API v1 で検索し place 情報を返す。"""
    query = build_query(title, address)
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": API_KEY,
        "X-Goog-FieldMask": FIELD_MASK,
    }
    body = {
        "textQuery": query,
        "languageCode": "ja",
        "regionCode": "JP",
        "maxResultCount": 1,
    }

    for attempt in range(1, RETRY_MAX + 1):
        try:
            resp = requests.post(PLACES_ENDPOINT, headers=headers, json=body, timeout=10)
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as e:
            log.warning(f"  [attempt {attempt}] API エラー ({title[:20]}): {e}")
            if attempt < RETRY_MAX:
                time.sleep(RETRY_WAIT * attempt)
            continue

        places = data.get("places", [])
        if not places:
            return {"status": "NOT_FOUND", "query": query}

        p = places[0]
        website = p.get("websiteUri", "")
        google_name = p.get("displayName", {}).get("text", "")
        sim = name_similarity(title, google_name)

        return {
            "status":        "OK",
            "query":         query,
            "place_id":      p.get("id", ""),
            "google_name":   google_name,
            "google_address": p.get("formattedAddress", ""),
            "website_uri":   website,
            "rating":        p.get("rating"),
            "review_count":  p.get("userRatingCount"),
            "match_confidence": round(sim, 3),
        }

    return {"status": "ERROR", "query": query, "error": "max retries exceeded"}


# ============================================================
# URL 疎通確認
# ============================================================
def check_url_ok(url: str) -> bool:
    """URL が 200/301/302 系で応答するか確認する。"""
    if not url or not url.startswith("http"):
        return False
    try:
        resp = requests.head(url, timeout=URL_TIMEOUT, allow_redirects=True,
                             headers=HEADERS)
        if resp.status_code in (405, 400):
            resp = requests.get(url, timeout=URL_TIMEOUT, allow_redirects=True,
                                stream=True, headers=HEADERS)
            resp.close()
        return resp.status_code < 400
    except Exception:
        return False


# ============================================================
# チェックポイント I/O
# ============================================================
def load_checkpoint() -> dict:
    p = Path(CHECKPOINT)
    if p.exists():
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        log.info(f"チェックポイント読み込み: {len(data)} 件")
        return data
    return {}


def save_checkpoint(data: dict):
    Path(CHECKPOINT).parent.mkdir(parents=True, exist_ok=True)
    with open(CHECKPOINT, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ============================================================
# メイン
# ============================================================
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0, help="処理件数上限（0=全件）")
    args = parser.parse_args()

    if not API_KEY:
        log.error("GOOGLE_PLACES_API_KEY が未設定です。.env を確認してください。")
        return

    # --- マスタCSV読み込み ---
    master = pd.read_csv(MASTER_CSV)
    # Webサイトが空（NaN または文字列 "nan"）のスタジオを対象
    empty_mask = master["Webサイト"].isna() | (master["Webサイト"].astype(str).str.strip() == "nan")
    targets = master[empty_mask][["タイトル", "住所", "pref"]].copy()
    targets = targets.dropna(subset=["タイトル"])

    if args.limit:
        targets = targets.head(args.limit)

    log.info(f"対象スタジオ: {len(targets)} 件（Webサイト空）")

    # --- チェックポイント読み込み ---
    checkpoint = load_checkpoint()
    todo = targets[~targets["タイトル"].isin(checkpoint)]
    log.info(f"未処理: {len(todo)} 件 / 処理済み: {len(checkpoint)} 件")

    # --- API 呼び出しループ ---
    interval = 1.0 / QPS_LIMIT
    last_call = 0.0

    for i, (_, row) in enumerate(todo.iterrows(), start=1):
        elapsed = time.time() - last_call
        wait = interval - elapsed
        if wait > 0:
            time.sleep(wait)
        last_call = time.time()

        title   = str(row["タイトル"])
        address = row.get("住所", "")

        result = search_place(title, address)
        checkpoint[title] = result

        if result["status"] == "OK":
            site = result.get("website_uri", "")
            conf = result.get("match_confidence", 0)
            log.info(
                f"  [{i+len(checkpoint)-len(todo)}/{len(targets)}] "
                f"{'✓' if conf >= MATCH_THRESH else '△'} "
                f"{title[:25]:25} conf={conf:.2f} url={site[:50] if site else '(なし)'}"
            )
        else:
            log.info(
                f"  [{i+len(checkpoint)-len(todo)}/{len(targets)}] "
                f"✗ {title[:25]:25} → {result['status']}"
            )

        if i % BATCH_SAVE == 0:
            save_checkpoint(checkpoint)

    save_checkpoint(checkpoint)

    # --- 結果集計 ---
    records = []
    for title, res in checkpoint.items():
        website = res.get("website_uri", "")
        conf    = res.get("match_confidence") or 0
        url_ok  = False

        # 名前マッチが低すぎる場合はスキップ
        if res["status"] == "OK" and conf >= MATCH_THRESH and website:
            url_ok = check_url_ok(website)

        records.append({
            "studio_title":      title,
            "api_status":        res.get("status"),
            "google_name":       res.get("google_name", ""),
            "match_confidence":  conf,
            "website_uri":       website,
            "url_accessible":    url_ok,
            "google_address":    res.get("google_address", ""),
            "query_used":        res.get("query", ""),
        })

    df_results = pd.DataFrame(records)
    df_results.to_csv(RESULTS_CSV, index=False, encoding="utf-8-sig")
    log.info(f"結果CSV保存: {RESULTS_CSV}")

    # 採用するURL（アクセス可能なもの）
    adopt = df_results[df_results["url_accessible"] == True]
    log.info(f"\n採用URL: {len(adopt)} 件 / 対象: {len(df_results)} 件")

    # --- マスタCSV 更新 ---
    master = pd.read_csv(MASTER_CSV)  # 最新を再読み込み
    updated = 0
    for _, r in adopt.iterrows():
        mask = master["タイトル"] == r["studio_title"]
        if mask.any():
            master.loc[mask, "Webサイト"] = r["website_uri"]
            updated += 1

    master.to_csv(MASTER_CSV, index=False, encoding="utf-8-sig")
    log.info(f"マスタCSV更新: {updated} 件")

    # --- サマリ ---
    print("\n" + "=" * 70)
    print(f"■ 補完結果サマリー（対象 {len(df_results)} 件）")
    print("=" * 70)
    print(f"  Places API 取得成功 : {(df_results['api_status']=='OK').sum()} 件")
    print(f"  websiteUri あり     : {(df_results['website_uri'] != '').sum()} 件")
    print(f"  URL 疎通OK          : {len(adopt)} 件")
    print(f"  マスタCSV 更新済み  : {updated} 件")

    # 名前信頼度が低いもの
    low = df_results[(df_results["api_status"] == "OK") & (df_results["match_confidence"] < MATCH_THRESH)]
    if not low.empty:
        print(f"\n  ⚠ 名前類似度が低いため除外: {len(low)} 件（手動確認推奨）")
        for _, r in low.iterrows():
            print(f"    [{r['match_confidence']:.2f}] {r['studio_title'][:30]} → {r['google_name']}")

    print("=" * 70)
    log.info(f"\n結果CSV: {RESULTS_CSV}")


if __name__ == "__main__":
    main()
