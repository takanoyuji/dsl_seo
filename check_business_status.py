#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
営業ステータス確認スクリプト (Google Places API v1)

使い方:
  python3 check_business_status.py --article "新宿駅のレンタルダンススタジオおすすめ"
  python3 check_business_status.py --csv data/pipeline/shinjuku_station_selected_studios.csv
  python3 check_business_status.py --article ... --force-refresh

キャッシュ: data/cache/google_place_business_status.csv (90日有効)
除外出力  : data/pipeline/business_status_excluded_studios.csv
"""

import argparse
import datetime
import logging
import os
import re
import time
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path

import pandas as pd
import requests

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
API_KEY           = os.environ.get("GOOGLE_PLACES_API_KEY", "")
CACHE_DIR         = Path("data/cache")
CACHE_CSV         = CACHE_DIR / "google_place_business_status.csv"
EXCLUDED_CSV      = Path("data/pipeline/business_status_excluded_studios.csv")
CACHE_EXPIRE_DAYS = 90
QPS_LIMIT         = 3    # 1秒あたり最大リクエスト数
RETRY_MAX         = 3
RETRY_WAIT        = 2.0

# 記事名 → 入力CSVのマッピング (駅を追加するたびにここに追記)
STATION_CSV_MAP: dict[str, str] = {
    "新宿駅のレンタルダンススタジオおすすめ": "data/pipeline/shinjuku_station_selected_studios.csv",
}

NEW_API_ENDPOINT = "https://places.googleapis.com/v1/places:searchText"
NEW_API_FIELDS   = "places.id,places.displayName,places.formattedAddress,places.businessStatus"

CACHE_COLUMNS = [
    "cache_key", "studio_title", "address", "website", "phone",
    "place_id", "google_display_name", "business_status",
    "checked_at", "source", "api_status", "error_message", "match_confidence",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ============================================================
# キャッシュキー生成
# ============================================================
def _norm(s: str) -> str:
    """NFKC正規化 → 小文字 → 空白・記号除去"""
    s = unicodedata.normalize("NFKC", s or "")
    s = s.lower()
    s = re.sub(r"[\s\-_・・　\(\)（）【】「」『』〒]", "", s)
    return s


def make_cache_key(
    place_id: str = "",
    title: str = "",
    address: str = "",
    website: str = "",
) -> str:
    """優先順位: place_id > title+address > title+website > title のみ"""
    if place_id:
        return f"pid:{place_id}"
    if title and address:
        return f"ta:{_norm(title)}:{_norm(address)}"
    if title and website:
        return f"tw:{_norm(title)}:{_norm(website)}"
    return f"t:{_norm(title)}"


# ============================================================
# キャッシュ操作
# ============================================================
def load_cache() -> pd.DataFrame:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if CACHE_CSV.exists():
        df = pd.read_csv(CACHE_CSV, dtype=str)
        for col in CACHE_COLUMNS:
            if col not in df.columns:
                df[col] = ""
        return df
    return pd.DataFrame(columns=CACHE_COLUMNS)


def save_cache(df: pd.DataFrame) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    df.to_csv(CACHE_CSV, index=False, encoding="utf-8")


def is_cache_valid(row: pd.Series, force_refresh: bool) -> bool:
    if force_refresh:
        return False
    checked_at = str(row.get("checked_at", ""))
    if not checked_at or checked_at in ("nan", ""):
        return False
    try:
        dt  = datetime.datetime.fromisoformat(checked_at)
        age = datetime.datetime.now() - dt
        return age.days < CACHE_EXPIRE_DAYS
    except (ValueError, TypeError):
        return False


def upsert_cache(cache_df: pd.DataFrame, record: dict) -> pd.DataFrame:
    key  = record["cache_key"]
    mask = cache_df["cache_key"] == key
    if mask.any():
        for col in CACHE_COLUMNS:
            cache_df.loc[mask, col] = str(record.get(col, ""))
    else:
        new_row  = pd.DataFrame([{c: str(record.get(c, "")) for c in CACHE_COLUMNS}])
        cache_df = pd.concat([cache_df, new_row], ignore_index=True)
    return cache_df


# ============================================================
# 文字列ユーティリティ
# ============================================================
def name_similarity(a: str, b: str) -> float:
    def _n(s: str) -> str:
        s = s.lower()
        return re.sub(r"[\s\-_・　]+", "", s)
    return SequenceMatcher(None, _n(a), _n(b)).ratio()


def build_query(title: str, address: str) -> str:
    """スタジオ名 + 市区町村部分 (番地は不要)"""
    addr_clean = re.sub(r"〒\d{3}-\d{4}\s*", "", address or "")
    addr_short = re.sub(r"(\d+丁目.*)$", "", addr_clean).strip()
    if not addr_short:
        addr_short = "東京"
    return f"{title} {addr_short}"


# ============================================================
# Places API v1 呼び出し
# ============================================================
def call_places_api(query: str) -> dict:
    """
    Returns:
      {"api_status": "OK", "place_id":..., "google_display_name":...,
       "google_address":..., "business_status":...}
      {"api_status": "NOT_FOUND"}
      {"api_status": "ERROR", "error_message":...}
    """
    headers = {
        "Content-Type":    "application/json",
        "X-Goog-Api-Key":  API_KEY,
        "X-Goog-FieldMask": NEW_API_FIELDS,
    }
    body = {
        "textQuery":      query,
        "languageCode":   "ja",
        "regionCode":     "JP",
        "maxResultCount": 1,
    }
    for attempt in range(1, RETRY_MAX + 1):
        try:
            resp = requests.post(NEW_API_ENDPOINT, headers=headers, json=body, timeout=10)
            resp.raise_for_status()
            data   = resp.json()
            places = data.get("places", [])
            if not places:
                return {"api_status": "NOT_FOUND"}
            p = places[0]
            return {
                "api_status":          "OK",
                "place_id":            p.get("id", ""),
                "google_display_name": p.get("displayName", {}).get("text", ""),
                "google_address":      p.get("formattedAddress", ""),
                "business_status":     p.get("businessStatus", "UNKNOWN"),
            }
        except requests.RequestException as e:
            if attempt < RETRY_MAX:
                log.warning(f"  リトライ {attempt}/{RETRY_MAX}: {e}")
                time.sleep(RETRY_WAIT)
            else:
                return {"api_status": "ERROR", "error_message": str(e)}
    return {"api_status": "ERROR", "error_message": "max retries exceeded"}


# ============================================================
# スタジオ1件チェック
# ============================================================
def check_one(
    row_data: dict,
    cache_df: pd.DataFrame,
    force_refresh: bool,
) -> dict:
    title   = str(row_data.get("studio_title", ""))
    address = str(row_data.get("address", ""))
    website = str(row_data.get("website", ""))
    phone   = str(row_data.get("phone", ""))

    key   = make_cache_key(title=title, address=address, website=website)
    match = cache_df[cache_df["cache_key"] == key]

    if not match.empty and is_cache_valid(match.iloc[0], force_refresh):
        cached = match.iloc[0]
        log.info(f"[CACHE HIT] {title}")
        return {
            "cache_key":           key,
            "studio_title":        title,
            "address":             address,
            "website":             website,
            "phone":               phone,
            "place_id":            str(cached.get("place_id", "")),
            "google_display_name": str(cached.get("google_display_name", "")),
            "business_status":     str(cached.get("business_status", "UNKNOWN")),
            "checked_at":          str(cached.get("checked_at", "")),
            "source":              "cache",
            "api_status":          str(cached.get("api_status", "OK")),
            "error_message":       str(cached.get("error_message", "")),
            "match_confidence":    cached.get("match_confidence", ""),
        }

    # API 呼び出し
    query  = build_query(title, address)
    log.info(f"[API] {title} → {query!r}")
    result = call_places_api(query)
    time.sleep(1.0 / QPS_LIMIT)

    confidence = ""
    if result.get("api_status") == "OK":
        confidence = round(name_similarity(title, result.get("google_display_name", "")), 3)

    now = datetime.datetime.now().isoformat(timespec="seconds")
    return {
        "cache_key":           key,
        "studio_title":        title,
        "address":             address,
        "website":             website,
        "phone":               phone,
        "place_id":            result.get("place_id", ""),
        "google_display_name": result.get("google_display_name", ""),
        "business_status":     result.get("business_status", "UNKNOWN"),
        "checked_at":          now,
        "source":              "api",
        "api_status":          result.get("api_status", "ERROR"),
        "error_message":       result.get("error_message", ""),
        "match_confidence":    confidence,
    }


# ============================================================
# 除外判定
# ============================================================
def is_excluded(business_status: str) -> bool:
    return business_status in ("CLOSED_PERMANENTLY", "CLOSED_TEMPORARILY")


# ============================================================
# main
# ============================================================
def main() -> None:
    parser = argparse.ArgumentParser(description="Google Places 営業ステータス確認")
    parser.add_argument(
        "--article",
        default="新宿駅のレンタルダンススタジオおすすめ",
        help="記事名 (STATION_CSV_MAP のキー)",
    )
    parser.add_argument(
        "--csv",
        default="",
        help="入力CSVを直接指定 (--article より優先)",
    )
    parser.add_argument(
        "--force-refresh",
        action="store_true",
        help="90日キャッシュを無視してAPIを再取得",
    )
    args = parser.parse_args()

    # 入力CSV 決定
    if args.csv:
        input_csv = Path(args.csv)
    else:
        csv_path = STATION_CSV_MAP.get(args.article)
        if not csv_path:
            log.error(f"記事名が未登録: {args.article!r}")
            log.error(f"登録済み: {list(STATION_CSV_MAP.keys())}")
            return
        input_csv = Path(csv_path)

    if not input_csv.exists():
        log.error(f"入力CSVが見つかりません: {input_csv}")
        return
    if not API_KEY:
        log.error("GOOGLE_PLACES_API_KEY が設定されていません (.env または環境変数)")
        return

    df_studios = pd.read_csv(input_csv)
    cache_df   = load_cache()

    results:  list[dict] = []
    excluded: list[dict] = []

    log.info(f"チェック対象: {len(df_studios)} 件 (article={args.article!r})")
    if args.force_refresh:
        log.info("[--force-refresh] キャッシュを無視して全件 API 取得します")

    for _, row in df_studios.iterrows():
        row_data = {
            "studio_title": str(row.get("studio_title", "")),
            "address":      str(row.get("address", "")),
            "website":      str(row.get("website", "") or row.get("official_url", "")),
            "phone":        "",
        }

        record   = check_one(row_data, cache_df, args.force_refresh)
        cache_df = upsert_cache(cache_df, record)
        results.append(record)

        status = record["business_status"]
        if is_excluded(status):
            excluded.append({
                "studio_title":    record["studio_title"],
                "business_status": status,
                "address":         record["address"],
                "checked_at":      record["checked_at"],
            })
            log.warning(f"[EXCLUDED] {record['studio_title']} → {status}")
        elif status == "UNKNOWN" or record["api_status"] != "OK":
            log.info(
                f"[UNKNOWN] {record['studio_title']} "
                f"(api_status={record['api_status']})"
            )
        else:
            log.info(
                f"[OK] {record['studio_title']} → {status} "
                f"(confidence={record['match_confidence']})"
            )

    # キャッシュ保存
    save_cache(cache_df)
    log.info(f"キャッシュ保存: {CACHE_CSV} ({len(cache_df)} 件)")

    # 除外CSV 出力 (空でもファイルを作る)
    excl_df = pd.DataFrame(
        excluded if excluded
        else [],
        columns=["studio_title", "business_status", "address", "checked_at"],
    )
    EXCLUDED_CSV.parent.mkdir(parents=True, exist_ok=True)
    excl_df.to_csv(EXCLUDED_CSV, index=False, encoding="utf-8")
    log.info(f"除外CSV: {EXCLUDED_CSV} ({len(excl_df)} 件)")

    # ============================================================
    # レポート
    # ============================================================
    total     = len(results)
    ok_count  = sum(1 for r in results if r["api_status"] == "OK")
    cache_hit = sum(1 for r in results if r["source"] == "cache")
    unknown   = sum(1 for r in results if r["business_status"] == "UNKNOWN")
    excl_cnt  = len(excluded)

    print("\n[営業ステータスチェック レポート]")
    print(f"入力CSV          : {input_csv}")
    print(f"チェック件数     : {total}")
    print(f"API取得成功      : {ok_count}")
    print(f"キャッシュヒット : {cache_hit}")
    print(f"UNKNOWN          : {unknown}")
    print(f"除外対象         : {excl_cnt} 件")
    if excluded:
        for e in excluded:
            print(f"  - {e['studio_title']} ({e['business_status']})")
    else:
        print("  (除外なし)")
    print(f"キャッシュ保存先 : {CACHE_CSV}")
    print(f"除外CSV保存先    : {EXCLUDED_CSV}")

    if excl_cnt > 0:
        remaining = total - excl_cnt
        print(f"\n⚠ {excl_cnt} 件が除外されました。掲載可能残数: {remaining}")
        print("  step4_generate_shinjuku.py を再実行してください。")
    else:
        print("\n✓ 閉業スタジオなし。全件掲載可能です。")


if __name__ == "__main__":
    main()
