#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
公式サイトをスクレイピングして Claude でスタジオ設備情報を抽出する。
入力: data/pipeline/shinjuku_station_selected_studios.csv
出力: data/pipeline/studio_site_details.csv
チェックポイント: checkpoints/studio_site_details.json

実行:
    python3 fetch_studio_details.py
    python3 fetch_studio_details.py --reset   # チェックポイント削除して再実行
"""

import argparse
import json
import os
import re
import time
from pathlib import Path

import anthropic
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
STATION_CSV  = "data/pipeline/shinjuku_station_selected_studios.csv"
OUTPUT_CSV   = "data/pipeline/studio_site_details.csv"
CHECKPOINT   = "checkpoints/studio_site_details.json"

FETCH_TIMEOUT   = 12      # HTTP タイムアウト (秒)
FETCH_SLEEP     = 1.2     # スタジオ間のスリープ (秒)
API_MAX_TOKENS  = 600
JS_TEXT_THRESH  = 400     # テキストがこれ未満 → JS依存と判定
EXTRACT_CHARS   = 9000    # Claude に渡す最大文字数

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja,en-US;q=0.7,en;q=0.3",
}

EXTRACT_SYSTEM = """あなたはレンタルスタジオ情報の構造化抽出エキスパートです。
与えられたWebページのテキストから、ダンサーが練習場所を選ぶ際に必要な情報を抽出します。"""

EXTRACT_PROMPT = """\
以下はレンタルスタジオの公式サイトのテキスト内容です。
ダンス練習を目的とするユーザーに役立つ情報をJSONで抽出してください。

【抽出項目（情報がなければ null）】
- area_sqm        : 最大フロア面積（数値のみ。例: 50.0）
- ceiling_height_m: 天井高（数値のみ。例: 2.5）
- capacity        : 最大定員または推奨最大人数（整数のみ）
- floor_type      : 床材。"フローリング"/"リノリウム"/"カーペット"/"タイル"/"その他"/"不明" のどれか
- has_mirror      : 鏡あり true/false/null
- has_speaker     : スピーカー・音響設備あり true/false/null
- has_tripod      : 三脚あり true/false/null
- has_shower      : シャワーあり true/false/null
- has_changing_room: 更衣室・更衣スペースあり true/false/null
- hourly_rate_yen : 1時間あたりの最安料金（整数のみ）
- dancer_summary  : ダンサー目線での特記事項（25文字以内の日本語。例: "三脚常備・LED照明で動画映え"）

JSONのみ返してください。余分な説明は不要です。"""


# ============================================================
# HTML取得
# ============================================================
def fetch_text(url: str) -> tuple[str, str]:
    """
    Returns: (text, status)
      status: "ok" / "js_required" / "timeout" / "http_error:N" / "error:msg"
    """
    try:
        resp = requests.get(url, headers=HEADERS, timeout=FETCH_TIMEOUT,
                            allow_redirects=True)
        resp.encoding = resp.apparent_encoding or "utf-8"

        if resp.status_code >= 400:
            return "", f"http_error:{resp.status_code}"

        html = resp.text

        # <script>/<style> 除去
        clean = re.sub(r"<script[^>]*>.*?</script>", " ", html, flags=re.DOTALL | re.IGNORECASE)
        clean = re.sub(r"<style[^>]*>.*?</style>",  " ", clean, flags=re.DOTALL | re.IGNORECASE)
        clean = re.sub(r"<[^>]+>", " ", clean)
        clean = re.sub(r"\s+", " ", clean).strip()

        if len(clean) < JS_TEXT_THRESH:
            return "", "js_required"

        return clean[:EXTRACT_CHARS], "ok"

    except requests.exceptions.Timeout:
        return "", "timeout"
    except Exception as e:
        return "", f"error:{str(e)[:60]}"


# ============================================================
# Claude による抽出
# ============================================================
def _parse_json_robust(raw: str) -> dict:
    """
    モデル出力から JSON を堅牢に抽出する。
    1. ```json ... ``` ブロック
    2. 文字列全体
    3. 最初の { から対応する } までをブレース深さで探索
    のいずれかで試みる。
    """
    # 1. fenced code block
    m = re.search(r"```json\s*(.*?)```", raw, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except json.JSONDecodeError:
            pass

    # 2. 文字列全体
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # 3. ブレース対応で最初の完全な {...} を取り出す
    start = raw.find("{")
    if start >= 0:
        depth = 0
        for i, c in enumerate(raw[start:], start):
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(raw[start : i + 1])
                    except json.JSONDecodeError:
                        break

    raise json.JSONDecodeError("JSONを抽出できませんでした", raw, 0)


def _flatten_if_multi_room(data: dict) -> dict:
    """
    モデルが {部屋名: {field: value}, ...} のネスト形式で返した場合、
    面積が最大の部屋のデータをフラット化して返す。
    """
    if not data:
        return data
    # 全値がdictならマルチルーム形式と判断
    if all(isinstance(v, dict) for v in data.values()):
        best = max(data.values(), key=lambda r: r.get("area_sqm") or 0)
        return best
    return data


def extract_with_claude(client: anthropic.Anthropic, studio_title: str, text: str) -> dict:
    user_msg = f"スタジオ名: {studio_title}\n\n---\n{text}"

    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=API_MAX_TOKENS,
        system=EXTRACT_SYSTEM,
        messages=[
            {"role": "user", "content": EXTRACT_PROMPT + "\n\n" + user_msg}
        ],
    )
    raw = msg.content[0].text.strip()
    data = _parse_json_robust(raw)
    return _flatten_if_multi_room(data)


# ============================================================
# メイン
# ============================================================
RESULT_COLS = [
    "studio_title", "official_url", "fetch_status",
    "area_sqm", "ceiling_height_m", "capacity", "floor_type",
    "has_mirror", "has_speaker", "has_tripod",
    "has_shower", "has_changing_room", "hourly_rate_yen", "dancer_summary",
]


RETRY_STATUSES = {"parse_error"}  # これらのステータスは --retry-errors で再試行


def _should_retry(entry: dict, retry_errors: bool) -> bool:
    """チェックポイントのエントリを再試行すべきか判断する"""
    status = entry.get("fetch_status", "")
    if retry_errors and status in RETRY_STATUSES:
        return True
    # ok だが有用フィールドが全て None/空 → 再試行
    if status == "ok":
        useful = any(
            entry.get(k) not in (None, "", "不明", "null")
            for k in ("area_sqm", "floor_type", "has_mirror", "has_speaker",
                      "has_tripod", "dancer_summary")
        )
        if not useful and retry_errors:
            return True
    return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset",        action="store_true", help="チェックポイントを削除して最初から実行")
    parser.add_argument("--retry-errors", action="store_true", help="parse_error / データ空のエントリを再取得")
    args = parser.parse_args()

    Path("checkpoints").mkdir(exist_ok=True)
    cp_path = Path(CHECKPOINT)

    if args.reset and cp_path.exists():
        cp_path.unlink()
        print("[INFO] チェックポイントをリセットしました")

    # チェックポイント読み込み
    checkpoint: dict = {}
    if cp_path.exists():
        checkpoint = json.loads(cp_path.read_text(encoding="utf-8"))

    df = pd.read_csv(STATION_CSV)
    client = anthropic.Anthropic()

    new_count = 0
    for _, row in df.iterrows():
        title = str(row.get("studio_title", "")).strip()
        url   = str(row.get("official_url", "") or row.get("website", "")).strip()

        if not url or url.lower() == "nan":
            print(f"[SKIP] {title} (URLなし)")
            continue

        if title in checkpoint:
            prev = checkpoint[title]
            if _should_retry(prev, args.retry_errors):
                print(f"[RETRY] {title} (前回: {prev.get('fetch_status')})")
                del checkpoint[title]
            else:
                print(f"[SKIP] {title} (済: {prev.get('fetch_status')})")
                continue

        print(f"\n[FETCH] {title}")
        print(f"        {url}")

        text, status = fetch_text(url)
        print(f"  → fetch: {status} / {len(text)} chars")

        result: dict = {"studio_title": title, "official_url": url, "fetch_status": status}

        if status == "ok" and text:
            try:
                details = extract_with_claude(client, title, text)
                result.update(details)
                # 主要フィールドをプレビュー
                preview = {k: v for k, v in details.items() if v is not None and v != "不明" and v != ""}
                print(f"  → 抽出: {preview}")
            except json.JSONDecodeError as e:
                print(f"  → JSON parse error: {e}")
                result["fetch_status"] = "parse_error"
            except Exception as e:
                print(f"  → API error: {e}")
                result["fetch_status"] = f"api_error:{str(e)[:60]}"

        checkpoint[title] = result
        cp_path.write_text(json.dumps(checkpoint, ensure_ascii=False, indent=2), encoding="utf-8")
        new_count += 1
        time.sleep(FETCH_SLEEP)

    # CSV 出力
    all_results = list(checkpoint.values())
    out_df = pd.DataFrame(all_results)
    for col in RESULT_COLS:
        if col not in out_df.columns:
            out_df[col] = None
    out_df = out_df[RESULT_COLS]
    out_df.to_csv(OUTPUT_CSV, index=False, encoding="utf-8-sig")

    print(f"\n{'='*50}")
    print(f"完了: {OUTPUT_CSV}")
    print(f"  新規取得: {new_count} 件 / 合計: {len(out_df)} 件")
    ok = (out_df["fetch_status"] == "ok").sum()
    js = (out_df["fetch_status"] == "js_required").sum()
    err = len(out_df) - ok - js
    print(f"  OK: {ok}件  JS依存: {js}件  エラー: {err}件")


if __name__ == "__main__":
    main()
