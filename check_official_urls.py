#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
公式サイトURL疎通・正当性チェックスクリプト

使い方:
  python3 check_official_urls.py --article "新宿駅のレンタルダンススタジオおすすめ"
  python3 check_official_urls.py --csv data/pipeline/shinjuku_station_selected_studios.csv
  python3 check_official_urls.py --article ... --force-refresh

キャッシュ: data/cache/official_url_check_cache.csv (30日有効)
レポート  : data/pipeline/official_url_check_report.csv
警告サマリ: data/pipeline/url_warnings_summary.csv

alert_level:
  OK    … 200 OK・リダイレクトなし or 同一ドメイン内リダイレクト
  WARN  … SSL エラー / タイムアウト / ドメイン変更リダイレクト / キーワード未検出
  ERROR … 4xx/5xx / 接続不可 / SNS/外部サービスへリダイレクト / ドメイン駐車ページ
"""

import argparse
import datetime
import logging
import os
import re
import time
import unicodedata
from pathlib import Path
from urllib.parse import urlparse

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
CACHE_DIR         = Path("data/cache")
CACHE_CSV         = CACHE_DIR / "official_url_check_cache.csv"
REPORT_CSV        = Path("data/pipeline/official_url_check_report.csv")
WARNINGS_CSV      = Path("data/pipeline/url_warnings_summary.csv")
CACHE_EXPIRE_DAYS = 30
REQUEST_TIMEOUT   = 8
RETRY_MAX         = 2   # 初回 + 1 リトライ

# 記事名 → 入力CSVのマッピング (駅を追加するたびにここに追記)
STATION_CSV_MAP: dict[str, str] = {
    "新宿駅のレンタルダンススタジオおすすめ": "data/pipeline/shinjuku_station_selected_studios.csv",
}

# 公式サイトとして不適切なリダイレクト先ドメイン → ERROR
SUSPICIOUS_DOMAINS = {
    "instagram.com",
    "www.instagram.com",
    "facebook.com",
    "www.facebook.com",
    "twitter.com",
    "www.twitter.com",
    "x.com",
    "www.x.com",
    "line.me",
    "lin.ee",
    "lit.link",
    "ameblo.jp",
    "ameba.jp",
    "tiktok.com",
    "www.tiktok.com",
    "youtube.com",
    "www.youtube.com",
    "parking.godaddy.com",
    "sedo.com",
    "sedoparking.com",
}

# ページ内にあればドメイン駐車と判定 → ERROR
PARKING_KEYWORDS = [
    "this domain is for sale",
    "domain for sale",
    "domain parking",
    "buy this domain",
    "parked domain",
    "お名前.com",
    "ムームードメイン",
]

CACHE_COLUMNS = [
    "cache_key", "url", "studio_title",
    "http_status", "final_url", "redirect_count", "is_redirect",
    "content_keyword_found", "alert_level", "alert_reason",
    "checked_at", "error_message",
]

REPORT_COLUMNS = [
    "studio_title", "url", "alert_level", "alert_reason",
    "http_status", "final_url", "is_redirect", "redirect_count",
    "content_keyword_found", "checked_at", "error_message",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept":          "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "ja,en;q=0.5",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ============================================================
# ユーティリティ
# ============================================================
def normalize_url(url: str) -> str:
    """キャッシュキー用URL正規化 (末尾スラッシュ除去・小文字化)"""
    return url.strip().rstrip("/").lower()


def extract_domain(url: str) -> str:
    try:
        return urlparse(url).netloc.lower().lstrip("www.")
    except Exception:
        return ""


def is_suspicious_domain(domain: str) -> bool:
    bare = domain.lstrip("www.")
    return domain in SUSPICIOUS_DOMAINS or bare in SUSPICIOUS_DOMAINS


def keywords_from_title(title: str) -> list[str]:
    """スタジオ名から検索キーワードを抽出 (2文字以上のトークン)"""
    t = unicodedata.normalize("NFKC", title)
    parts = re.split(r"[\s\-_・　「」【】（）()\[\]]+", t)
    return [p for p in parts if len(p) >= 2]


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
# HTTP リクエスト
# ============================================================
def _do_request(url: str, method: str) -> dict:
    """
    Returns:
      {"status": 200, "final_url": "...", "redirect_count": 0, "body": ""}
      {"status": -N, "error": "...", "final_url": url}
    """
    try:
        resp = requests.request(
            method, url,
            headers=HEADERS,
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True,
        )
        body = ""
        if method == "GET":
            # content_type が html 系のときのみテキスト取得
            ct = resp.headers.get("Content-Type", "")
            if "html" in ct or "text" in ct:
                body = resp.text[:50_000]
        return {
            "status":         resp.status_code,
            "final_url":      resp.url,
            "redirect_count": len(resp.history),
            "body":           body,
        }
    except requests.exceptions.SSLError as e:
        return {"status": -2, "error": str(e), "final_url": url}
    except requests.exceptions.ConnectionError as e:
        return {"status": -1, "error": str(e), "final_url": url}
    except requests.exceptions.Timeout:
        return {"status": -3, "error": "タイムアウト", "final_url": url}
    except requests.exceptions.TooManyRedirects:
        return {"status": -4, "error": "リダイレクト過多", "final_url": url}
    except requests.RequestException as e:
        return {"status": -9, "error": str(e), "final_url": url}


def _request_with_retry(url: str, method: str) -> dict:
    for attempt in range(1, RETRY_MAX + 1):
        result = _do_request(url, method)
        if result["status"] >= 0:
            return result
        if attempt < RETRY_MAX:
            log.debug(f"  リトライ {attempt}/{RETRY_MAX}: {result.get('error', '')}")
            time.sleep(1.0)
    return result


# ============================================================
# URL チェック本体
# ============================================================
def check_url(url: str, studio_title: str) -> dict:
    """
    HEAD → 失敗時 GET フォールバック。
    HEAD 成功時も content 確認のため GET を追加実施する。

    Returns:
      {http_status, final_url, redirect_count, is_redirect,
       content_keyword_found, alert_level, alert_reason, error_message}
    """
    # ---- HEAD リクエスト ----
    head = _request_with_retry(url, "HEAD")
    head_status = head["status"]

    # HEAD 非対応 (405) または接続エラー → GET フォールバック
    if head_status == 405 or head_status < 0:
        log.debug(f"  HEAD {head_status} → GET fallback")
        result = _request_with_retry(url, "GET")
    elif head_status < 300:
        # HEAD 成功 → コンテンツ確認のために GET も実施
        get = _request_with_retry(url, "GET")
        result = get if get["status"] >= 0 else head
    else:
        # 4xx/5xx → GET で確認
        result = _request_with_retry(url, "GET")

    status         = result["status"]
    final_url      = result.get("final_url", url)
    redirect_count = result.get("redirect_count", 0)
    is_redirect    = redirect_count > 0
    body           = result.get("body", "")
    error          = result.get("error", "")

    orig_domain  = extract_domain(url)
    final_domain = extract_domain(final_url)

    # ==== アラート判定 ====

    # (1) 接続エラー系
    if status < 0:
        level = "WARN" if status in (-2, -3) else "ERROR"
        label = {-1: "接続エラー", -2: "SSL証明書エラー",
                 -3: "タイムアウト", -4: "リダイレクト過多"}.get(status, "エラー")
        return _result(status, final_url, redirect_count, is_redirect,
                       False, level, f"{label}: {error}", error)

    # (2) 4xx / 5xx
    if status >= 400:
        return _result(status, final_url, redirect_count, is_redirect,
                       False, "ERROR", f"HTTP {status}", "")

    # (3) SNS・外部サービスへのリダイレクト
    if is_redirect and is_suspicious_domain(final_domain):
        return _result(status, final_url, redirect_count, is_redirect,
                       False, "ERROR",
                       f"SNS/外部サービスへリダイレクト: {final_domain}", "")

    # (4) ドメイン駐車ページ
    body_lower = body.lower()
    if body_lower:
        for kw in PARKING_KEYWORDS:
            if kw.lower() in body_lower:
                return _result(status, final_url, redirect_count, is_redirect,
                               False, "ERROR",
                               f"ドメイン駐車ページ検知: {kw!r}", "")

    # (5) ドメイン変更リダイレクト (www. 差異は無視)
    alert_level  = "OK"
    alert_reason = ""
    if is_redirect and orig_domain and final_domain and orig_domain != final_domain:
        alert_level  = "WARN"
        alert_reason = f"ドメイン変更リダイレクト: {orig_domain} → {final_domain}"

    # (6) コンテンツキーワード確認
    kw_found = False
    if body_lower and studio_title:
        kws = keywords_from_title(studio_title)
        for kw in kws:
            if kw.lower() in body_lower:
                kw_found = True
                break
        if not kw_found and not alert_reason:
            alert_level  = "WARN"
            alert_reason = "ページ内にスタジオ名キーワード未検出"

    return _result(status, final_url, redirect_count, is_redirect,
                   kw_found, alert_level, alert_reason, "")


def _result(
    status: int, final_url: str, redirect_count: int, is_redirect: bool,
    kw_found: bool, level: str, reason: str, error: str,
) -> dict:
    return {
        "http_status":           status,
        "final_url":             final_url,
        "redirect_count":        redirect_count,
        "is_redirect":           is_redirect,
        "content_keyword_found": kw_found,
        "alert_level":           level,
        "alert_reason":          reason,
        "error_message":         error,
    }


# ============================================================
# 1件チェック (キャッシュ対応)
# ============================================================
def check_one(
    url: str,
    studio_title: str,
    cache_df: pd.DataFrame,
    force_refresh: bool,
) -> dict:
    cache_key = normalize_url(url)
    match     = cache_df[cache_df["cache_key"] == cache_key]

    if not match.empty and is_cache_valid(match.iloc[0], force_refresh):
        cached = match.iloc[0]
        log.info(f"[CACHE] {studio_title}: {url}")
        return {
            "cache_key":             cache_key,
            "url":                   url,
            "studio_title":          studio_title,
            "http_status":           str(cached.get("http_status", "")),
            "final_url":             str(cached.get("final_url", url)),
            "redirect_count":        str(cached.get("redirect_count", "0")),
            "is_redirect":           str(cached.get("is_redirect", "False")),
            "content_keyword_found": str(cached.get("content_keyword_found", "False")),
            "alert_level":           str(cached.get("alert_level", "OK")),
            "alert_reason":          str(cached.get("alert_reason", "")),
            "checked_at":            str(cached.get("checked_at", "")),
            "error_message":         str(cached.get("error_message", "")),
            "source":                "cache",
        }

    log.info(f"[HTTP] {studio_title}: {url}")
    res = check_url(url, studio_title)

    level = res["alert_level"]
    icon  = "✓" if level == "OK" else ("!" if level == "WARN" else "×")
    log.info(
        f"  [{level}] {icon} HTTP {res['http_status']} "
        f"redirect={res['redirect_count']} kw={res['content_keyword_found']} "
        f"reason={res['alert_reason']!r}"
    )

    now    = datetime.datetime.now().isoformat(timespec="seconds")
    record = {
        "cache_key":             cache_key,
        "url":                   url,
        "studio_title":          studio_title,
        "http_status":           str(res["http_status"]),
        "final_url":             str(res["final_url"]),
        "redirect_count":        str(res["redirect_count"]),
        "is_redirect":           str(res["is_redirect"]),
        "content_keyword_found": str(res["content_keyword_found"]),
        "alert_level":           res["alert_level"],
        "alert_reason":          res["alert_reason"],
        "checked_at":            now,
        "error_message":         res["error_message"],
        "source":                "http",
    }
    return record


# ============================================================
# main
# ============================================================
def main() -> None:
    parser = argparse.ArgumentParser(description="公式サイトURL疎通・正当性チェック")
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
        help="30日キャッシュを無視してHTTPを再取得",
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

    df_studios = pd.read_csv(input_csv, dtype=str).fillna("")
    cache_df   = load_cache()

    results:      list[dict] = []
    checked_urls: set[str]   = set()

    log.info(f"入力CSV: {input_csv} ({len(df_studios)}件)")
    if args.force_refresh:
        log.info("[--force-refresh] キャッシュを無視して全件HTTP取得します")

    for _, row in df_studios.iterrows():
        title   = str(row.get("studio_title", "")).strip()
        website = str(row.get("website", "")).strip()

        # URL なし・重複をスキップ
        if not website or website.lower() in ("nan", ""):
            log.debug(f"[SKIP URL なし] {title}")
            continue
        if website in checked_urls:
            log.debug(f"[SKIP 重複] {title}: {website}")
            continue
        checked_urls.add(website)

        record   = check_one(website, title, cache_df, args.force_refresh)
        cache_df = upsert_cache(cache_df, record)
        results.append(record)

        # 実際にHTTPアクセスした場合は間隔を空ける
        if record.get("source") != "cache":
            time.sleep(0.5)

    # キャッシュ保存
    save_cache(cache_df)
    log.info(f"キャッシュ保存: {CACHE_CSV} ({len(cache_df)}件)")

    # ============================================================
    # レポート CSV 出力
    # ============================================================
    if results:
        report_df = pd.DataFrame(results).reindex(columns=REPORT_COLUMNS + ["source"])
        report_df = report_df[REPORT_COLUMNS]  # source は出力しない
    else:
        report_df = pd.DataFrame(columns=REPORT_COLUMNS)

    REPORT_CSV.parent.mkdir(parents=True, exist_ok=True)
    report_df.to_csv(REPORT_CSV, index=False, encoding="utf-8")
    log.info(f"レポートCSV: {REPORT_CSV}")

    # 警告サマリ (WARN + ERROR のみ)
    warn_df = report_df[report_df["alert_level"].isin(["WARN", "ERROR"])]
    warn_df.to_csv(WARNINGS_CSV, index=False, encoding="utf-8")
    log.info(f"警告サマリ: {WARNINGS_CSV} ({len(warn_df)}件)")

    # ============================================================
    # コンソールレポート
    # ============================================================
    total     = len(results)
    ok_cnt    = sum(1 for r in results if r.get("alert_level") == "OK")
    warn_cnt  = sum(1 for r in results if r.get("alert_level") == "WARN")
    err_cnt   = sum(1 for r in results if r.get("alert_level") == "ERROR")
    cache_hit = sum(1 for r in results if r.get("source") == "cache")

    print("\n[公式サイトURLチェック レポート]")
    print(f"入力CSV          : {input_csv}")
    print(f"チェックURL数    : {total}")
    print(f"キャッシュヒット : {cache_hit}")
    print(f"OK               : {ok_cnt}")
    print(f"WARN             : {warn_cnt}")
    print(f"ERROR            : {err_cnt}")

    if err_cnt > 0:
        print("\n[ERROR 一覧] ← step4 HTML生成時にこれらのURLは非表示になります")
        for r in results:
            if r.get("alert_level") == "ERROR":
                print(f"  × {r['studio_title']}")
                print(f"      URL  : {r['url']}")
                print(f"      理由 : {r['alert_reason']}")

    if warn_cnt > 0:
        print("\n[WARN 一覧]")
        for r in results:
            if r.get("alert_level") == "WARN":
                print(f"  ! {r['studio_title']}")
                print(f"      URL  : {r['url']}")
                print(f"      理由 : {r['alert_reason']}")

    print(f"\nレポートCSV      : {REPORT_CSV}")
    print(f"警告サマリCSV    : {WARNINGS_CSV}")
    print(f"キャッシュ保存先 : {CACHE_CSV}")

    if err_cnt > 0:
        print(f"\n⚠ {err_cnt}件のURLにERRORがあります。")
        print("  step4_generate_shinjuku.py を実行するとこれらのURLは自動的に非表示になります。")
    elif warn_cnt > 0:
        print(f"\n! {warn_cnt}件のURLにWARNがあります。内容を確認してください。")
    else:
        print("\n✓ すべてのURLが正常です。")


if __name__ == "__main__":
    main()
