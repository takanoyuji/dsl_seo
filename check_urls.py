#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
URLアクセスチェックスクリプト
studio_with_tags_dslurl_address_filled.csv の Webサイト・dslurl を全件チェックし
アクセス可否・ステータスコード・最終URLをCSVに出力する。

使い方:
  python3 check_urls.py               # 全件チェック（~20〜40分）
  python3 check_urls.py --limit 50    # 先頭50件のみ（動作確認用）
  python3 check_urls.py --resume      # 途中再開（既チェック済みをスキップ）

出力:
  data/pipeline/url_check_results.csv
"""

import time
import random
import argparse
import logging
import urllib.parse
from pathlib import Path

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ============================================================
# 設定
# ============================================================
STUDIO_CSV  = "data/master/studio_with_tags_dslurl_address_filled.csv"
OUTPUT_CSV  = "data/pipeline/url_check_results.csv"

TIMEOUT       = 12        # 接続タイムアウト（秒）
DELAY_MIN     = 0.5       # リクエスト間隔 下限（秒）
DELAY_MAX     = 1.5       # リクエスト間隔 上限（秒）
MAX_WORKERS   = 1         # 並列数（サーバー負荷を避けるため1推奨）

# ステータスコード分類
STATUS_OK      = {200, 301, 302, 303, 307, 308}   # リダイレクト含め正常
STATUS_BLOCKED = {403, 429}                         # アクセス拒否（URLは存在する可能性あり）
STATUS_NOTFOUND = {404, 410}                        # ページ消滅
STATUS_ERROR    = {500, 502, 503, 504}              # サーバーエラー

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja,en-US;q=0.7,en;q=0.3",
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
# HTTP チェック本体
# ============================================================
def make_session() -> requests.Session:
    """リトライ付き Session を生成"""
    session = requests.Session()
    retry = Retry(
        total=2,
        backoff_factor=1,
        status_forcelist=[500, 502, 503, 504],
        allowed_methods=["HEAD", "GET"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update(HEADERS)
    return session


def classify_status(status_code: int) -> str:
    if status_code in STATUS_OK:
        return "OK"
    if status_code in STATUS_BLOCKED:
        return "BLOCKED"
    if status_code in STATUS_NOTFOUND:
        return "NOT_FOUND"
    if status_code in STATUS_ERROR:
        return "SERVER_ERROR"
    return "OTHER"


def check_url(session: requests.Session, url: str) -> dict:
    """
    1件のURLをチェックして結果 dict を返す。
    HEAD → 失敗なら GET にフォールバック。
    """
    result = {
        "url":          url,
        "final_url":    "",
        "status_code":  None,
        "status_label": "",
        "redirected":   False,
        "error":        "",
    }

    try:
        # まず HEAD で確認（帯域節約）
        resp = session.head(url, timeout=TIMEOUT, allow_redirects=True)

        # HEAD を拒否するサーバーは GET にフォールバック
        if resp.status_code in (405, 400):
            resp = session.get(url, timeout=TIMEOUT, allow_redirects=True, stream=True)
            resp.close()

        result["status_code"]  = resp.status_code
        result["final_url"]    = resp.url
        result["redirected"]   = (resp.url.rstrip("/").lower() != url.rstrip("/").lower())
        result["status_label"] = classify_status(resp.status_code)

    except requests.exceptions.SSLError as e:
        result["error"] = f"SSL_ERROR: {str(e)[:80]}"
        result["status_label"] = "SSL_ERROR"
    except requests.exceptions.ConnectionError as e:
        result["error"] = f"CONNECTION_ERROR: {str(e)[:80]}"
        result["status_label"] = "CONNECTION_ERROR"
    except requests.exceptions.Timeout:
        result["error"] = "TIMEOUT"
        result["status_label"] = "TIMEOUT"
    except Exception as e:
        result["error"] = f"ERROR: {str(e)[:80]}"
        result["status_label"] = "ERROR"

    return result


# ============================================================
# メイン
# ============================================================
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit",  type=int, default=0,     help="チェック件数上限（0=全件）")
    parser.add_argument("--resume", action="store_true",      help="既チェック済みをスキップして再開")
    parser.add_argument("--type",   choices=["web", "dsl", "all"], default="all",
                        help="チェック対象 (web=Webサイトのみ / dsl=dslurlのみ / all=両方)")
    args = parser.parse_args()

    # ── データ読み込み ──────────────────────────────────────
    df = pd.read_csv(STUDIO_CSV)
    rows = []

    if args.type in ("web", "all"):
        web_df = df[["タイトル", "Webサイト"]].rename(columns={"Webサイト": "url"})
        web_df = web_df[web_df["url"].notna() & web_df["url"].astype(str).str.startswith("http")]
        web_df["url_type"] = "Webサイト"
        rows.append(web_df[["タイトル", "url_type", "url"]])

    if args.type in ("dsl", "all"):
        dsl_df = df[["タイトル", "dslurl"]].rename(columns={"dslurl": "url"})
        dsl_df = dsl_df[dsl_df["url"].notna() & dsl_df["url"].astype(str).str.startswith("http")]
        dsl_df["url_type"] = "dslurl"
        rows.append(dsl_df[["タイトル", "url_type", "url"]])

    targets = pd.concat(rows, ignore_index=True)
    targets["url"] = targets["url"].astype(str).str.strip()
    targets = targets.drop_duplicates("url")   # 同一URLの重複除去

    # ── 再開モード: 既チェック済みをスキップ ────────────────
    already_checked: set = set()
    existing_results: list = []
    out_path = Path(OUTPUT_CSV)

    if args.resume and out_path.exists():
        df_exist = pd.read_csv(out_path)
        already_checked = set(df_exist["url"].astype(str))
        existing_results = df_exist.to_dict("records")
        log.info(f"再開モード: {len(already_checked)}件はスキップ")

    targets = targets[~targets["url"].isin(already_checked)]
    if args.limit:
        targets = targets.head(args.limit)

    total = len(targets)
    log.info(f"チェック対象: {total}件")

    # ── HTTP チェックループ ─────────────────────────────────
    session  = make_session()
    results  = list(existing_results)
    done     = 0
    ng_count = 0

    for _, row in targets.iterrows():
        studio_name = str(row["タイトル"])
        url_type    = str(row["url_type"])
        url         = str(row["url"])

        chk = check_url(session, url)
        chk["studio_name"] = studio_name
        chk["url_type"]    = url_type
        results.append(chk)
        done += 1

        label = chk["status_label"]
        is_ng = label not in ("OK", "BLOCKED")
        if is_ng:
            ng_count += 1

        # ログ: 問題あり or 100件ごと
        if is_ng or done % 100 == 0:
            code  = chk["status_code"] or chk["error"][:20]
            redir = f" → {chk['final_url'][:50]}" if chk["redirected"] else ""
            log.info(f"  [{done}/{total}] [{label}:{code}] {studio_name[:25]}{redir}")

        # 中間保存（100件ごと）
        if done % 100 == 0:
            _save(results, out_path)

        time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))

    # ── 最終保存 ────────────────────────────────────────────
    _save(results, out_path)

    # ── サマリ出力 ───────────────────────────────────────────
    df_out = pd.DataFrame(results)
    summary = df_out["status_label"].value_counts()
    log.info("\n=== チェック完了 ===")
    log.info(f"総チェック件数: {len(df_out)}")
    log.info(summary.to_string())

    ng = df_out[~df_out["status_label"].isin(["OK", "BLOCKED"])]
    if len(ng):
        log.info(f"\n問題あり: {len(ng)}件")
        for _, r in ng.iterrows():
            log.info(f"  [{r['status_label']}] {r['studio_name'][:30]} | {r['url'][:60]}")

    log.info(f"\n結果CSV: {out_path}")


def _save(results: list, out_path: Path):
    cols = ["studio_name", "url_type", "url", "status_code",
            "status_label", "final_url", "redirected", "error"]
    pd.DataFrame(results)[cols].to_csv(out_path, index=False, encoding="utf-8-sig")


if __name__ == "__main__":
    main()
