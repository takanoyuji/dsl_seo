#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Google Places API バッチ取得スクリプト
places_api_targets.csv → places_api_results.csv

使い方:
  export GOOGLE_PLACES_API_KEY="AIza..."
  python3 fetch_places_api.py

途中で止めても places_api_checkpoint.json から再開できる。
"""

import os
import re
import time
import json
import logging
import requests
import pandas as pd
from pathlib import Path
from difflib import SequenceMatcher
from tqdm import tqdm

# ============================================================
# .env 読み込み (python-dotenv がなくても動く簡易版)
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
API_KEY          = os.environ.get("GOOGLE_PLACES_API_KEY", "")
INPUT_CSV        = "data/pipeline/places_api_targets.csv"
OUTPUT_CSV       = "data/pipeline/places_api_results.csv"
CHECKPOINT_FILE  = "checkpoints/places_api_checkpoint.json"

# --- API エンドポイント ---
# "new"  → Places API (v1) Text Search  ← 推奨
# "legacy" → Maps Platform Text Search   ← 旧API (2025年廃止予定)
API_MODE = "new"

# --- レート制御 ---
QPS_LIMIT    = 5    # 1秒あたり最大リクエスト数
RETRY_MAX    = 3    # エラー時リトライ上限
RETRY_WAIT   = 2.0  # リトライ間隔(秒)
BATCH_SAVE   = 10   # 何件ごとにチェックポイント保存するか

# --- マッチング ---
# API返却名とスタジオ名の類似度がこれ未満なら "low_confidence" とマーク
MATCH_CONFIDENCE_THRESHOLD = 0.5

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
# 文字列正規化・類似度
# ============================================================
def normalize(s: str) -> str:
    """スペース・記号・全角を正規化して比較しやすくする"""
    if not s:
        return ""
    s = s.lower()
    # 全角英数→半角
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


# ============================================================
# クエリ生成
# ============================================================
def build_query(title: str, address: str) -> str:
    """
    住所から都道府県・郵便番号を落として市区町村+スタジオ名で検索する。
    長すぎるクエリは精度を下げることがある。
    """
    # 〒xxx-xxxx を除去
    addr_clean = re.sub(r"〒\d{3}-\d{4}\s*", "", str(address) if pd.notna(address) else "")
    # "東京都" を残して番地系を省略（丁目以降を切り落とす）
    addr_short = re.sub(r"(\d+丁目.*)$", "", addr_clean).strip()
    if not addr_short:
        addr_short = "東京"
    return f"{title} {addr_short}"


# ============================================================
# API 呼び出し (New Places API v1)
# ============================================================
NEW_API_ENDPOINT = "https://places.googleapis.com/v1/places:searchText"
NEW_API_FIELDS   = "places.id,places.displayName,places.rating,places.userRatingCount,places.formattedAddress"


def call_new_api(query: str, api_key: str) -> dict:
    """
    Places API (v1) Text Search を呼び出す。
    戻り値: {"place_id":..., "google_name":..., "rating":..., "review_count":..., "google_address":...}
    見つからない場合: {"status": "NOT_FOUND"}
    エラー時: {"status": "ERROR", "error": ...}
    """
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": NEW_API_FIELDS,
    }
    body = {
        "textQuery": query,
        "languageCode": "ja",
        "regionCode": "JP",
        "maxResultCount": 1,
    }

    try:
        resp = requests.post(NEW_API_ENDPOINT, headers=headers, json=body, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        return {"status": "ERROR", "error": str(e)}

    places = data.get("places", [])
    if not places:
        return {"status": "NOT_FOUND"}

    p = places[0]
    return {
        "place_id":      p.get("id", ""),
        "google_name":   p.get("displayName", {}).get("text", ""),
        "rating":        p.get("rating"),
        "review_count":  p.get("userRatingCount"),
        "google_address": p.get("formattedAddress", ""),
        "status":        "OK",
    }


# ============================================================
# API 呼び出し (Legacy Places API)
# ============================================================
LEGACY_API_ENDPOINT = "https://maps.googleapis.com/maps/api/place/textsearch/json"


def call_legacy_api(query: str, api_key: str) -> dict:
    params = {
        "query":    query,
        "language": "ja",
        "region":   "jp",
        "key":      api_key,
    }
    try:
        resp = requests.get(LEGACY_API_ENDPOINT, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        return {"status": "ERROR", "error": str(e)}

    if data.get("status") == "ZERO_RESULTS":
        return {"status": "NOT_FOUND"}
    if data.get("status") != "OK":
        return {"status": "ERROR", "error": data.get("status")}

    results = data.get("results", [])
    if not results:
        return {"status": "NOT_FOUND"}

    r = results[0]
    return {
        "place_id":      r.get("place_id", ""),
        "google_name":   r.get("name", ""),
        "rating":        r.get("rating"),
        "review_count":  r.get("user_ratings_total"),
        "google_address": r.get("formatted_address", ""),
        "status":        "OK",
    }


# ============================================================
# リトライ付き呼び出しディスパッチャ
# ============================================================
def fetch_place(title: str, address: str, api_key: str, mode: str) -> dict:
    query = build_query(title, address)
    call_fn = call_new_api if mode == "new" else call_legacy_api

    for attempt in range(1, RETRY_MAX + 1):
        result = call_fn(query, api_key)

        if result["status"] == "OK":
            sim = name_similarity(title, result.get("google_name", ""))
            result["match_confidence"] = round(sim, 3)
            result["query_used"] = query
            return result

        if result["status"] == "NOT_FOUND":
            result["match_confidence"] = None
            result["query_used"] = query
            return result

        # ERROR → リトライ
        log.warning(
            f"[attempt {attempt}/{RETRY_MAX}] エラー: {result.get('error')} "
            f"({title[:20]})"
        )
        if attempt < RETRY_MAX:
            time.sleep(RETRY_WAIT * attempt)

    result["match_confidence"] = None
    result["query_used"] = query
    return result


# ============================================================
# チェックポイント I/O
# ============================================================
def load_checkpoint(path: str) -> dict:
    p = Path(path)
    if p.exists():
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        log.info(f"チェックポイント読み込み: {len(data)} 件処理済み")
        return data
    return {}


def save_checkpoint(data: dict, path: str):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ============================================================
# メイン
# ============================================================
def main():
    if not API_KEY:
        log.error(
            "API キーが設定されていません。\n"
            "  export GOOGLE_PLACES_API_KEY='AIza...'\n"
            "または fetch_places_api.py の API_KEY 変数に直接設定してください。"
        )
        return

    # --- 入力読み込み ---
    log.info(f"入力: {INPUT_CSV}")
    df = pd.read_csv(INPUT_CSV)
    log.info(f"  対象スタジオ: {len(df)} 件")

    # --- チェックポイント読み込み ---
    checkpoint = load_checkpoint(CHECKPOINT_FILE)

    # --- 未処理スタジオを抽出 ---
    todo = df[~df["studio_title"].isin(checkpoint)].copy()
    log.info(f"  未処理: {len(todo)} 件 / 処理済み: {len(checkpoint)} 件")

    if todo.empty:
        log.info("全件処理済み。チェックポイントから結果を生成します。")
    else:
        # --- QPS 制御用タイマー ---
        interval = 1.0 / QPS_LIMIT
        last_call = 0.0

        for i, (_, row) in enumerate(
            tqdm(todo.iterrows(), total=len(todo), desc="Places API"),
            start=1,
        ):
            # レート制限
            elapsed = time.time() - last_call
            wait = interval - elapsed
            if wait > 0:
                time.sleep(wait)
            last_call = time.time()

            result = fetch_place(
                title=row["studio_title"],
                address=row.get("address", ""),
                api_key=API_KEY,
                mode=API_MODE,
            )
            checkpoint[row["studio_title"]] = result

            if result["status"] == "OK":
                conf = result.get("match_confidence", 0)
                conf_label = "✓" if conf >= MATCH_CONFIDENCE_THRESHOLD else "△"
                log.debug(
                    f"{conf_label} {row['studio_title'][:25]:25} → "
                    f"{result.get('google_name','')[:25]:25} "
                    f"★{result.get('rating','?')} "
                    f"({result.get('review_count','?')}件) "
                    f"conf={conf:.2f}"
                )
            else:
                log.debug(f"✗ {row['studio_title'][:25]} → {result['status']}")

            # 定期保存
            if i % BATCH_SAVE == 0:
                save_checkpoint(checkpoint, CHECKPOINT_FILE)

        # 最終保存
        save_checkpoint(checkpoint, CHECKPOINT_FILE)
        log.info(f"チェックポイント保存完了: {CHECKPOINT_FILE}")

    # --- 結果をDataFrameに変換 ---
    result_rows = []
    for title, res in checkpoint.items():
        result_rows.append({
            "studio_title":    title,
            "place_id":        res.get("place_id", ""),
            "google_name":     res.get("google_name", ""),
            "rating":          res.get("rating"),
            "review_count":    res.get("review_count"),
            "google_address":  res.get("google_address", ""),
            "match_confidence": res.get("match_confidence"),
            "api_status":      res.get("status", "UNKNOWN"),
            "query_used":      res.get("query_used", ""),
        })

    df_results = pd.DataFrame(result_rows)
    df_merged  = df.merge(df_results, on="studio_title", how="left")

    # --- 統計ログ ---
    total   = len(df_merged)
    found   = (df_merged["api_status"] == "OK").sum()
    not_found = (df_merged["api_status"] == "NOT_FOUND").sum()
    error   = total - found - not_found
    low_conf = (
        (df_merged["api_status"] == "OK") &
        (df_merged["match_confidence"] < MATCH_CONFIDENCE_THRESHOLD)
    ).sum()

    log.info(
        f"結果サマリー: 全{total}件  "
        f"取得OK={found}  NOT_FOUND={not_found}  エラー={error}  "
        f"低信頼度={low_conf}"
    )

    if found > 0:
        log.info(
            f"  平均評価: {df_merged[df_merged['api_status']=='OK']['rating'].mean():.2f}  "
            f"平均口コミ数: {df_merged[df_merged['api_status']=='OK']['review_count'].mean():.0f}"
        )

    # --- 出力 ---
    df_merged.to_csv(OUTPUT_CSV, index=False, encoding="utf-8-sig")
    log.info(f"出力完了: {OUTPUT_CSV} ({len(df_merged)} 件)")

    # --- 低信頼度スタジオを表示 ---
    low_conf_df = df_merged[
        (df_merged["api_status"] == "OK") &
        (df_merged["match_confidence"] < MATCH_CONFIDENCE_THRESHOLD)
    ][["studio_title", "google_name", "match_confidence", "query_used"]]

    if not low_conf_df.empty:
        print("\n" + "="*70)
        print("■ 低信頼度マッチ (手動確認推奨)")
        print("="*70)
        for _, r in low_conf_df.iterrows():
            print(
                f"  [{r['match_confidence']:.2f}] "
                f"{r['studio_title'][:30]:30} → {r['google_name']}"
            )
        print("="*70)


if __name__ == "__main__":
    main()
