#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SEO記事生成スクリプト v2
article_top15_final.csv + studio CSV + descriptions CSV → HTML記事ファイル

使い方:
  python3 generate_articles.py --article "新宿のレンタルダンススタジオおすすめ"
  python3 generate_articles.py --all
"""

import re
import math
import json
import base64
import shutil
import logging
import argparse
import urllib.parse
import unicodedata
import pandas as pd
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

# ============================================================
# 設定
# ============================================================
TOP15_CSV       = "data/pipeline/article_top15_final.csv"
STUDIO_CSV      = "data/master/studio_with_tags_dslurl_address_filled.csv"
DSL_ROOM_CSV    = "data/master/studio_room_all_columns_excel.csv"
DESC_CSV        = "data/pipeline/studio_descriptions.csv"
PLACES_API_CSV  = "data/pipeline/places_api_results.csv"
OUTPUT_DIR      = Path("articles")
SUMMARY_CSV     = "data/pipeline/article_generation_summary.csv"
SELECTED_CSV    = "data/pipeline/article_selected_studios.csv"

DSL_SITE_URL    = "https://dance-studio-lab.com"
CANONICAL_BASE  = "https://dance-studio-lab.com/media/"   # 記事公開先ベースURL
OGP_IMAGE_URL   = "https://dance-studio-lab.com/media/ogp-default.jpg"  # デフォルトOGP画像

# ルーム画像
ROOM_IMAGES_DIR = Path("room_images_backup/private/uploads/room")
DSL_IMAGE_BASE  = "https://dance-studio-lab.com/private/uploads/room"

MIN_GROUPS           = 10
MAX_GROUPS           = 15
MAX_PARTNERS         = 8
RENTAL_FIT_THRESHOLD = 3   # 非提携スタジオはこれ未満を除外

PRIORITY_BONUS = {"S": 20, "A": 10, "B": 0}

# ============================================================
# ブランドグルーピングルール
# (正規化済みキーワード, カノニカルグループ名) の順序リスト
# 先に書いたものが優先される
# ============================================================
BRAND_GROUP_RULES: list[tuple[str, str]] = [
    ("buzz",               "BUZZ レンタルスタジオ"),
    ("bassontop",          "ダンススペース ベースオントップ"),
    ("ベースオントップ",   "ダンススペース ベースオントップ"),
    ("studionoah",         "サウンドスタジオノア"),
    ("サウンドスタジオノア", "サウンドスタジオノア"),
    ("noahstudio",         "ノアスタジオ"),
    ("ノアスタジオ",       "ノアスタジオ"),
    ("noadance",           "NOAダンス"),
    ("noaballet",          "NOAバレエ"),
    ("noa",                "NOA"),
    ("landstudio",         "LANDstudio"),
    ("studioonda",         "Studioonda"),
    ("roots",              "ROOTS"),
    ("worcle",             "Studio Worcle"),
    ("ワークル",           "Studio Worcle"),
    ("ソニズ",             "ソニズダンスタジオ"),
    ("stepps",             "STEPPSダンススタジオ"),
    ("ステップス",         "ステップスアーツ"),
    ("hinastudio",         "Hina STUDIO"),
    ("ヒナスタジオ",       "Hina STUDIO"),
    ("aivic",              "レンタルスタジオAivic"),
    ("チェリッシュスタジオ", "チェリッシュスタジオ"),
    ("cherish",            "チェリッシュスタジオ"),
    ("dubway",             "Dubwayレンタルスタジオ"),
    ("スタジオピアーチェ", "スタジオピアーチェ"),
]

# レンタルダンス適合スコア: プラス要因
RENTAL_HIGH_KW = [
    "レンタルダンス", "レンタルスタジオ", "ダンススタジオ",
    "ダンススペース", "dance studio", "rental studio",
    "チアダンス", "ベリーダンス", "フラダンス",
]
# マイナス要因（スクール・教室系）: -4
RENTAL_LOW_KW = [
    "スクール", "アカデミー", "バレエスクール",
    "フォトスタジオ", "photo studio", "教室",
    "school", "academy",
]
# 完全非ダンス系（音楽スタジオ・専業ヨガ・ジム等）: -5
NON_DANCE_KW = [
    # 音楽スタジオ
    "サウンドスタジオ", "音楽スタジオ", "バンドスタジオ", "ピアノスタジオ",
    "アカペラスタジオ", "sound studio", "rinky dink",
    # ヨガ専業
    "ホットヨガ",
    # ジム・スポーツ施設
    "スポーツオアシス", "ゴールドジム", "joyfit", "フィットネスジム",
    # 格闘技・道場
    "道場",
]
# 非ダンス寄り（ヨガ・ピラティス・フィットネス等）: -3
NON_DANCE_LIGHT_KW = [
    "ヨガスタジオ", "ピラティス", "pilates", "フィットネス", "gym",
]

# NG表現の置換ルール（紹介文サニタイズ用）
NG_PATTERNS: list[tuple[str, str]] = [
    (r"レッスンを受け(られ|る|やすい|、)", "利用し"),
    (r"講師(から|の指導|によって|に)", "スタッフのサポートで"),
    (r"(ダンス)?スクール(として|の|に)おすすめ", "レンタルスタジオとして利用しやすい"),
    (r"クラスを(開講|実施)", "レンタル利用できる"),
    (r"レッスン枠", "利用枠"),
    (r"初心者から(上級者|経験者)まで(通え|通学|レッスン|学べ)", "幅広いダンサーが練習場所として使いやすく"),
    (r"初心者(でも|から)", "ダンス練習で"),
    (r"通(える|学|い、|い。)", "利用"),
    (r"プロ(の|による|講師)", ""),
    (r"(通|学べ)るスタジオ", "使えるスタジオ"),
    # 禁止フレーズ（テンプレート汚染）
    (r"本格的な音響設備(を備え|が充実|があり)", "音響設備があり"),
    (r"ダンサーの動きに配慮した素材", "ダンス練習に使いやすい床材"),
    (r"ターンやジャンプも安心(して(こなせ|使え|練習でき)ます)?。?", ""),
    (r"最適な環境が整っています", "環境が整っています"),
    (r"(大人気|リピーター多数)", ""),
    (r"(誰にでも|絶対に)(おすすめ|安心)", "おすすめ"),
]

# ============================================================
# DSL ルームデータ 読み込み・提携スタジオ説明文生成
# ============================================================

# 絵文字・装飾文字を除去する正規表現
_EMOJI_RE = re.compile(
    "[\U00010000-\U0010ffff\U0001F300-\U0001F9FF"
    "\u2600-\u27BF\u2702-\u27B0\u203C\u2049"
    "\u231A\u231B\u23E9-\u23FA]+"
)


def _clean_room_text(text) -> str:
    """ルーム紹介文・キャッチコピーからURL・絵文字・装飾を除去する"""
    if not text or (hasattr(text, '__float__') and math.isnan(float(text) if isinstance(text, float) else 0)):
        return ""
    text = str(text)
    text = re.sub(r"https?://\S+", "", text)
    text = _EMOJI_RE.sub("", text)
    text = re.sub(r"[【】✨🎉💫⭐★☆♪♫❤💕👍🙏\[\]]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def load_dsl_room_data(csv_path: str) -> dict:
    """
    studio_room_all_columns_excel.csv を読み込み検索インデックスを構築する。

    Returns:
        {
            'studios': {studio_id: {
                'studio_name': str,
                'catchphrase':  str,
                'reserve_type': int,   # 0=リクエスト予約 / 1=即時予約
                'rooms':        DataFrame,
            }},
            'by_name':   {studio_name: studio_id},
            'by_dslurl': {room_dslurl: studio_id},   # 全ルームURL → studio_id
        }
    """
    path = Path(csv_path)
    if not path.exists():
        log.warning(f"DSLルームCSV が見つかりません: {csv_path}")
        return {}

    df = pd.read_csv(csv_path)
    # テスト・ダミースタジオを除外
    df = df[~df['studio_name'].astype(str).str.contains(
        r'TEST|test|ダミー|^ああ|Rental Studio Lab\d*|^Aスタジオ$|^Bスタジオ$',
        na=False, regex=True,
    )]
    df = df[df['studio_id'].notna()]

    result: dict = {'studios': {}, 'by_name': {}, 'by_dslurl': {}}

    for studio_id, group in df.groupby('studio_id'):
        row0 = group.iloc[0]
        studio_name  = str(row0.get('studio_name', ''))
        catchphrase  = _clean_room_text(row0.get('studio_catchphrase', ''))
        reserve_type = (int(row0.get('studio_reserve_type', 1))
                        if pd.notna(row0.get('studio_reserve_type')) else 1)

        studio_data = {
            'studio_name':  studio_name,
            'catchphrase':  catchphrase,
            'reserve_type': reserve_type,
            'rooms':        group.reset_index(drop=True),
        }
        result['studios'][studio_id] = studio_data
        result['by_name'][studio_name] = studio_id

        # 各ルームの dslurl → studio_id
        for url in group['dslurl'].dropna().astype(str):
            if url.startswith('http'):
                result['by_dslurl'][url] = studio_id

    log.info(f"  DSLルームデータ: {len(result['studios'])}スタジオ / {len(df)}ルーム")
    return result


def _find_dsl_studio(studio_title: str, dslurl: str, dsl_data: dict) -> Optional[dict]:
    """studio_title または dslurl から DSL スタジオ情報を返す（見つからない場合は None）"""
    if not dsl_data:
        return None
    studios = dsl_data.get('studios', {})

    # 1. 完全名称一致
    sid = dsl_data.get('by_name', {}).get(studio_title)
    if sid and sid in studios:
        return studios[sid]

    # 2. dslurl 一致（master CSV の dslurl はルームURLと一致する）
    if dslurl and dslurl.startswith('http'):
        sid = dsl_data.get('by_dslurl', {}).get(dslurl)
        if sid and sid in studios:
            return studios[sid]

    return None


def _gen_desc_from_rooms(rooms: pd.DataFrame, catchphrase: str, reserve_type: int) -> str:
    """
    ルームの設備データから自然な日本語説明文（2〜4文）を生成する。
    室内スペック・音響・アメニティ・撮影設備・予約方式を具体的に盛り込む。
    """
    parts: list[str] = []
    room_count = len(rooms)

    # ─── ルーム数・広さ・ミラー・床材 ───────────────────────
    spec: list[str] = []

    spec.append(f"{room_count}室のダンスルームを完備" if room_count >= 2
                else "1室完備のレンタルダンスルーム")

    sizes = rooms['room_square_meters'].dropna().tolist()
    if sizes:
        if len(sizes) >= 2 and max(sizes) - min(sizes) > 5:
            spec.append(f"広さ{int(min(sizes))}〜{int(max(sizes))}㎡")
        else:
            spec.append(f"広さ約{int(round(sum(sizes) / len(sizes)))}㎡")

    mirror_vals = rooms['room_mirror'].dropna().astype(int)
    if len(mirror_vals) > 0:
        max_m = int(mirror_vals.max())
        if max_m >= 3:
            spec.append("3面以上のミラー設置")
        elif max_m == 2:
            spec.append("2面ミラー設置")
        elif max_m == 1:
            spec.append("ミラー設置")

    quality_vals = rooms['room_floor_quality'].dropna().astype(int)
    if len(quality_vals) > 0:
        q_label = {0: "リノリウム", 1: "フローリング"}
        quals = list(dict.fromkeys(q_label[q] for q in quality_vals if q in q_label))
        if quals:
            spec.append("/".join(quals) + "床")

    if spec:
        parts.append("、".join(spec))

    # ─── 音響・Wi-Fi・エアコン ────────────────────────────
    equip: list[str] = []
    has_bt     = rooms['room_speaker_bluetooth_flg'].eq('t').any()
    has_wired  = rooms['room_speaker_wired_flg'].eq('t').any()
    has_wifi   = rooms['room_free_wifi_flg'].eq('t').any()
    has_ac     = rooms['room_air_conditioner_flg'].eq('t').any()
    has_bamiri = rooms['room_bamiri_flg'].eq('t').any()

    if has_bt and has_wired:
        equip.append("Bluetooth・有線スピーカー完備")
    elif has_bt:
        equip.append("Bluetoothスピーカー完備")
    elif has_wired:
        equip.append("有線スピーカー完備")

    if has_wifi:
        equip.append("無料Wi-Fi完備")
    if has_ac:
        equip.append("エアコン完備")
    if has_bamiri:
        equip.append("バミリテープ無料貸出あり")

    if equip:
        parts.append("、".join(equip))

    # ─── アメニティ ──────────────────────────────────────
    amenity: list[str] = []
    if rooms['room_dressing_room_flg'].eq('t').any():
        amenity.append("更衣室")
    if rooms['room_toilet_flg'].eq('t').any():
        amenity.append("トイレ")
    if rooms['room_ballet_bar_flg'].eq('t').any():
        amenity.append("バレエバー")
    if rooms['room_window_flg'].eq('t').any():
        amenity.append("自然光の入る窓")
    ceil_vals = rooms['room_ceiling'].dropna()
    if len(ceil_vals) > 0 and (ceil_vals.astype(int) > 0).any():
        amenity.append("天井高5m以上")

    if amenity:
        parts.append("・".join(amenity) + "完備")

    # ─── 撮影設備 ─────────────────────────────────────────
    shoot: list[str] = []
    if rooms['room_shoot_flg'].eq('t').any():
        shoot.append("撮影向きルーム")
    if rooms['room_free_lighting_flg'].eq('t').any():
        shoot.append("照明機材無料レンタル")
    elif rooms['room_paid_lighting_flg'].eq('t').any():
        shoot.append("照明機材有料レンタル")
    if (rooms['room_free_camera_tripod_flg'].eq('t').any()
            or rooms['room_free_smartphone_tripod_flg'].eq('t').any()):
        shoot.append("三脚無料レンタル")
    if rooms['room_image_360_degree_url'].notna().any():
        shoot.append("360°VRビュー対応")

    if shoot:
        parts.append("・".join(shoot) + "あり")

    body = "。".join(parts) + "。"

    # ─── キャッチコピーを先頭に付加（適切な場合のみ）──────
    cp = catchphrase.strip("。！") if catchphrase else ""
    if cp and 8 <= len(cp) <= 60 and "\n" not in cp and "http" not in cp:
        body = cp + "。" + body

    return body


def build_dsl_partner_desc(studio_title: str, dslurl: str, dsl_data: dict) -> str:
    """提携スタジオのルームデータから説明文を生成する。見つからない場合は ""。"""
    info = _find_dsl_studio(studio_title, dslurl, dsl_data)
    if info is None:
        return ""
    return _gen_desc_from_rooms(info['rooms'], info['catchphrase'], info['reserve_type'])


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
def safe_float(val) -> Optional[float]:
    try:
        v = float(val)
        return None if math.isnan(v) else v
    except (TypeError, ValueError):
        return None


def safe_int(val) -> Optional[int]:
    f = safe_float(val)
    return int(f) if f is not None else None


def _norm_addr(a) -> str:
    """住所正規化（全角→半角・丁目変換・番地部分のみ抽出）"""
    s = unicodedata.normalize('NFKC', str(a or ''))
    s = re.sub(r'[\u2010-\u2015\u2212\uff0d]', '-', s)  # ダッシュ系→ハイフン
    s = re.sub(r'〒?\s*\d{3}-\d{4}\s*', '', s)           # 郵便番号除去
    s = s.lower()
    s = re.sub(r'(\d+)丁目', r'\1-', s)
    s = re.sub(r'(\d+)番地?', r'\1-', s)
    # スペース除去前に番地（N-N形式）を抽出（建物名・階数との結合を防ぐ）
    m = re.search(r'(\d+(?:-\d+)+)', s)
    if m:
        prefix = re.sub(r'[\s\u3000]', '', s[:m.start()])
        s = prefix + m.group(1)
    else:
        s = re.sub(r'[\s\u3000]', '', s)
    return s.rstrip('-_.,')


def decode_dslurl_to_uuid(dslurl: str) -> Optional[str]:
    """DSLルームURL の base64 部分をデコードして UUID を返す"""
    if not dslurl:
        return None
    try:
        m = re.search(r'/room/([A-Za-z0-9+/=]+)/', dslurl)
        if not m:
            return None
        b64 = m.group(1)
        padded = b64 + "=" * (4 - len(b64) % 4)
        decoded = base64.b64decode(padded).decode("utf-8")
        if re.match(
            r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", decoded
        ):
            return decoded
    except Exception:
        pass
    return None


def get_studio_images(dslurl: str, max_images: int = 2) -> list[tuple[str, str]]:
    """DSLルームURLから (uuid, filename) リストを返す。画像なしなら []"""
    uuid = decode_dslurl_to_uuid(dslurl)
    if not uuid:
        return []
    img_dir = ROOM_IMAGES_DIR / uuid / "main_images"
    if not img_dir.exists():
        return []
    imgs = sorted(img_dir.glob("*.jpg")) + sorted(img_dir.glob("*.png")) + sorted(img_dir.glob("*.webp"))
    return [(uuid, p.name) for p in imgs[:max_images]]


def dist_to_minutes(meters) -> Optional[str]:
    try:
        m = float(meters)
        if math.isnan(m):
            return None
        return f"徒歩{math.ceil(m / 80)}分"
    except (TypeError, ValueError):
        return None


def fmt_rating_html(rating, review_count) -> str:
    """Google評価 HTML。NaN の場合は空文字を返す（絶対に★nanを出さない）"""
    r = safe_float(rating)
    if r is None:
        return ""
    rc = safe_int(review_count)
    if rc:
        return f'<div class="studio-rating">Google評価 {r:.1f}（{rc}件）</div>'
    return f'<div class="studio-rating">Google評価 {r:.1f}</div>'


def fmt_rating_text(rating, review_count) -> tuple[str, str]:
    """比較表用: (評価テキスト, 口コミ数テキスト)"""
    r = safe_float(rating)
    if r is None:
        return "—", "—"
    rc = safe_int(review_count)
    return f"{r:.1f}", (str(rc) if rc else "—")


def normalize_name(name: str) -> str:
    s = str(name).lower()
    s = s.translate(str.maketrans(
        "ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺ"
        "ａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚ"
        "０１２３４５６７８９",
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789",
    ))
    s = re.sub(r"[\s\-_・　]+", "", s)
    return s


def get_group_key(studio_title: str) -> str:
    """スタジオ名からグループキー（カノニカル名）を取得"""
    norm = normalize_name(studio_title)
    for kw, canonical in BRAND_GROUP_RULES:
        if normalize_name(kw) in norm:
            return canonical
    # マッチしない場合: 店舗サフィックスを除去して基本名を返す
    base = re.sub(
        r"[\s　]*(新宿|渋谷|池袋|秋葉原|銀座|品川|恵比寿|中野|高田馬場|代々木|"
        r"大久保|新大久保|錦糸町|浅草|吉祥寺|下北沢|三軒茶屋|"
        r"本校|本店|\d+号店|[A-Z]\d*店|号館|[東西南北]\d*店|校|支店|店|地下\d+)$",
        "", studio_title
    ).strip()
    return base or studio_title


def extract_area_or_station(article_title: str, article_type: str) -> str:
    if article_type == "エリア":
        m = re.match(r"^(.+?)のレンタルダンス", article_title)
        return m.group(1) if m else article_title
    elif article_type == "駅":
        m = re.match(r"^(.+?駅)のレンタルダンス", article_title)
        return m.group(1) if m else article_title
    return ""


def slugify(title: str) -> str:
    s = re.sub(r"[^\w\u3040-\u9fff\u30a0-\u30ff\u4e00-\u9fff]", "-", title)
    return re.sub(r"-+", "-", s).strip("-")


def sanitize_desc(desc: str) -> str:
    """紹介文からレッスン・スクール系の表現を除去・置換"""
    for pattern, replacement in NG_PATTERNS:
        desc = re.sub(pattern, replacement, desc)
    return desc.strip()


# ============================================================
# 営業時間パース
# ============================================================
_DAY_ORDER = {"月曜日": 0, "火曜日": 1, "水曜日": 2, "木曜日": 3,
              "金曜日": 4, "土曜日": 5, "日曜日": 6}
_DAY_SHORT  = ["月", "火", "水", "木", "金", "土", "日"]


def parse_business_hours(raw) -> str:
    """
    '2024-02-29 木曜日 17時00分～23時00分\n...' を
    '月〜金: 17:00〜23:00 / 土: 13:00〜23:00 / 日: 13:00〜21:30' 形式に変換。
    全日同じなら '毎日 24時間営業' などに短縮。
    """
    if not raw or str(raw).strip() in ("nan", ""):
        return ""
    day_hours: dict[int, str] = {}
    for line in str(raw).strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        parts = line.split(" ", 2)
        if len(parts) < 3:
            continue
        day_name = parts[1]
        hours_str = parts[2].strip()
        idx = _DAY_ORDER.get(day_name)
        if idx is None:
            continue
        if "24 時間営業" in hours_str or "24時間営業" in hours_str:
            h = "24時間"
        else:
            h = re.sub(
                r"(\d+)時(\d+)分～(\d+)時(\d+)分",
                lambda m: f"{m.group(1)}:{m.group(2)}〜{m.group(3)}:{m.group(4)}",
                hours_str,
            )
        day_hours[idx] = h

    if not day_hours:
        return ""

    # 全曜日同じかチェック
    vals = list(day_hours.values())
    if len(day_hours) == 7 and len(set(vals)) == 1:
        h = vals[0]
        return "毎日 24時間営業" if h == "24時間" else f"毎日 {h}"

    # 連続する同一時間帯をグループ化
    sorted_items = sorted(day_hours.items())
    groups: list[tuple[list[int], str]] = []
    cur_days   = [sorted_items[0][0]]
    cur_hours  = sorted_items[0][1]
    for day_idx, hours in sorted_items[1:]:
        if hours == cur_hours and day_idx == cur_days[-1] + 1:
            cur_days.append(day_idx)
        else:
            groups.append((cur_days, cur_hours))
            cur_days  = [day_idx]
            cur_hours = hours
    groups.append((cur_days, cur_hours))

    parts_out = []
    for days, hours in groups:
        if len(days) == 1:
            label = _DAY_SHORT[days[0]]
        elif len(days) == 2:
            label = f"{_DAY_SHORT[days[0]]}・{_DAY_SHORT[days[1]]}"
        else:
            label = f"{_DAY_SHORT[days[0]]}〜{_DAY_SHORT[days[-1]]}"
        parts_out.append(f"{label}: {hours}")
    return " / ".join(parts_out)


# ============================================================
# 施設タイプ分類
# ============================================================
USAGE_TYPE_DANCE   = "ダンス主用途"
USAGE_TYPE_MULTI   = "多目的レンタル"
USAGE_TYPE_PHOTO   = "撮影兼用"
USAGE_TYPE_PILATES = "ピラティス・ヨガ兼用"
USAGE_TYPE_MUSIC   = "音楽・防音兼用"
USAGE_TYPE_UNKNOWN = "要確認"

_USAGE_KW_MUSIC   = ["サウンドスタジオ", "音楽スタジオ", "バンドスタジオ", "ピアノスタジオ",
                      "防音スタジオ", "sound studio", "rinky dink"]
_USAGE_KW_PHOTO   = ["撮影スタジオ", "フォトスタジオ", "photo studio", "シロホリ",
                      "クロホリ", "ホリゾント"]
_USAGE_KW_PILATES = ["ピラティス", "pilates", "マシンピラティス", "ホットヨガ", "ヨガスタジオ"]
_USAGE_KW_DANCE   = ["レンタルダンス", "ダンススタジオ", "ダンスルーム", "dance studio",
                      "ダンススペース"]
_USAGE_KW_MULTI   = ["レンタルスタジオ", "レンタルスペース", "rental studio",
                      "rental space", "多目的"]


def classify_usage_type(row) -> str:
    """施設タイプを分類する（優先順: 音楽 > 撮影 > ピラティス > ダンス > 多目的 > 要確認）"""
    name_l = str(row.get("studio_title", row.get("タイトル", ""))).lower()
    web_l  = str(row.get("Webサイト", "")).lower()
    text   = name_l + " " + web_l

    for kw in _USAGE_KW_MUSIC:
        if kw.lower() in text:
            return USAGE_TYPE_MUSIC
    for kw in _USAGE_KW_PHOTO:
        if kw.lower() in text:
            return USAGE_TYPE_PHOTO
    for kw in _USAGE_KW_PILATES:
        if kw.lower() in text:
            return USAGE_TYPE_PILATES
    for kw in _USAGE_KW_DANCE:
        if kw.lower() in text:
            return USAGE_TYPE_DANCE
    for kw in _USAGE_KW_MULTI:
        if kw.lower() in text:
            return USAGE_TYPE_MULTI
    return USAGE_TYPE_UNKNOWN


# ============================================================
# レンタルダンス適合スコア
# ============================================================
def calc_rental_fit_score(row: pd.Series) -> int:
    """
    レンタルダンススタジオとしての適合スコア (0〜10)
    高いほどレンタル練習用途向け
    """
    score = 5
    name_l = str(row.get("studio_title", "")).lower()
    tags_l = str(row.get("article_tags", "")).lower()
    web_l  = str(row.get("Webサイト", "")).lower()

    # + dslurl あり（提携確認済み）
    dslurl = str(row.get("dslurl", "")).strip()
    if pd.notna(row.get("dslurl")) and dslurl:
        score += 3

    # + 名称にレンタル/ダンス系
    if any(kw.lower() in name_l for kw in RENTAL_HIGH_KW):
        score += 2

    # + タグにレンタル系
    if "type:レンタル" in tags_l or "rental" in tags_l:
        score += 1

    # + WebサイトURLにdance/rental/studio
    if any(kw in web_l for kw in ["dance", "rental", "studio"]):
        score += 1

    # − スクール/アカデミー系
    if any(kw.lower() in name_l for kw in RENTAL_LOW_KW):
        score -= 4

    # − 完全非ダンス系（音楽スタジオ・専業ヨガ・ジム等）
    if any(kw.lower() in name_l for kw in NON_DANCE_KW):
        score -= 5

    # − 非ダンス寄り（ヨガ・ピラティス・フィットネス等）
    if any(kw.lower() in name_l for kw in NON_DANCE_LIGHT_KW):
        score -= 3

    return max(0, min(10, score))


# ============================================================
# グルーピング
# ============================================================
@dataclass
class StudioGroup:
    group_key:  str
    group_name: str
    studios:    list = field(default_factory=list)  # list of pd.Series

    # 代表値（グループ内最良）
    best_rating:       Optional[float] = None
    best_review_count: Optional[int]   = None
    primary_station:   str             = ""
    primary_dist_min:  Optional[str]   = None
    address:           str             = ""
    website:           str             = ""

    # 提携
    is_partner: bool = False
    dslurl_map: dict = field(default_factory=dict)  # studio_title → dslurl

    # 施設タイプ
    usage_type: str = USAGE_TYPE_UNKNOWN

    # スコア
    rental_fit_score: float = 0
    selection_score:  float = 0
    selection_reason: list  = field(default_factory=list)

    def best_desc(self, desc_map: dict) -> str:
        """最高評価店舗の紹介文を返す"""
        for s in sorted(self.studios,
                        key=lambda r: safe_float(r.get("rating")) or 0,
                        reverse=True):
            name = str(s.get("studio_title", ""))
            d = desc_map.get(name, "")
            if d:
                return sanitize_desc(d)
        return ""


def build_studio_groups(rows: pd.DataFrame) -> list[StudioGroup]:
    """DataFrame の行をブランドグルーピングして StudioGroup リストを返す"""
    groups: dict[str, StudioGroup] = {}

    for _, row in rows.iterrows():
        gkey = get_group_key(str(row["studio_title"]))
        if gkey not in groups:
            groups[gkey] = StudioGroup(group_key=gkey, group_name=gkey)

        g = groups[gkey]

        # 同一住所の重複スキップ（グループ内）
        row_addr = _norm_addr(row.get("address", ""))
        if row_addr and len(row_addr) > 5:
            existing_addrs = {_norm_addr(s.get("address", "")) for s in g.studios}
            if row_addr in existing_addrs:
                continue

        g.studios.append(row)

        # 最高評価
        r  = safe_float(row.get("rating"))
        rc = safe_int(row.get("review_count"))
        if r is not None and (g.best_rating is None or r > g.best_rating):
            g.best_rating       = r
            g.best_review_count = rc

        # 提携
        dslurl = str(row.get("dslurl", "")).strip()
        if pd.notna(row.get("dslurl")) and dslurl:
            g.is_partner = True
            g.dslurl_map[str(row["studio_title"])] = dslurl

        # 公式サイト（最初に見つかったもの）
        if not g.website:
            ws = str(row.get("Webサイト", ""))
            if pd.notna(row.get("Webサイト")) and ws.startswith("http"):
                g.website = ws

        # 住所（最初に見つかったもの）
        if not g.address:
            addr = str(row.get("address", ""))
            if pd.notna(row.get("address")) and addr:
                g.address = addr

        # レンタル適合スコア（グループ内最大）
        fit = calc_rental_fit_score(row)
        g.rental_fit_score = max(g.rental_fit_score, fit)

        # 施設タイプ（最初の1件で確定、またはダンス主用途が後から見つかれば上書き）
        utype = classify_usage_type(row)
        if g.usage_type == USAGE_TYPE_UNKNOWN or utype == USAGE_TYPE_DANCE:
            g.usage_type = utype

    # Post-process: 各グループの代表駅（最短距離の店舗）を選択
    for g in groups.values():
        best_d = None
        for s in g.studios:
            d  = safe_float(s.get("distance_to_primary_station_m"))
            st = str(s.get("primary_station", "")).strip()
            if st and st != "なし" and d is not None:
                if best_d is None or d < best_d:
                    best_d             = d
                    g.primary_station  = st
                    g.primary_dist_min = dist_to_minutes(d)

    return list(groups.values())


def score_group(g: StudioGroup, p_bonus: int) -> tuple[float, list[str]]:
    score   = 0.0
    reasons = []

    if g.is_partner:
        score += 100
        reasons.append("提携スタジオ")

    score += p_bonus
    score += g.rental_fit_score * 2
    reasons.append(f"rental_fit={g.rental_fit_score}")

    if g.best_rating is not None:
        pts = g.best_rating / 5.0 * 20
        score += pts
        reasons.append(f"★{g.best_rating:.1f}(+{pts:.0f}pt)")

    if g.best_review_count:
        pts = min(g.best_review_count, 300) / 300 * 15
        score += pts
        reasons.append(f"口コミ{g.best_review_count}件(+{pts:.0f}pt)")

    # 最近接店舗の距離
    min_dist = None
    for s in g.studios:
        d = safe_float(s.get("distance_to_primary_station_m"))
        if d is not None and (min_dist is None or d < min_dist):
            min_dist = d
    if min_dist is not None:
        if min_dist <= 500:
            score += 15; reasons.append("駅近500m以内")
        elif min_dist <= 1000:
            score += 8;  reasons.append("駅近1000m以内")

    return round(score, 2), reasons


def select_groups(
    groups:  list[StudioGroup],
    p_bonus: int,
) -> tuple[list[StudioGroup], int]:
    """スコアリング・選定。返り値: (選定リスト, 除外数)"""
    removed   = 0
    eligible  = []

    for g in groups:
        # 提携スタジオは閾値に関わらず常に含める
        if g.is_partner or g.rental_fit_score >= RENTAL_FIT_THRESHOLD:
            eligible.append(g)
        else:
            removed += 1
            log.debug(f"  除外(fit={g.rental_fit_score}): {g.group_key}")

    for g in eligible:
        g.selection_score, g.selection_reason = score_group(g, p_bonus)

    partners     = sorted([g for g in eligible if g.is_partner],
                          key=lambda x: x.selection_score, reverse=True)
    non_partners = sorted([g for g in eligible if not g.is_partner],
                          key=lambda x: x.selection_score, reverse=True)

    n_p  = min(len(partners), MAX_PARTNERS)
    n_np = min(len(non_partners), MAX_GROUPS - n_p)

    selected = partners[:n_p] + non_partners[:n_np]
    selected.sort(key=lambda x: x.selection_score, reverse=True)
    return selected[:MAX_GROUPS], removed


# ============================================================
# 提携スタジオ直接注入
# ============================================================
def inject_missing_partners(
    rows:             pd.DataFrame,
    article_title:    str,
    article_type:     str,
    article_priority: str,
    all_partners:     pd.DataFrame,
    api_map:          dict,
) -> tuple[pd.DataFrame, int]:
    """
    article_top15_final.csv に入っていない提携スタジオを
    エリア/駅マッチングで候補に追加する。
    駅・距離が空欄の提携スタジオも拾えるよう、名前とprimary_area/candidate_areasで判定。
    """
    area = extract_area_or_station(article_title, article_type)
    if not area:
        return rows, 0

    existing_names     = set(rows["studio_title"].astype(str).unique())
    existing_addr_norms = {_norm_addr(a) for a in rows["address"].astype(str).unique() if _norm_addr(a)}
    new_rows = []

    for _, p in all_partners.iterrows():
        name = str(p.get("タイトル", ""))
        if name in existing_names:
            continue
        # 同住所の既存エントリがある場合はスキップ（別名の同一スタジオの二重注入を防ぐ）
        p_addr_norm = _norm_addr(str(p.get("住所", "") or ""))
        if p_addr_norm and p_addr_norm in existing_addr_norms:
            continue

        p_area     = str(p.get("primary_area", "") or "")
        c_areas    = [s.strip() for s in str(p.get("candidate_areas", "") or "").split("|") if s.strip()]
        p_station  = str(p.get("primary_station", "") or "")
        c_stations = [s.strip() for s in str(p.get("candidate_stations", "") or "").split("|") if s.strip()]

        if article_type == "エリア":
            matches = (
                area == p_area
                or area in c_areas
                or area in name
            )
        elif article_type == "駅":
            kw = area.rstrip("駅")  # "新宿駅" → "新宿"
            matches = (
                kw in p_station
                or any(kw in s for s in c_stations)
                or area in name
                or kw in name
            )
        else:
            matches = area in name

        if not matches:
            continue

        api  = api_map.get(name, {})
        dist = p.get("distance_to_primary_station_m")
        new_rows.append({
            "article_title":                 article_title,
            "article_type":                  article_type,
            "article_priority":              article_priority,
            "rank":                          0,
            "studio_title":                  name,
            "address":                       str(p.get("住所", "")) if pd.notna(p.get("住所")) else "",
            "is_partner":                    1,
            "dslurl":                        str(p.get("dslurl", "")).strip(),
            "primary_station":               p_station if p_station not in ("", "なし") else "",
            "distance_to_primary_station_m": dist if pd.notna(dist) else None,
            "place_id":                      api.get("place_id"),
            "rating":                        api.get("rating"),
            "review_count":                  api.get("review_count"),
            "api_status":                    "OK" if api else "",
            "selection_score":               0,
            "selection_reason":              "提携スタジオ直接追加",
        })

    if new_rows:
        injected = pd.DataFrame(new_rows)
        rows = pd.concat([rows, injected], ignore_index=True)

    return rows, len(new_rows)


# ============================================================
# CSS
# ============================================================
CSS = """<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Noto+Sans+JP:wght@400;700&display=swap" rel="stylesheet">
<style>
  :root {
    --pink: #ee93b9;
    --pink-dark: #d4709d;
    --pink-light: #fdf2f7;
    --pink-border: #f5c6de;
    --text: #333;
    --text-sub: #666;
    --border: #e8e8e8;
    --bg-th: #fdf2f7;
  }
  * { box-sizing: border-box; }
  body {
    font-family: "Noto Sans JP","Hiragino Kaku Gothic ProN",sans-serif;
    margin: 0; padding: 0;
    color: var(--text); background: #fff; line-height: 1.7;
  }
  /* ---- レイアウト ---- */
  .page-wrapper {
    max-width: 1200px; margin: 0 auto; padding: 1em 1.5em;
  }
  .page-layout {
    display: grid;
    grid-template-columns: 1fr 280px;
    gap: 2em;
    align-items: start;
  }
  .main-content { min-width: 0; }
  .sidebar { position: sticky; top: 1.5em; align-self: start; max-height: calc(100vh - 3em); overflow-y: auto; }
  /* サイドバー: ウィジェット共通 */
  .sidebar-widget {
    background: #fff; border: 1px solid var(--border);
    border-radius: 10px; padding: 1.2em; margin-bottom: 1.5em;
  }
  /* サイドバー: 掲載CTA */
  .sidebar-cta {
    background: var(--pink-light); border-color: var(--pink-border);
    text-align: center;
  }
  .sidebar-cta-title {
    font-weight: 700; color: #222; font-size: .95em; margin: 0 0 .9em;
    line-height: 1.5;
  }
  .sidebar-cta-btn {
    display: block; background: var(--pink); color: #fff !important;
    padding: .8em 1em; border-radius: 24px;
    text-decoration: none; font-weight: 700; font-size: .9em;
    text-align: center; transition: background .2s;
    box-shadow: 0 2px 6px rgba(238,147,185,.35);
  }
  .sidebar-cta-btn:hover { background: var(--pink-dark); color: #fff !important; }
  /* サイドバー: 関連記事 */
  .sidebar-related h3 {
    margin: 0 0 .7em; font-size: .95em; font-weight: 700; color: #222;
    border-left: 3px solid var(--pink); padding-left: .5em;
  }
  .sidebar-related ul { margin: 0; padding: 0; list-style: none; }
  .sidebar-related li {
    font-size: .85em; border-bottom: 1px solid var(--border);
    padding: .45em 0;
  }
  .sidebar-related li:last-child { border-bottom: none; }
  .sidebar-related a { color: var(--text); text-decoration: none; }
  .sidebar-related a:hover { color: var(--pink-dark); text-decoration: underline; }
  /* レスポンシブ: タブレット */
  @media (max-width: 1024px) {
    .page-layout { grid-template-columns: 1fr 240px; gap: 1.5em; }
    .page-wrapper { padding: 1em; }
  }
  /* レスポンシブ: モバイル */
  @media (max-width: 768px) {
    .page-wrapper { padding: .75em .9em; }
    .page-layout { grid-template-columns: 1fr; }
    .main-content { order: 0; }
    .sidebar { position: static; order: 1; max-height: none; overflow-y: visible; }
    h1 { font-size: 1.35em; }
    h2 { font-size: 1.1em; }
    .studio-card { padding: 1em; margin: 1.2em 0; }
    .studio-info th { width: 72px; font-size: .85em; }
    .studio-info td { font-size: .9em; }
    .branch-name { min-width: 0; width: 100%; }
    .branch-list li { flex-direction: column; gap: .1em; padding: .5em .7em; }
    .branch-access, .branch-rating { flex: none; }
    .dsl-btn { display: block; text-align: center; margin: .35em 0; }
    .area-table { font-size: .85em; }
    .toc { padding: .8em 1em; }
    .info-section { padding: .9em 1em; }
    .article-summary { padding: 1em; }
  }
  /* サイドバーなし (generate_articles.py 直接生成) */
  .page-wrapper--single { max-width: 900px; }
  a { color: var(--pink-dark); }
  a:hover { color: var(--pink); }
  h1 {
    font-size: 1.75em; font-weight: 700;
    border-bottom: 3px solid var(--pink);
    padding-bottom: .5em; margin-bottom: .8em; color: #222;
  }
  h2 {
    font-size: 1.25em; font-weight: 700; margin-top: 2em; color: #222;
    border-left: 4px solid var(--pink); padding-left: .6em;
  }
  h3 { font-size: 1.05em; font-weight: 700; margin: 1.2em 0 .5em; color: #333; }
  /* 目次 */
  .toc {
    background: var(--pink-light); border: 1px solid var(--pink-border);
    border-radius: 8px; padding: 1em 1.5em; margin: 1.2em 0;
  }
  .toc strong { color: var(--pink-dark); }
  .toc > ol { margin: .5em 0; padding-left: .5em; list-style: none; }
  .toc li { margin: .35em 0; font-size: .95em; }
  .toc a { color: var(--text); text-decoration: none; }
  .toc a:hover { color: var(--pink-dark); text-decoration: underline; }
  .toc .sub-note { list-style: none; padding-left: 1em; margin: .15em 0; }
  .toc .sub-note li { font-size: .88em; color: var(--text-sub); margin: .1em 0; }
  /* 比較表 */
  .comparison-wrap { overflow-x: auto; margin: 1.5em 0; }
  .comparison-table {
    width: 100%; border-collapse: collapse; font-size: .88em; min-width: 640px;
  }
  .comparison-table th {
    background: var(--pink-light); color: var(--pink-dark);
    padding: .5em .6em; border: 1px solid var(--pink-border);
    text-align: left; white-space: nowrap;
  }
  .comparison-table td {
    padding: .45em .6em; border: 1px solid var(--border); vertical-align: middle;
  }
  .comparison-table tr:hover td { background: #fafafa; }
  .ct-ok { color: #2a9d4e; font-weight: 700; }
  /* スタジオカード */
  .studio-card {
    background: #fff; border: 1px solid var(--border);
    border-radius: 10px; padding: 1.4em 1.5em; margin: 1.8em 0;
    box-shadow: 0 2px 8px rgba(238,147,185,.08);
  }
  .studio-card h2 { margin-top: 0; }
  .studio-rating { font-size: .95em; color: var(--text-sub); margin: .3em 0 .8em; }
  .studio-info { width: 100%; border-collapse: collapse; margin-bottom: .9em; font-size: .92em; }
  .studio-info th {
    width: 90px; background: var(--bg-th); color: var(--pink-dark);
    padding: .45em .7em; text-align: left; font-weight: 700;
    border-bottom: 1px solid var(--pink-border);
  }
  .studio-info td { padding: .45em .7em; border-bottom: 1px solid var(--border); }
  .studio-desc { line-height: 1.9; margin: .7em 0; }
  /* 複数店舗リスト */
  .branch-list {
    margin: .9em 0 .5em; padding: 0; list-style: none;
    border: 1px solid var(--pink-border); border-radius: 8px; overflow: hidden;
    font-size: .92em;
  }
  .branch-list li {
    display: flex; flex-wrap: wrap; gap: .3em .7em;
    align-items: baseline; padding: .55em .9em;
    border-bottom: 1px solid var(--border);
  }
  .branch-list li:last-child { border-bottom: none; }
  .branch-name { font-weight: 700; min-width: 13em; }
  .branch-access { color: var(--text-sub); flex: 1; }
  .branch-rating { font-size: .83em; color: #888; white-space: nowrap; }
  /* DSLボタン */
  .dsl-booking { margin-top: 1em; }
  .dsl-btn {
    display: inline-block; background: var(--pink); color: #fff;
    padding: .6em 1.4em; border-radius: 24px;
    text-decoration: none; font-weight: 700; font-size: .9em;
    transition: background .2s; margin: .3em .4em .3em 0;
  }
  .dsl-btn:hover { background: var(--pink-dark); color: #fff; }
  /* 情報セクション */
  .info-section {
    background: #fafafa; border: 1px solid var(--border);
    border-left: 4px solid var(--pink);
    border-radius: 0 8px 8px 0; padding: 1.1em 1.3em; margin: 2em 0;
  }
  .info-section h2 { margin-top: 0; }
  .info-section ul { margin: .4em 0; padding-left: 1.4em; }
  .info-section li { margin: .45em 0; }
  /* エリア比較テーブル */
  .area-table-wrap { overflow-x: auto; }
  .area-table { width: 100%; border-collapse: collapse; font-size: .93em; margin-top: .8em; min-width: 400px; }
  .area-table th {
    background: var(--pink-light); color: var(--pink-dark);
    padding: .5em .8em; border: 1px solid var(--pink-border); text-align: left;
  }
  .area-table td { padding: .55em .8em; border: 1px solid var(--border); vertical-align: top; line-height: 1.65; }
  /* まとめ */
  .article-summary {
    background: var(--pink-light); border: 1px solid var(--pink-border);
    border-radius: 10px; padding: 1.4em 1.5em; margin-top: 2.5em;
  }
  .article-summary h2 { margin-top: 0; }
  .dsl-search-link { margin-top: 1em; }
  .dsl-search-link a {
    display: inline-block; background: var(--pink-light);
    border: 1px solid var(--pink-border); border-radius: 24px;
    padding: .55em 1.4em; color: var(--pink-dark); font-weight: 700;
    text-decoration: none; font-size: .95em; transition: background .2s;
  }
  .dsl-search-link a:hover { background: var(--pink-border); }
  /* FAQ */
  .faq-section { margin-top: 2.5em; }
  .faq-item { border: 1px solid var(--border); border-radius: 8px; margin: 1em 0; overflow: hidden; }
  .faq-q {
    background: var(--pink-light); color: #222;
    font-weight: 700; padding: .8em 1em; font-size: .96em;
  }
  .faq-q::before { content: "Q. "; color: var(--pink-dark); }
  .faq-a { padding: .85em 1em; font-size: .95em; line-height: 1.85; }
  .faq-a::before { content: "A. "; font-weight: 700; color: var(--pink-dark); }
  /* 編集方針 */
  .editorial-note {
    margin-top: 3em; padding: 1.2em 1.5em;
    background: #f9f9f9; border: 1px solid var(--border); border-radius: 8px;
    font-size: .9em; color: var(--text-sub);
  }
  .editorial-note h2 { font-size: 1em; margin-top: 0; color: var(--text); }
  .editorial-note ul { margin: .5em 0 0; padding-left: 1.4em; }
  .editorial-note li { margin: .3em 0; }
  /* 更新日 */
  .updated { font-size: .85em; color: var(--text-sub); margin: .5em 0 1.2em; }
  /* 施設タイプバッジ */
  .usage-badge {
    display: inline-block; font-size: .78em; font-weight: 700;
    padding: .15em .6em; border-radius: 3px; margin: 0 0 .5em;
    letter-spacing: .02em;
  }
  .usage-ダンス主用途     { background: #e8f5e9; color: #2e7d32; border: 1px solid #a5d6a7; }
  .usage-多目的レンタル   { background: #e3f2fd; color: #1565c0; border: 1px solid #90caf9; }
  .usage-撮影兼用         { background: #fff3e0; color: #e65100; border: 1px solid #ffcc80; }
  .usage-ピラティス-ヨガ兼用 { background: #f3e5f5; color: #6a1b9a; border: 1px solid #ce93d8; }
  .usage-音楽-防音兼用    { background: #fce4ec; color: #880e4f; border: 1px solid #f48fb1; }
  .usage-要確認           { background: #f5f5f5; color: #616161; border: 1px solid #e0e0e0; }
  /* 著者ボックス */
  .author-box {
    display: flex; gap: 1em; align-items: flex-start;
    background: var(--pink-light); border: 1px solid var(--pink-border);
    border-radius: 10px; padding: 1.1em 1.3em; margin: 1em 0 1.5em;
  }
  .author-avatar { font-size: 2.2em; flex-shrink: 0; line-height: 1; }
  .author-name { margin: 0 0 .35em; font-size: 1em; }
  .author-bio { margin: 0; font-size: .88em; color: var(--text); line-height: 1.75; }
  @media (max-width: 600px) {
    .author-box { flex-direction: column; gap: .6em; }
  }
  /* スタジオ画像 */
  .studio-images {
    display: flex; gap: .5em; flex-wrap: wrap; margin: .75em 0;
  }
  .studio-images img {
    width: calc(50% - .25em); max-width: 320px;
    height: 185px; object-fit: cover;
    border-radius: 6px; border: 1px solid var(--border);
    background: #f0f0f0;
  }
  @media (max-width: 600px) {
    .studio-images img { width: 100%; max-width: none; height: 200px; }
  }
  /* だんコメント */
  .dan-comment {
    margin: .6em 0;
    padding: .55em .9em;
    background: var(--pink-light);
    border-left: 3px solid var(--pink);
    border-radius: 0 6px 6px 0;
    font-size: .88em; color: #444; line-height: 1.75;
  }
  /* まとめ署名 */
  .dan-sign { font-style: italic; color: var(--text-sub); text-align: right; margin-top: .8em; font-size: .92em; }
  /* ヒーロー画像 */
  .hero-image { margin: .8em 0 1.4em; border-radius: 10px; overflow: hidden; }
  .hero-image img {
    width: 100%; max-height: 420px; object-fit: cover;
    display: block; border-radius: 10px;
    border: 1px solid var(--border);
  }
  /* パンくずリスト */
  .breadcrumb { font-size: .82em; color: var(--text-sub); margin: 0 0 .8em; }
  .breadcrumb ol { list-style: none; padding: 0; margin: 0; display: flex; flex-wrap: wrap; gap: .15em .1em; }
  .breadcrumb li + li::before { content: "›"; margin: 0 .3em; color: #bbb; }
  .breadcrumb a { color: var(--text-sub); text-decoration: none; }
  .breadcrumb a:hover { text-decoration: underline; }
  /* ランキング根拠タグ */
  .ranking-note { margin: .35em 0 .6em; display: flex; flex-wrap: wrap; gap: .3em; }
  .rank-tag { font-size: .74em; padding: .15em .55em; border-radius: 3px; font-weight: 700; letter-spacing: .02em; }
  .rank-partner { background: #fff3e0; color: #e65100; border: 1px solid #ffcc80; }
  .rank-rating  { background: #fff8e1; color: #f57f17; border: 1px solid #ffe082; }
  .rank-review  { background: #e8f5e9; color: #2e7d32; border: 1px solid #a5d6a7; }
  .rank-access  { background: #e3f2fd; color: #1565c0; border: 1px solid #90caf9; }
</style>"""


# ============================================================
# HTML パーツ生成
# ============================================================

# ── だん著者ボックス ──────────────────────────────────────
def make_author_box() -> str:
    return (
        '\n<div class="author-box">\n'
        '  <div class="author-avatar">📸</div>\n'
        '  <div class="author-content">\n'
        '    <p class="author-name"><strong>だん</strong>（Dance Cover Lab・28歳・都内在住）</p>\n'
        '    <p class="author-bio">ダンス専門カメラマンとして都内を中心に活動しています。'
        '撮影現場でスタジオ選びの相談をよく受けるようになり、このサイトにまとめることにしました📸 '
        '鏡・床材・光の入り方など、カメラマン目線での情報もお届けします！</p>\n'
        '  </div>\n'
        '</div>\n'
    )


# ── スタジオ画像 ──────────────────────────────────────────
def make_studio_images_html(
    images: list[tuple[str, str]],
    studio_name: str,
    output_dir: Optional[Path] = None,
) -> str:
    """(uuid, filename) リストからスタジオ画像 HTML を生成する。
    output_dir を渡すと articles/room-images/{uuid}/ に画像をコピーし相対 URL を使用。
    """
    if not images:
        return ""
    imgs = []
    for uuid, filename in images:
        if output_dir is not None:
            # 画像を articles/room-images/{uuid}/ にコピー
            src = ROOM_IMAGES_DIR / uuid / "main_images" / filename
            dst_dir = output_dir / "room-images" / uuid
            dst = dst_dir / filename
            if not dst.exists() and src.exists():
                dst_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
            url = f"room-images/{uuid}/{filename}"
        else:
            url = f"{DSL_IMAGE_BASE}/{uuid}/main_images/{filename}"
        alt = f"{studio_name}のダンスルーム"
        imgs.append(
            f'<img src="{url}" alt="{alt}" loading="lazy" width="400" height="300">'
        )
    return (
        '\n  <div class="studio-images">\n    '
        + "\n    ".join(imgs)
        + "\n  </div>"
    )


def make_hero_image_html(
    groups: "list[StudioGroup]",
    output_dir: Optional[Path] = None,
) -> tuple[str, str]:
    """記事の最初の提携スタジオ画像をヒーロー画像として返す。
    Returns: (hero_html, absolute_url_for_ogp)
    """
    for g in groups:
        if not g.is_partner or not g.dslurl_map:
            continue
        for url in g.dslurl_map.values():
            images = get_studio_images(url, max_images=1)
            if not images:
                continue
            uuid, filename = images[0]
            abs_url = f"{DSL_IMAGE_BASE}/{uuid}/main_images/{filename}"
            if output_dir is not None:
                src = ROOM_IMAGES_DIR / uuid / "main_images" / filename
                dst_dir = output_dir / "room-images" / uuid
                dst = dst_dir / filename
                if not dst.exists() and src.exists():
                    dst_dir.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
                local_url = f"room-images/{uuid}/{filename}"
            else:
                local_url = abs_url
            hero_html = (
                f'\n<div class="hero-image">\n'
                f'  <img src="{local_url}" alt="{g.group_name}のダンスルーム" '
                f'loading="eager" width="800" height="450">\n'
                f'</div>\n'
            )
            return hero_html, abs_url
    return "", ""


# ── だんのパーソナルコメント ──────────────────────────────

def _make_dan_partner_comment_from_rooms(rooms: pd.DataFrame, group_key: str) -> str:
    """ルームスペックからだんのカメラマン視点コメントを生成（提携スタジオ専用）"""
    import hashlib
    _h = int(hashlib.md5(group_key.encode()).hexdigest(), 16)

    # 設備フラグ収集
    has_shoot    = rooms['room_shoot_flg'].eq('t').any()
    has_light    = (rooms['room_free_lighting_flg'].eq('t').any()
                    or rooms['room_paid_lighting_flg'].eq('t').any())
    has_window   = rooms['room_window_flg'].eq('t').any()
    has_high_ceil = (rooms['room_ceiling'].dropna().astype(int) > 0).any()
    has_cam_tripod = rooms['room_free_camera_tripod_flg'].eq('t').any()
    has_phone_tripod = rooms['room_free_smartphone_tripod_flg'].eq('t').any()
    has_bamiri   = rooms['room_bamiri_flg'].eq('t').any()
    has_bt       = rooms['room_speaker_bluetooth_flg'].eq('t').any()
    has_wired    = rooms['room_speaker_wired_flg'].eq('t').any()

    sizes = rooms['room_square_meters'].dropna().tolist()
    max_size = max(sizes) if sizes else 0

    mirror_vals = rooms['room_mirror'].dropna().astype(int)
    max_mirror  = int(mirror_vals.max()) if len(mirror_vals) > 0 else 0

    floor_vals  = rooms['room_floor_quality'].dropna().astype(int)
    has_lino    = (floor_vals == 0).any()

    # 優先度順にコメント決定
    if has_shoot and has_light:
        opts = [
            "撮影向けルームに照明機材まで揃っています📸 動画クオリティにこだわりたい人には特におすすめですよ。",
            "照明機材付きの撮影向けルームは贅沢📷 ライティングを工夫するだけで映像の質がぐっと上がります。",
        ]
    elif has_shoot and has_window:
        opts = [
            "撮影向けで自然光も入るルームがあります📸 昼間の撮影なら窓からの光が映像をきれいに見せてくれますよ。",
            "自然光×撮影向けルームは動画に向いた条件が揃ってます📷 時間帯を選んで使うと映像が変わりますね。",
        ]
    elif has_shoot and has_cam_tripod:
        opts = [
            "撮影向けルームにカメラ三脚まで無料で借りられます📸 機材を持ち込まなくても本格的な撮影ができますね。",
            "三脚無料レンタル付きの撮影向けルーム📷 荷物を減らして撮影に集中できるのは嬉しい。",
        ]
    elif has_high_ceil:
        opts = [
            "天井高5m以上のルームがあります📸 縦のフレームでも余裕があって、ジャンプや跳び系の動きも映えますよ。",
            "天井が高いスタジオは動画映えしやすい📷 アクロバット系の動きもフレームに収まりやすいですね。",
        ]
    elif has_shoot and max_size >= 50:
        sz = int(max_size)
        opts = [
            f"撮影向けで最大{sz}㎡の広さがあります📸 引いたカメラポジションが取りやすくてフレームに余裕ができますよ。",
            f"広さ{sz}㎡の撮影向けルーム📷 カメラを引いて全身フレームで撮れるのは動画制作で助かります。",
        ]
    elif has_shoot:
        opts = [
            "撮影向けルームがあります📸 照明や背景の環境もルームページで確認してから使うといいですよ。",
            "撮影を想定したルーム設計になってます📷 床の反射や鏡の映り込みも事前にチェックしてみてください。",
        ]
    elif has_light:
        opts = [
            "照明機材が借りられるスタジオです📸 ライティングを少し工夫するだけで動画クオリティが上がりますよ！",
            "照明レンタルあり。機材を持ち込まなくても映像のクオリティを上げられるのは助かります📷",
        ]
    elif has_window:
        opts = [
            "自然光が入る窓があります📸 昼間の練習・撮影なら窓からの光を活かした映像が撮れそうですね。",
            "窓からの自然光は動画を柔らかくきれいに見せてくれます📷 時間帯を合わせて使うと映像が変わりますよ。",
        ]
    elif max_size >= 60 and max_mirror >= 2:
        sz = int(max_size)
        opts = [
            f"最大{sz}㎡で鏡が複数面あります📸 フォーメーション確認しながら全体映像も撮りやすい環境ですね。",
            f"広さ{sz}㎡×複数ミラーは練習にも撮影にも使いやすい条件📷 振付の全体像をチェックするのに向いてます。",
        ]
    elif max_size >= 50:
        sz = int(max_size)
        opts = [
            f"最大{sz}㎡のスペースは動画に余白が出てフレームが作りやすいです📸 グループ練習にも余裕で対応できますね。",
            f"広さ{sz}㎡あると撮影時にカメラを引いたポジションが取りやすい📷 余裕のあるフレーミングができます。",
        ]
    elif has_lino:
        opts = [
            "リノリウム床はスライドやターンがしやすくて動画でも映えやすい床材です📸 練習・撮影どちらにも向いてますよ。",
            "リノリウム床は動きが映えやすいのでカメラマン目線でもおすすめできます📷 足元の質感が映像に出やすいですね。",
        ]
    elif max_mirror >= 2:
        opts = [
            "鏡が複数面あると自分の動きを多角度でチェックできます📸 振付の細部確認や撮影アングルの参考にも使えますね。",
            "2面以上の鏡はフォーム確認に使いやすい📷 いろんな角度から自分の動きを見られるのは練習効率が上がります。",
        ]
    elif has_bamiri:
        opts = [
            "バミリテープが借りられます📸 フォーメーション練習や撮影の立ち位置確認をしたいグループ向けですね。",
            "バミリテープ無料貸出あり。隊形の確認がしやすくてグループ練習の効率が上がりますよ📷",
        ]
    elif has_phone_tripod:
        opts = [
            "スマホ三脚が無料で借りられます📸 セルフ撮影や練習動画のチェックをしたいときに便利ですよ。",
            "スマホ三脚の無料レンタルあり📷 機材を持ち込まなくてもセルフ撮影できるのは使いやすいですね。",
        ]
    elif has_bt or has_wired:
        spk = "Bluetooth" if has_bt else "有線"
        opts = [
            f"{spk}スピーカー完備のルームがあります📸 音楽をしっかり鳴らして練習・撮影できる環境ですね。",
            f"{spk}スピーカーが使えます📷 音源の再生環境が整っているのは練習にも動画制作にも助かります。",
        ]
    else:
        opts = [
            "設備の詳細はDSLのルームページで確認できます📸 鏡・床材・広さを事前にチェックしてから予約を。",
            "ルームページで広さ・床材・設備をしっかり確認してから予約するといいですよ📷",
        ]

    return opts[_h % len(opts)]


_DAN_PARTNER_COMMENTS = [
    "ダンススタジオラボで予約できます📸 空き状況がオンラインで確認できて助かります！",
    "DSL提携スタジオです✨ 事前に空き状況をオンラインで確認できるのは便利ですよね！",
    "オンライン予約ができるDSL提携スタジオです📷 直前でも空き状況が確認できます！",
    "DSL提携なのでオンライン予約OK！📸 撮影案件でも使いやすいスタジオです。",
]
_DAN_USAGE_COMMENTS: dict[str, list[str]] = {
    "ダンス主用途": [
        "ダンス専用スタジオなので鏡と床は期待できそう📸 撮影視点だと光の入り方も確認したいですね！",
        "ダンス向けに設計されたスタジオです✨ 鏡の大きさと照明環境を事前に確認してから予約を！",
        "練習用途にしっかり対応しているスタジオですね。振付確認にも使いやすそう📷",
    ],
    "撮影兼用": [
        "撮影兼用のスタジオは私も重宝してます📸 照明環境と背景が気になるので要チェックです！",
        "ダンス動画撮影にも向きそうなスペース📷 光の入り方と床の反射も確認できるといいですね！",
    ],
    "多目的レンタル": [
        "多目的スタジオなので、ダンス用途での使い勝手は事前確認が大事🔍 鏡と床材をチェックしてみてください！",
        "設備が充実してそうですね📸 ダンス・撮影利用の可否は公式サイトで確認してから予約を！",
    ],
    "ピラティス・ヨガ兼用": [
        "主用途は違いますが、軽い振付確認やストレッチなら使える場合もありますよ📷 事前確認推奨です！",
        "ヨガ・ピラティス系ですが、ダンス利用できる場合も。問い合わせてみる価値ありです💡",
    ],
    "音楽・防音兼用": [
        "防音しっかりしてそうなので通し練習には向きそう🎵 撮影も音出しを気にせずできそうですね！",
        "音楽スタジオ系ですが、音を出しながら練習できるのは魅力✨ ダンス利用可否を確認して！",
    ],
    "要確認": [
        "詳細が気になりますね👀 公式サイトで鏡・床材・広さをチェックしてから判断してみてください！",
        "設備の確認が先決ですね🔍 撮影・ダンス練習に向いてるか公式サイトでチェックを！",
    ],
}


def make_dan_comment(g: "StudioGroup", dsl_data: Optional[dict] = None) -> str:
    """だんのパーソナルコメント（提携状況・usage_type に基づく）"""
    import hashlib
    _h = int(hashlib.md5(g.group_key.encode()).hexdigest(), 16)
    if g.is_partner:
        # ルームデータがあればスペックベースのコメントを生成
        if dsl_data and g.dslurl_map:
            for title, dslurl in g.dslurl_map.items():
                info = _find_dsl_studio(title, dslurl, dsl_data)
                if info and info.get('rooms') is not None and len(info['rooms']) > 0:
                    return _make_dan_partner_comment_from_rooms(info['rooms'], g.group_key)
        return _DAN_PARTNER_COMMENTS[_h % len(_DAN_PARTNER_COMMENTS)]
    tpls = _DAN_USAGE_COMMENTS.get(g.usage_type, _DAN_USAGE_COMMENTS["要確認"])
    return tpls[_h % len(tpls)]


def make_page_title(area_or_station: str, article_title: str, count: int = 0) -> str:
    area = area_or_station or ""
    if area:
        if count >= 10:
            return f"{area}のレンタルダンススタジオおすすめ｜駅近・個人練習・グループ練習向け"
        elif count >= 5:
            return f"{area}周辺のレンタルスタジオ候補｜近隣エリアも含む"
        elif count >= 2:
            return f"{area}周辺でダンス練習に使えるレンタルスタジオ候補"
        else:
            return f"{area}のレンタルダンススタジオ｜周辺エリア情報"
    return f"{article_title}｜レンタルダンススタジオ"


def make_h1(area_or_station: str, article_title: str, count: int = 0) -> str:
    """titleタグと主要KWを一致させる"""
    area = area_or_station or ""
    if area:
        if count >= 10:
            return f"{area}のレンタルダンススタジオおすすめ"
        elif count >= 5:
            return f"{area}周辺のレンタルスタジオ候補（近隣エリアも含む）"
        elif count >= 2:
            return f"{area}周辺でダンス練習に使えるレンタルスタジオ候補"
        else:
            return f"{area}のレンタルダンススタジオ情報"
    return article_title


def make_meta_description(area_or_station: str, count: int = 0) -> str:
    area  = area_or_station or "東京"
    cnt   = f"{count}か所" if count else "複数"
    return (
        f"{area}周辺のレンタルダンススタジオ{cnt}を、駅からの距離・Google口コミ評価・"
        f"設備を比較してご紹介。個人練習・グループ練習・振付確認など用途別に選びやすく整理しています。"
    )


def make_intro(area_or_station: str, article_type: str, count: int,
               distant_stations: list[str] | None = None) -> str:
    area = area_or_station or "このエリア"
    distant_stations = distant_stations or []

    # 掲載数に応じた注記
    if count <= 4:
        count_note = (
            f"<p>※ {area}周辺は候補スタジオが少なめなので、"
            f"近隣エリア・駅のスタジオも含めて紹介しています🙏</p>\n"
        )
    else:
        count_note = ""

    # エリア外スタジオの説明
    if distant_stations:
        distant_str = "・".join(distant_stations)
        distant_note = (
            f"<p>（{distant_str}など、{area}から少し離れた駅のスタジオも含まれています。）</p>\n"
        )
    else:
        distant_note = ""

    if article_type == "エリア":
        return (
            f"<p>{area}周辺でダンス練習場所を探している方へ。"
            f"同じエリアでも最寄り駅によって使いやすさがかなり変わるので、"
            f"駅からの距離・Google口コミ評価・設備をもとにまとめました📸</p>\n"
            f"<p>個人練習・グループ練習・振付確認など、目的に合わせて比較しやすいよう整理しています。"
            f"提携スタジオはダンススタジオラボ（DSL）からオンライン予約もできます💪</p>\n"
            + count_note
            + distant_note
        )
    elif article_type == "駅":
        return (
            f"<p>{area}周辺のレンタルダンススタジオを、駅からの距離・口コミ・設備でまとめました📸</p>\n"
            f"<p>個人練習・グループ練習・振付確認など用途に合わせて比較できます。"
            f"提携スタジオはDSLからオンライン予約OK。"
            f"空き状況や設備の詳細は各スタジオの公式ページでご確認ください💪</p>\n"
            + count_note
            + distant_note
        )
    else:
        return (
            f"<p>条件に合ったレンタルダンススタジオをまとめました🎬</p>\n"
            f"<p>口コミ評価や駅からの距離も掲載しているので、比較の参考にしてください。</p>\n"
            + count_note
        )


def make_breadcrumb_html(page_title: str) -> str:
    """視覚的パンくずリストHTML"""
    return (
        '\n<nav class="breadcrumb" aria-label="パンくずリスト">\n'
        '  <ol>\n'
        '    <li><a href="https://dance-studio-lab.com/">ホーム</a></li>\n'
        '    <li><a href="https://dance-studio-lab.com/studio/">スタジオ一覧</a></li>\n'
        f'    <li>{page_title}</li>\n'
        '  </ol>\n'
        '</nav>\n'
    )


def make_ranking_note(g: "StudioGroup") -> str:
    """スタジオカードのランキング選定根拠タグHTML"""
    tags = []
    for r in g.selection_reason:
        if r == "提携スタジオ":
            tags.append('<span class="rank-tag rank-partner">DSL提携</span>')
        elif r.startswith("★"):
            star = r.split("(")[0]
            tags.append(f'<span class="rank-tag rank-rating">Google{star}</span>')
        elif r.startswith("口コミ") and "件" in r:
            m = re.search(r"口コミ(\d+)件", r)
            if m:
                tags.append(f'<span class="rank-tag rank-review">口コミ{m.group(1)}件</span>')
        elif "駅近" in r:
            tags.append('<span class="rank-tag rank-access">駅近</span>')
    if not tags:
        return ""
    return f'  <div class="ranking-note">{"".join(tags)}</div>\n'


def make_toc(groups: list[StudioGroup], area_or_station: str) -> str:
    area = area_or_station or "このエリア"
    items = []
    for i, g in enumerate(groups, 1):
        if len(g.studios) > 1:
            store_names = "　".join(
                str(s.get("studio_title", "")) for s in g.studios[:4]
            )
            if len(g.studios) > 4:
                store_names += f"　他{len(g.studios)-4}店舗"
            items.append(
                f'    <li><a href="#group-{i}">{i}. {g.group_name}</a>\n'
                f'      <ul class="sub-note"><li>{store_names}</li></ul></li>'
            )
        else:
            items.append(f'    <li><a href="#group-{i}">{i}. {g.group_name}</a></li>')

    items += [
        f'    <li><a href="#points">{area}でスタジオを選ぶポイント</a></li>',
        '    <li><a href="#areas">エリア・駅別の特徴</a></li>',
        '    <li><a href="#equipment">個人練習・グループ練習で確認すべき設備</a></li>',
        '    <li><a href="#summary">まとめ</a></li>',
        '    <li><a href="#faq">よくある質問</a></li>',
    ]
    return (
        '\n<nav class="toc">\n  <strong>目次</strong>\n  <ol>\n'
        + "\n".join(items)
        + "\n  </ol>\n</nav>"
    )


def make_comparison_table(groups: list[StudioGroup]) -> str:
    rows = []
    for i, g in enumerate(groups, 1):
        rating_txt, rc_txt = fmt_rating_text(g.best_rating, g.best_review_count)
        dist  = g.primary_dist_min or "—"
        st    = g.primary_station or "—"
        utype = g.usage_type

        individual = '<span class="ct-ok">○</span>'
        group_ok   = g.is_partner or g.rental_fit_score >= 7 or len(g.studios) > 1
        group_cell = '<span class="ct-ok">○</span>' if group_ok else "公式サイトで確認"

        if g.is_partner and g.dslurl_map:
            first_url = next(iter(g.dslurl_map.values()))
            link = f'<a href="{first_url}" target="_blank" rel="noopener">空き状況</a>'
        elif g.website:
            link = f'<a href="{g.website}" target="_blank" rel="noopener">公式サイト</a>'
        else:
            link = f'<a href="#group-{i}">詳細</a>'

        rows.append(
            f"  <tr>\n"
            f"    <td><a href=\"#group-{i}\">{g.group_name}</a></td>\n"
            f"    <td>{st}</td><td>{dist}</td>\n"
            f"    <td>{rating_txt}</td><td>{rc_txt}</td>\n"
            f"    <td>{utype}</td>\n"
            f"    <td>{individual}</td><td>{group_cell}</td>\n"
            f"    <td>{link}</td>\n"
            f"  </tr>"
        )

    return (
        '\n<section>\n  <h2>スタジオ比較表</h2>\n'
        '  <div class="comparison-wrap">\n'
        '  <table class="comparison-table">\n'
        '    <thead><tr>\n'
        '      <th>スタジオ名</th><th>最寄り駅</th><th>徒歩</th>\n'
        '      <th>Google評価</th><th>口コミ数</th>\n'
        '      <th>施設タイプ</th><th>個人練習</th><th>グループ練習</th><th>詳細</th>\n'
        '    </tr></thead>\n    <tbody>\n'
        + "\n".join(rows)
        + "\n    </tbody>\n  </table>\n  </div>\n</section>"
    )


def _make_cta_single(g: StudioGroup) -> str:
    """単一店舗 or 代表店舗への DSL ボタン"""
    if not g.is_partner or not g.dslurl_map:
        return ""
    dslurl = next(iter(g.dslurl_map.values()))
    return (
        '\n  <div class="dsl-booking">\n'
        f'    <a href="{dslurl}" target="_blank" rel="noopener" class="dsl-btn">'
        f'空き状況を確認する</a>\n  </div>'
    )


def _make_cta_multi(g: StudioGroup) -> str:
    """複数店舗それぞれへの DSL ボタン"""
    if not g.is_partner or not g.dslurl_map:
        return ""
    btns = []
    for title, url in g.dslurl_map.items():
        # グループ名をプレフィックスとして除去してラベルを短縮
        label = title.replace(g.group_name, "").strip(" 　・-")
        label = label or title
        btns.append(
            f'    <a href="{url}" target="_blank" rel="noopener" class="dsl-btn">'
            f'{label}の空き状況を確認する</a>'
        )
    return (
        '\n  <div class="dsl-booking">\n'
        + "\n".join(btns)
        + "\n  </div>"
    )


def _make_cta_website(g: StudioGroup) -> str:
    """非提携スタジオ向け公式サイトボタン"""
    if g.is_partner or not g.website:
        return ""
    return (
        '\n  <div class="dsl-booking">\n'
        f'    <a href="{g.website}" target="_blank" rel="noopener" class="dsl-btn website-btn">'
        '公式サイトを見る</a>\n  </div>'
    )


MIN_DESC_LEN = 100


def _review_count_sentence(rc) -> str:
    """口コミ件数に応じた補足文（グローバル共通）"""
    if rc is None:
        return ""
    if rc >= 10:
        return f"口コミ{rc}件の評価が参考になります。"
    if rc >= 3:
        return "少数ながら利用者の声も確認でき、公式情報とあわせて検討しやすい候補です。"
    if rc >= 1:
        return "口コミはまだ少ないため、設備や予約条件もあわせて確認しておくと安心です。"
    return ""


_DANCE_BODY_TEMPLATES = [
    ("ダンス練習向けのレンタルスタジオで、"
     "鏡を使いながら振りを確認できる環境が整っています。"
     "床材はダンス練習に使いやすい素材を採用しており、"
     "個人練習からグループ練習まで幅広く利用できます。"),
    ("ダンスに特化したレンタルスタジオで、"
     "鏡での動作確認がしやすい環境が整っています。"
     "自主練習や仲間との合わせ練習など、さまざまな用途に対応しています。"),
    ("ダンス利用を想定したレンタルスタジオです。"
     "鏡・床材ともに練習用途に対応しており、"
     "空き時間を使ってコンパクトに練習できる場所です。"),
    ("ダンス練習に使えるレンタルスタジオで、"
     "鏡で振付を確認しながら集中して練習できます。"
     "予約状況はカレンダーや公式サイトで確認してください。"),
]

_MULTI_BODY_TEMPLATES = [
    ("ダンスにも対応するレンタルスタジオです。"
     "床材や広さはダンスの内容によって適否が変わるため、"
     "予約前に確認しておくとよいでしょう。"),
    ("多目的のレンタルスタジオで、ダンス練習にも利用できます。"
     "鏡・床材の仕様は公式サイトまたは問い合わせで確認しておくと安心です。"),
    ("レンタルスタジオとして運営されており、ダンス練習での利用実績もあります。"
     "床材や広さ、利用ルールについては事前に確認することをおすすめします。"),
    ("多用途型のレンタルスタジオで、ダンスやエクササイズにも対応しています。"
     "ダンスの用途に合うかどうかは、鏡の有無や床材を確認してから判断しましょう。"),
]

_PHOTO_BODY_TEMPLATES = [
    ("撮影向けのレンタルスペースとして利用されていますが、"
     "ダンス動画の撮影や振付確認を兼ねた練習にも向く候補です。"
     "鏡の有無や床材は事前に確認してください。"),
    ("主に撮影用途で使われるスペースです。"
     "ダンス動画撮影や振付の録画確認などを目的とした利用に向く場合があります。"
     "床材・鏡・利用ルールを事前に確認しておくと安心です。"),
]

_PILATES_BODY_TEMPLATES = [
    ("ピラティスやヨガを主用途とするスタジオで、"
     "少人数での振付確認や軽い自主練習に向く可能性があります。"
     "大きな移動を伴う練習や激しいダンスの場合は、"
     "床材・広さ・利用ルールを事前に確認してください。"),
    ("ヨガやピラティスを中心とした施設ですが、"
     "床材が整っているため軽いストレッチや振付確認などにも使える場合があります。"
     "激しいダンス利用の可否については公式サイトまたは問い合わせで確認してください。"),
]

_MUSIC_BODY_TEMPLATES = [
    ("音楽・防音用途を中心とするスタジオで、"
     "音出しを伴う通し練習や動画撮影の場にも向く候補です。"
     "鏡の有無や動ける広さは事前に確認しておくと安心です。"),
    ("防音性の高い音楽スタジオで、音を気にせず通し練習できる環境があります。"
     "ダンス練習での利用を検討する場合は、鏡・床・利用ルールを確認してください。"),
]

_UNKNOWN_BODY_TEMPLATES = [
    ("施設詳細は公式サイトをご確認ください。"
     "床材・鏡の有無・広さなど、ダンス練習の用途に合うかどうか"
     "事前に確認しておくと安心です。"),
    ("詳細な設備情報は公式サイトに掲載されています。"
     "ダンス利用の可否・床材・鏡の有無を確認してから予約しましょう。"),
]

_USAGE_TEMPLATES: dict[str, list[str]] = {
    USAGE_TYPE_DANCE:   _DANCE_BODY_TEMPLATES,
    USAGE_TYPE_MULTI:   _MULTI_BODY_TEMPLATES,
    USAGE_TYPE_PHOTO:   _PHOTO_BODY_TEMPLATES,
    USAGE_TYPE_PILATES: _PILATES_BODY_TEMPLATES,
    USAGE_TYPE_MUSIC:   _MUSIC_BODY_TEMPLATES,
    USAGE_TYPE_UNKNOWN: _UNKNOWN_BODY_TEMPLATES,
}


def _fallback_desc(g: StudioGroup, article_type: str, area_or_station: str) -> str:
    """紹介文がない場合のフォールバック（usage_type別・禁止フレーズなし・バリエーションあり）"""
    import hashlib
    _h = int(hashlib.md5(g.group_key.encode()).hexdigest(), 16)

    parts = []

    # ── アクセス文 ──
    if g.primary_station and g.primary_dist_min:
        access_tpls = [
            f"{g.primary_station}から{g.primary_dist_min}に位置するスタジオです。",
            f"最寄りの{g.primary_station}から{g.primary_dist_min}でアクセスできます。",
            f"{g.primary_station}{g.primary_dist_min}に構えるスタジオです。",
        ]
        parts.append(access_tpls[_h % len(access_tpls)])
    elif area_or_station:
        area_tpls = [
            f"{area_or_station}エリアにあるスタジオです。",
            f"{area_or_station}エリアで利用できるスタジオです。",
        ]
        parts.append(area_tpls[_h % len(area_tpls)])

    # ── usage_type 別本文（バリエーションあり） ──
    tpls = _USAGE_TEMPLATES.get(g.usage_type, _UNKNOWN_BODY_TEMPLATES)
    parts.append(tpls[_h % len(tpls)])

    # ── 口コミ文 ──
    rc_sent = _review_count_sentence(g.best_review_count)
    if rc_sent:
        parts.append(rc_sent)

    # ── 多店舗補足 ──
    if len(g.studios) > 1:
        multi_tpls = [
            f"エリア内に{len(g.studios)}店舗を展開しており、集合場所や日程に合わせて使い分けられます。",
            f"{len(g.studios)}店舗を運営しており、その日の予定に合わせて選べます。",
        ]
        parts.append(multi_tpls[_h % len(multi_tpls)])

    return "".join(parts)


_ENSURE_MIN_SUPPLEMENTS: dict[str, list[str]] = {
    USAGE_TYPE_DANCE: [
        "空き状況や設備の詳細は公式サイトでご確認ください。",
        "設備や予約条件は公式サイトで確認しておくと安心です。",
    ],
    USAGE_TYPE_MULTI: [
        "ダンス利用の適否は、床材・鏡・広さを事前に公式サイトで確認してください。",
        "ダンス練習に使えるかどうかは、鏡・床材・広さを確認してから判断しましょう。",
    ],
    USAGE_TYPE_PHOTO: [
        "ダンス動画撮影での利用を検討する場合は、鏡・床・スペースを事前に確認してください。",
        "撮影目的での利用が中心ですが、ダンス動画撮影にも向くか事前に確認しましょう。",
    ],
    USAGE_TYPE_PILATES: [
        "激しいダンスへの対応状況は、公式サイトまたは問い合わせで確認してください。",
        "ヨガ・ピラティス向けの施設なので、ダンス利用の可否は事前確認が安心です。",
    ],
    USAGE_TYPE_MUSIC: [
        "ダンス利用の可否・床材・鏡の有無は事前に確認しておくと安心です。",
        "音楽スタジオですが、ダンス練習での利用を検討する場合は鏡と床材を確認してください。",
    ],
    USAGE_TYPE_UNKNOWN: [
        "ダンス利用の適否は、床材・鏡・広さを事前に公式サイトで確認してください。",
        "設備の詳細は公式サイトで確認しておくと安心です。",
    ],
}


def _ensure_min_desc(desc: str, g: "StudioGroup") -> str:
    """説明文が MIN_DESC_LEN 未満の場合に usage_type ベースの補足を追加する（バリエーションあり）"""
    if len(desc) >= MIN_DESC_LEN:
        return desc
    import hashlib
    _h = int(hashlib.md5(g.group_key.encode()).hexdigest(), 16)
    if not desc.endswith("。"):
        desc += "。"
    tpls = _ENSURE_MIN_SUPPLEMENTS.get(g.usage_type, _ENSURE_MIN_SUPPLEMENTS[USAGE_TYPE_UNKNOWN])
    desc += tpls[_h % len(tpls)]
    return desc


# 感嘆符に変換しやすいキーワード（ポジティブ・感情的な語）
_EXCLAIM_KW = [
    "魅力", "おすすめ", "最適", "便利", "快適", "テンション", "集中",
    "本格的", "充実", "信頼", "好評", "人気", "リピート", "安心",
    "通いやすい", "使い分け", "気軽", "通し練習", "スムーズ",
]


def _format_desc_html(desc: str) -> str:
    """説明文を HTML 用に整形する（改行挿入・感嘆符付与）"""
    sentences = [s for s in desc.split("。") if s.strip()]
    if not sentences:
        return desc

    # 感嘆符候補: キーワードを含む文を最初の1つだけ選ぶ（末尾の文は除く）
    exclaim_idx = None
    for i, s in enumerate(sentences[:-1]):
        if any(kw in s for kw in _EXCLAIM_KW):
            exclaim_idx = i
            break

    # 各文に句点 or 感嘆符を付ける
    marked = []
    for i, s in enumerate(sentences):
        marked.append(s + ("！" if i == exclaim_idx else "。"))

    # 2文ごとに <br> で改行
    lines = []
    for i in range(0, len(marked), 2):
        lines.append("".join(marked[i:i + 2]))
    return "<br>".join(lines)


def make_studio_card(
    rank: int, g: StudioGroup,
    desc_map: dict, article_type: str, area_or_station: str,
    output_dir: Optional[Path] = None,
    dsl_data: Optional[dict] = None,
) -> str:
    desc = g.best_desc(desc_map) or _fallback_desc(g, article_type, area_or_station)
    desc = _ensure_min_desc(desc, g)
    desc = _format_desc_html(desc)

    if len(g.studios) == 1:
        # ── 単一店舗カード ──
        s       = g.studios[0]
        address = str(s.get("address", "")) if pd.notna(s.get("address")) else g.address
        website = str(s.get("Webサイト", "")) if pd.notna(s.get("Webサイト")) else g.website
        st      = g.primary_station
        dm      = g.primary_dist_min

        map_url = ("https://www.google.com/maps/search/?api=1&query="
                   + urllib.parse.quote(address)) if address else ""
        map_lnk = f' <a href="{map_url}" target="_blank" rel="noopener">地図</a>' if map_url else ""

        phone   = str(s.get("電話番号", "")) if pd.notna(s.get("電話番号")) else ""
        hours_raw = s.get("営業時間", "")
        hours_str = parse_business_hours(hours_raw)

        info = ""
        if address:
            info += f'      <tr><th>住所</th><td>{address}{map_lnk}</td></tr>\n'
        if st and dm:
            info += f'      <tr><th>アクセス</th><td>{st} {dm}</td></tr>\n'
        elif st:
            info += f'      <tr><th>アクセス</th><td>{st}</td></tr>\n'
        if hours_str:
            info += f'      <tr><th>営業時間</th><td>{hours_str}</td></tr>\n'
        if phone:
            info += f'      <tr><th>電話番号</th><td><a href="tel:{phone}">{phone}</a></td></tr>\n'
        if website:
            info += f'      <tr><th>公式サイト</th><td><a href="{website}" target="_blank" rel="noopener">公式サイト</a></td></tr>\n'

        rating_html = fmt_rating_html(g.best_rating, g.best_review_count)
        cta = _make_cta_single(g) or _make_cta_website(g)

        # usage_type バッジ
        ut_cls  = g.usage_type.replace("・", "-").replace("　", "-").replace(" ", "-")
        ut_badge = f'<span class="usage-badge usage-{ut_cls}">{g.usage_type}</span>\n'

        # 画像（提携スタジオのみ・複数ルーム対応で最大3枚収集）
        images = []
        if g.is_partner and g.dslurl_map:
            for _url in list(g.dslurl_map.values())[:3]:
                images.extend(get_studio_images(_url, max_images=2))
                if len(images) >= 3:
                    break
            images = images[:3]
        images_html = make_studio_images_html(images, g.group_name, output_dir=output_dir)

        # だんコメント
        dan_text = make_dan_comment(g, dsl_data=dsl_data)
        yaji_html = f'\n  <p class="dan-comment">📸 {dan_text}</p>'

        # ランキング根拠タグ
        rank_html = make_ranking_note(g)

        return (
            f'\n<section class="studio-card" id="group-{rank}">\n'
            f'  <h2>{rank}. {g.group_name}</h2>\n'
            f'{ut_badge}'
            f'{rank_html}'
            f'{rating_html}\n'
            f'{images_html}\n'
            f'  <table class="studio-info"><tbody>\n{info}  </tbody></table>\n'
            f'  <p class="studio-desc">{desc}</p>\n'
            f'{yaji_html}\n'
            f'{cta}\n</section>'
        )

    else:
        # ── 複数店舗グループカード ──
        branches = []
        for s in g.studios:
            sname  = str(s.get("studio_title", ""))
            s_st   = str(s.get("primary_station", "")).strip()
            s_dm   = dist_to_minutes(s.get("distance_to_primary_station_m"))
            access = f"{s_st} {s_dm}" if s_st and s_st != "なし" and s_dm else (s_st or "—")

            addr   = str(s.get("address", "")) if pd.notna(s.get("address")) else ""
            map_url = ("https://www.google.com/maps/search/?api=1&query="
                       + urllib.parse.quote(addr)) if addr else ""
            map_lnk = f'<a href="{map_url}" target="_blank" rel="noopener">地図</a> ' if map_url else ""

            dsl    = g.dslurl_map.get(sname, "")
            ws     = str(s.get("Webサイト", "")) if pd.notna(s.get("Webサイト")) else ""
            if dsl:
                site_lnk = f'<a href="{dsl}" target="_blank" rel="noopener">空き状況を確認する</a>'
            elif ws:
                site_lnk = f'<a href="{ws}" target="_blank" rel="noopener">公式サイトを見る</a>'
            else:
                site_lnk = ""

            r  = safe_float(s.get("rating"))
            rc = safe_int(s.get("review_count"))
            if r is not None:
                badge = f'<span class="branch-rating">Google評価 {r:.1f}{"（"+str(rc)+"件）" if rc else ""}</span>'
            else:
                badge = ""

            branches.append(
                f'    <li>\n'
                f'      <span class="branch-name">{sname}</span>\n'
                f'      <span class="branch-access">{access}　{map_lnk}{site_lnk}</span>\n'
                f'      {badge}\n'
                f'    </li>'
            )

        branch_html = "\n".join(branches)
        cta         = _make_cta_multi(g)
        if not cta and g.website:
            # 非提携の複数店舗グループ: 共通サイトをボタン化
            site_line = (
                f'\n  <div class="dsl-booking">\n'
                f'    <a href="{g.website}" target="_blank" rel="noopener" class="dsl-btn website-btn">'
                f'公式サイトを見る</a>\n  </div>'
            )
        else:
            site_line = ""

        # usage_type バッジ
        ut_cls   = g.usage_type.replace("・", "-").replace("　", "-").replace(" ", "-")
        ut_badge = f'  <span class="usage-badge usage-{ut_cls}">{g.usage_type}</span>\n'

        # だんコメント
        dan_text = make_dan_comment(g, dsl_data=dsl_data)
        yaji_html = f'\n  <p class="dan-comment">📸 {dan_text}</p>'

        # ランキング根拠タグ
        rank_html = make_ranking_note(g)

        return (
            f'\n<section class="studio-card" id="group-{rank}">\n'
            f'  <h2>{rank}. {g.group_name}（{len(g.studios)}店舗）</h2>\n'
            f'{ut_badge}'
            f'{rank_html}'
            f'  <p class="studio-desc">{desc}</p>\n'
            f'  <ul class="branch-list">\n{branch_html}\n  </ul>\n'
            f'{site_line}\n'
            f'{yaji_html}\n'
            f'{cta}\n</section>'
        )


def make_selection_points_section(area_or_station: str) -> str:
    area = area_or_station or "このエリア"
    return (
        f'\n<section class="info-section" id="points">\n'
        f'  <h2>{area}でレンタルダンススタジオを選ぶポイント</h2>\n'
        f'  <ul>\n'
        f'    <li><strong>駅からの距離：</strong>練習後の移動も考えると、駅から徒歩5分以内が使いやすいです。最寄り駅ごとにスタジオ数が変わるため、集合しやすい駅を基準に選ぶのがおすすめです。</li>\n'
        f'    <li><strong>床材の確認：</strong>ダンス練習にはフローリングやリノリウム床が向いています。カーペット床はターンやスライドに不向きな場合があるため、予約前に確認しておきましょう。</li>\n'
        f'    <li><strong>鏡の有無・サイズ：</strong>振付確認には全身が映る大きな鏡が便利です。鏡の枚数や配置は公式サイトの写真で事前に確認できることが多いです。</li>\n'
        f'    <li><strong>部屋の広さ：</strong>個人練習なら10〜20㎡程度、グループ練習には30㎡以上が目安です。人数に合わせた広さを確認しておきましょう。</li>\n'
        f'    <li><strong>音響設備：</strong>Bluetooth対応スピーカーや音楽再生環境が整っているか確認しましょう。持ち込み可否についても確認しておくと安心です。</li>\n'
        f'    <li><strong>予約のしやすさ：</strong>オンライン予約・直前予約への対応、キャンセルポリシーも事前に確認しておくと利用しやすくなります。</li>\n'
        f'  </ul>\n</section>'
    )


def make_area_section(area_or_station: str, groups: list[StudioGroup]) -> str:
    # 駅ごとにグループを集約
    station_groups: dict[str, list[StudioGroup]] = {}
    for g in groups:
        st = g.primary_station or ""
        if not st:
            continue
        station_groups.setdefault(st, []).append(g)

    if not station_groups:
        return ""

    rows = []
    for st, st_groups in sorted(station_groups.items(), key=lambda x: -len(x[1])):
        group_count = len(st_groups)  # 掲載グループ数（チェーンは1グループ）
        studio_count = sum(len(g.studios) for g in st_groups)  # 実店舗数

        # チェーン店（同グループに複数店舗）の数
        chain_groups = [g for g in st_groups if len(g.studios) > 1]
        chain_count = len(chain_groups)

        # 提携スタジオ数
        partner_count = sum(1 for g in st_groups if g.is_partner)

        # 駅距離（m）の最小値・平均値（データがあるもののみ）
        dists = []
        for g in st_groups:
            for s in g.studios:
                try:
                    d = float(s.get("distance_to_primary_station_m") or "")
                    dists.append(d)
                except (TypeError, ValueError):
                    pass

        # Google評価（データがあるグループのみ）
        ratings = [g.best_rating for g in st_groups if g.best_rating is not None]
        avg_rating = sum(ratings) / len(ratings) if ratings else None

        # --- 特徴テキスト生成 ---
        parts = []

        # スタジオ数
        if group_count >= 4:
            parts.append(f"掲載{group_count}スタジオと選択肢が豊富")
        elif group_count >= 2:
            parts.append(f"掲載{group_count}スタジオ")
        else:
            parts.append("掲載1スタジオ")

        # チェーン vs 個人
        if chain_count > 0 and chain_count == group_count:
            parts.append("チェーン系が中心")
        elif chain_count > 0:
            parts.append(f"チェーン{chain_count}・個人{group_count - chain_count}の混在")
        else:
            parts.append("個人・独立系スタジオのみ")

        # 駅距離
        if dists:
            min_dist = min(dists)
            avg_dist = sum(dists) / len(dists)
            if min_dist <= 300:
                parts.append(f"最短{int(min_dist)}mと駅直近")
            elif avg_dist <= 500:
                parts.append(f"平均{int(avg_dist)}mと駅近")
            elif avg_dist <= 1000:
                parts.append(f"平均{int(avg_dist)}mほど")
            else:
                parts.append(f"平均{int(avg_dist)}mとやや距離あり")

        # Google評価
        if avg_rating is not None:
            if avg_rating >= 4.5:
                parts.append(f"平均★{avg_rating:.1f}と高評価")
            elif avg_rating >= 4.0:
                parts.append(f"平均★{avg_rating:.1f}")
            else:
                parts.append(f"平均★{avg_rating:.1f}")

        # 提携
        if partner_count > 0:
            parts.append(f"DSL提携{partner_count}スタジオあり")

        desc = "。".join(parts) + "。"

        rows.append(
            f"  <tr><td><strong>{st}周辺</strong></td>"
            f"<td>{desc}</td></tr>"
        )

    return (
        '\n<section class="info-section" id="areas">\n'
        '  <h2>エリア・駅別の特徴</h2>\n'
        '  <div class="area-table-wrap">\n'
        '  <table class="area-table">\n'
        '    <thead><tr><th>エリア（駅）</th><th>特徴</th></tr></thead>\n'
        '    <tbody>\n'
        + "\n".join(rows)
        + "\n    </tbody>\n  </table>\n  </div>\n</section>"
    )


def make_sidebar_html(article_title: str, title_type_map: dict[str, str],
                      article_type: str = "") -> str:
    """サイドバーHTML（掲載CTA + エリア別/駅別/その他の関連記事）を生成する"""
    # 汎用ワードを除いたキーワード抽出（関連度スコアリング用）
    GENERIC = {"レンタル", "ダンス", "スタジオ", "おすすめ", "使える", "東京", "ある"}
    kws = {w for w in re.findall(r'[\u4e00-\u9fff\u30a0-\u30ff]{2,}', article_title)
           if w not in GENERIC}

    def rel_score(t: str):
        return (-sum(1 for k in kws if k in t), t)

    # タイプ別に分類（現在の記事は除外）
    by_type: dict[str, list[str]] = {"エリア": [], "駅": [], "条件": []}
    for t, tp in title_type_map.items():
        if t == article_title:
            continue
        by_type.setdefault(tp, []).append(t)

    # 各グループを関連度順にソートして上位N件
    # 駅別記事なら「駅別」を先頭、それ以外は「エリア別」を先頭
    MAX_PER_GROUP = 6
    if article_type == "駅":
        sections = [
            ("駅別",     by_type.get("駅", [])),
            ("エリア別", by_type.get("エリア", [])),
            ("その他",   by_type.get("条件", [])),
        ]
    else:
        sections = [
            ("エリア別", by_type.get("エリア", [])),
            ("駅別",     by_type.get("駅", [])),
            ("その他",   by_type.get("条件", [])),
        ]

    widgets = []
    for heading, titles in sections:
        if not titles:
            continue
        top = sorted(titles, key=rel_score)[:MAX_PER_GROUP]
        items = "\n".join(
            f'    <li><a href="{slugify(t)}.html">{t}</a></li>'
            for t in top
        )
        widgets.append(
            f'<div class="sidebar-widget sidebar-related">\n'
            f'  <h3>{heading}</h3>\n'
            f'  <ul>\n{items}\n  </ul>\n'
            f'</div>'
        )

    return (
        '<div class="sidebar-widget sidebar-cta">\n'
        '  <p class="sidebar-cta-title">スタジオ掲載・集客のご相談</p>\n'
        '  <a href="https://dance-studio-lab.com/studio-registration/lp/"'
        ' target="_blank" rel="noopener sponsored" class="sidebar-cta-btn">'
        '掲載について問い合わせる</a>\n'
        '</div>\n'
        + "\n".join(widgets)
    )


def make_equipment_section() -> str:
    return (
        '\n<section class="info-section" id="equipment">\n'
        '  <h2>個人練習・グループ練習で確認すべき設備</h2>\n'
        '  <h3>個人練習の場合</h3>\n'
        '  <ul>\n'
        '    <li>全身が映る鏡（フォームや振付を確認できるサイズ）</li>\n'
        '    <li>音楽再生環境（スピーカー・Bluetooth接続など）</li>\n'
        '    <li>1時間単位で予約できる柔軟な料金体系</li>\n'
        '    <li>荷物を置けるスペースや更衣室の有無</li>\n'
        '  </ul>\n'
        '  <h3>グループ練習の場合</h3>\n'
        '  <ul>\n'
        '    <li>4〜10人が動ける広さ（30㎡以上が目安）</li>\n'
        '    <li>フォーメーション確認しやすい鏡の配置</li>\n'
        '    <li>スピーカーの音量が十分であること</li>\n'
        '    <li>複数時間まとめて予約できる料金プランの有無</li>\n'
        '    <li>着替えスペース・荷物置き場の確保</li>\n'
        '  </ul>\n</section>'
    )


def make_faq(area_or_station: str) -> tuple[str, list[dict]]:
    area = area_or_station or "このエリア"
    faqs = [
        {
            "q": f"{area}でレンタルダンススタジオを選ぶポイントは？",
            "a": (f"駅からの距離、床材（フローリング・リノリウムなど）、鏡の有無と大きさ、"
                  f"部屋の広さ、音楽再生設備を確認するのが基本です。"
                  f"練習人数や内容に合わせて選ぶと、実際に使ったときのミスマッチを減らせます。"),
        },
        {
            "q": "個人練習でも1人でレンタルできますか？",
            "a": ("多くのレンタルダンススタジオは1人からの利用に対応しています。"
                  "ただし最低利用時間や最低料金が設定されている場合があるため、"
                  "予約前に各スタジオの条件をご確認ください。"),
        },
        {
            "q": "グループ練習で確認しておくべき設備は？",
            "a": ("部屋の広さ（4〜10人が動ける30㎡以上が目安）、鏡の配置、"
                  "スピーカーの音量、着替えスペースの有無を確認しておくと安心です。"
                  "人数上限が設定されているスタジオもあるため、事前に確認しましょう。"),
        },
        {
            "q": "深夜や早朝に使えるスタジオはありますか？",
            "a": ("24時間・深夜対応のスタジオは限られています。"
                  "各スタジオの営業時間を事前に確認するか、「深夜可」などの条件で"
                  "絞り込んで探すのがおすすめです。"),
        },
        {
            "q": "予約前に確認しておくべきことは？",
            "a": ("①空き状況・料金体系、②キャンセルポリシー、"
                  "③持ち込み機材の可否（スピーカー・三脚など）、"
                  "④シャワーや更衣室の有無、⑤複数人で使う場合の人数上限、"
                  "の5点を確認しておくと安心です。"),
        },
    ]

    items = []
    for f in faqs:
        items.append(
            f'  <div class="faq-item">\n'
            f'    <div class="faq-q">{f["q"]}</div>\n'
            f'    <div class="faq-a">{f["a"]}</div>\n'
            f'  </div>'
        )

    html = (
        '\n<section class="faq-section" id="faq">\n'
        '  <h2>よくある質問</h2>\n'
        + "\n".join(items)
        + "\n</section>"
    )
    return html, faqs


def make_summary(area_or_station: str, groups: list[StudioGroup]) -> str:
    area       = area_or_station or "このエリア"
    top_names  = "、".join(g.group_name for g in groups[:3])
    search_url = (DSL_SITE_URL + "/search?free="
                  + urllib.parse.quote(area))

    return (
        '\n<div id="summary">\n'
        '<section class="article-summary">\n'
        '  <h2>まとめ</h2>\n'
        f'  <p>{area}エリアには、{top_names}などのレンタルダンススタジオがあります。'
        f'いずれも駅から近い立地にあり、個人練習・グループ練習・振付確認など幅広い用途に使えます。</p>\n'
        f'  <p>気になるスタジオがあれば、空き状況や設備条件を確認しながら練習スタイルに合う場所を選んでみてください💪'
        f' 鏡の有無・床の種類・部屋の広さなど、用途に合わせた条件で比較するのが選びやすいですよ！</p>\n'
        f'  <p class="dan-sign">— だん（Dance Cover Lab）📸</p>\n'
        f'  <div class="dsl-search-link">\n'
        f'    <a href="{search_url}" target="_blank" rel="noopener">'
        f'{area}周辺のレンタルダンススタジオを検索する</a>\n'
        f'  </div>\n'
        f'</section>\n</div>'
    )


def make_editorial_note() -> str:
    return (
        '\n<section class="editorial-note" aria-label="編集方針">\n'
        '  <h2>この記事について</h2>\n'
        '  <p>この記事は<strong>だん</strong>（Dance Cover Lab・ダンス専門カメラマン・28歳）が執筆・監修しています。'
        '撮影現場でのスタジオ選びの経験をもとに、鏡・床材・光環境などカメラマン目線の情報をシェアしています📸</p>\n'
        '  <ul>\n'
        '    <li>掲載スタジオはGoogle口コミ評価・駅からの距離・ダンス利用実績をもとに選定しています。</li>\n'
        '    <li>営業時間・電話番号は公式情報をもとに記載していますが変更されることがあります。最新情報は公式サイトをご確認ください。</li>\n'
        '    <li>DSL提携スタジオはオンライン即時予約が可能です。</li>\n'
        '  </ul>\n'
        '</section>'
    )


def make_faq_jsonld(faqs: list[dict]) -> str:
    entities = [
        {
            "@type": "Question",
            "name": f["q"],
            "acceptedAnswer": {"@type": "Answer", "text": f["a"]},
        }
        for f in faqs
    ]
    data = {
        "@context": "https://schema.org",
        "@type": "FAQPage",
        "mainEntity": entities,
    }
    return (
        '<script type="application/ld+json">\n'
        + json.dumps(data, ensure_ascii=False, indent=2)
        + "\n</script>"
    )


def make_article_jsonld(page_title: str, canonical_url: str, area_or_station: str) -> str:
    from datetime import date
    today = date.today().isoformat()
    data = {
        "@context": "https://schema.org",
        "@type": "Article",
        "headline": page_title,
        "datePublished": today,
        "dateModified": today,
        "author": {
            "@type": "Person",
            "name": "だん",
            "jobTitle": "ダンス専門カメラマン",
            "affiliation": {
                "@type": "Organization",
                "name": "Dance Cover Lab",
            },
        },
        "publisher": {
            "@type": "Organization",
            "name": "Dance Studio Lab",
            "url": "https://dance-studio-lab.com/",
        },
        "about": {
            "@type": "Thing",
            "name": f"{area_or_station} レンタルダンススタジオ",
        },
    }
    if canonical_url:
        data["url"] = canonical_url
    return (
        '<script type="application/ld+json">\n'
        + json.dumps(data, ensure_ascii=False, indent=2)
        + "\n</script>"
    )


def make_breadcrumb_jsonld(area_or_station: str, canonical_url: str) -> str:
    items = [
        {"@type": "ListItem", "position": 1, "name": "ホーム",
         "item": "https://dance-studio-lab.com/"},
        {"@type": "ListItem", "position": 2, "name": "ダンススタジオ一覧",
         "item": "https://dance-studio-lab.com/studio/"},
    ]
    if area_or_station:
        items.append({
            "@type": "ListItem", "position": 3,
            "name": f"{area_or_station}のレンタルダンススタジオ",
            "item": canonical_url or "https://dance-studio-lab.com/",
        })
    data = {
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": items,
    }
    return (
        '<script type="application/ld+json">\n'
        + json.dumps(data, ensure_ascii=False, indent=2)
        + "\n</script>"
    )


def make_itemlist_jsonld(groups: list[StudioGroup], article_title: str) -> str:
    items = []
    for i, g in enumerate(groups, 1):
        url_val = next(iter(g.dslurl_map.values())) if g.dslurl_map else g.website
        item: dict = {
            "@type": "ListItem",
            "position": i,
            "name": g.group_name,
        }
        if url_val:
            item["url"] = url_val
        if g.address:
            item["address"] = g.address
        if g.best_rating is not None:
            item["aggregateRating"] = {
                "@type": "AggregateRating",
                "ratingValue": g.best_rating,
                "ratingCount": g.best_review_count or 1,
            }
        items.append(item)

    data = {
        "@context": "https://schema.org",
        "@type": "ItemList",
        "name": article_title,
        "numberOfItems": len(groups),
        "itemListElement": items,
    }
    return (
        '<script type="application/ld+json">\n'
        + json.dumps(data, ensure_ascii=False, indent=2)
        + "\n</script>"
    )


def make_local_business_jsonld(groups: list[StudioGroup], article_title: str) -> str:
    """各スタジオの SportsActivityLocation + ImageObject schema"""
    list_items = []
    for i, g in enumerate(groups, 1):
        item: dict = {
            "@type": "SportsActivityLocation",
            "name": g.group_name,
        }
        # 住所
        if g.address:
            item["address"] = {
                "@type": "PostalAddress",
                "streetAddress": g.address,
                "addressCountry": "JP",
            }
        # URL（DSL提携 → DSLルームURL優先、次に公式サイト）
        url_val = next(iter(g.dslurl_map.values())) if g.dslurl_map else g.website
        if url_val:
            item["url"] = url_val
        if g.website and g.website != url_val:
            item["sameAs"] = g.website
        # Google評価
        if g.best_rating is not None and g.best_review_count:
            item["aggregateRating"] = {
                "@type": "AggregateRating",
                "ratingValue": round(g.best_rating, 1),
                "ratingCount": g.best_review_count,
            }
        # ImageObject（提携スタジオのみ）
        if g.is_partner and g.dslurl_map:
            for dslurl in g.dslurl_map.values():
                imgs = get_studio_images(dslurl, max_images=1)
                if imgs:
                    uuid, filename = imgs[0]
                    item["photo"] = {
                        "@type": "ImageObject",
                        "contentUrl": f"{DSL_IMAGE_BASE}/{uuid}/main_images/{filename}",
                        "name": f"{g.group_name}のダンスルーム",
                        "description": f"{g.group_name}のレンタルダンススタジオ内観",
                    }
                    break
        list_items.append({"@type": "ListItem", "position": i, "item": item})

    data = {
        "@context": "https://schema.org",
        "@type": "ItemList",
        "name": article_title,
        "itemListElement": list_items,
    }
    return (
        '<script type="application/ld+json">\n'
        + json.dumps(data, ensure_ascii=False, indent=2)
        + "\n</script>"
    )


def build_html(
    page_title: str, meta_desc: str, body_html: str,
    faq_jsonld: str, itemlist_jsonld: str,
    sidebar_html: str = "",
    canonical_slug: str = "",
    area_or_station: str = "",
    ogp_image_url: str = "",
    local_business_jsonld: str = "",
) -> str:
    if sidebar_html:
        main_content = (
            '<div class="page-wrapper">\n'
            '<div class="page-layout">\n'
            '<div class="main-content">\n<article>\n'
            + body_html
            + '\n</article>\n</div>\n'
            '<aside class="sidebar">\n'
            + sidebar_html
            + '\n</aside>\n'
            '</div>\n</div>'
        )
    else:
        main_content = (
            '<div class="page-wrapper page-wrapper--single">\n<article>\n'
            + body_html
            + '\n</article>\n</div>'
        )
    encoded_slug = urllib.parse.quote(canonical_slug, safe="") if canonical_slug else ""
    canonical_url = (CANONICAL_BASE + encoded_slug) if encoded_slug else ""
    canonical_tag = (f'  <link rel="canonical" href="{canonical_url}">\n'
                     if canonical_url else "")
    _ogp_img = ogp_image_url or OGP_IMAGE_URL
    ogp_tags = (
        f'  <meta property="og:type" content="article">\n'
        f'  <meta property="og:title" content="{page_title}">\n'
        f'  <meta property="og:description" content="{meta_desc}">\n'
        + (f'  <meta property="og:url" content="{canonical_url}">\n' if canonical_url else "")
        + f'  <meta property="og:image" content="{_ogp_img}">\n'
        + '  <meta property="og:site_name" content="Dance Studio Lab">\n'
        '  <meta property="og:locale" content="ja_JP">\n'
        '  <meta name="twitter:card" content="summary_large_image">\n'
        f'  <meta name="twitter:title" content="{page_title}">\n'
        f'  <meta name="twitter:description" content="{meta_desc}">\n'
        f'  <meta name="twitter:image" content="{_ogp_img}">\n'
    )
    article_jsonld   = make_article_jsonld(page_title, canonical_url, area_or_station)
    breadcrumb_jsonld = make_breadcrumb_jsonld(area_or_station, canonical_url)
    return (
        '<!DOCTYPE html>\n<html lang="ja">\n<head>\n'
        '  <meta charset="UTF-8">\n'
        '  <meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
        '  <meta name="robots" content="index,follow">\n'
        f'  <title>{page_title}</title>\n'
        f'  <meta name="description" content="{meta_desc}">\n'
        f'{canonical_tag}'
        f'{ogp_tags}'
        f'  {CSS}\n'
        '</head>\n<body>\n'
        + main_content + "\n"
        + faq_jsonld + "\n"
        + itemlist_jsonld + "\n"
        + article_jsonld + "\n"
        + breadcrumb_jsonld + "\n"
        + (local_business_jsonld + "\n" if local_business_jsonld else "")
        + '</body>\n</html>'
    )


# ============================================================
# ログ・CSV出力
# ============================================================
def log_article_stats(
    article_title:   str,
    candidate_count: int,
    all_groups:      list[StudioGroup],
    selected:        list[StudioGroup],
    removed:         int,
) -> dict:
    partner_count    = sum(1 for g in selected if g.is_partner)
    group_count      = len(selected)
    studio_count     = sum(len(g.studios) for g in selected)
    grouped_count    = sum(1 for g in selected if len(g.studios) > 1)
    low_fit          = [g for g in selected if not g.is_partner
                        and g.rental_fit_score < RENTAL_FIT_THRESHOLD]

    warnings = []
    if group_count < MIN_GROUPS:
        warnings.append(f"掲載主体数{group_count}件（推奨{MIN_GROUPS}件以上）")
    if low_fit:
        warnings.append(
            f"rental_fit低スタジオ掲載 {len(low_fit)}件: "
            + ", ".join(g.group_key for g in low_fit)
        )
    if any(g.is_partner for g in all_groups) and partner_count == 0:
        warnings.append("提携スタジオが候補にあるのに1件も選ばれていない")

    log.info(f"[{article_title}]")
    log.info(f"  候補{candidate_count}件 → グループ{len(all_groups)} → "
             f"選定{group_count}主体({studio_count}店舗)  提携:{partner_count}  "
             f"除外:{removed}  グループ化:{grouped_count}ブランド")
    for w in warnings:
        log.warning(f"  ⚠ {w}")

    return {
        "article_title":        article_title,
        "candidate_count":      candidate_count,
        "selected_group_count": group_count,
        "selected_studio_count": studio_count,
        "partner_count":        partner_count,
        "removed_low_fit_count": removed,
        "grouped_brand_count":  grouped_count,
        "warnings":             " | ".join(warnings),
    }


def collect_selected_rows(article_title: str, groups: list[StudioGroup]) -> list[dict]:
    rows = []
    for rank, g in enumerate(groups, 1):
        for s in g.studios:
            rows.append({
                "article_title":    article_title,
                "rank":             rank,
                "group_name":       g.group_name,
                "studio_title":     s.get("studio_title", ""),
                "is_partner":       int(g.is_partner),
                "dslurl":           g.dslurl_map.get(str(s.get("studio_title", "")), ""),
                "google_rating":    g.best_rating,
                "google_review_count": g.best_review_count,
                "rental_fit_score": g.rental_fit_score,
                "selection_score":  g.selection_score,
                "selection_reason": " | ".join(g.selection_reason),
            })
    return rows


# ============================================================
# 品質チェック
# ============================================================
_QUALITY_BANNED = [
    "本格的な音響設備",
    "ダンサーの動きに配慮した素材",
    "ターンやジャンプも安心",
    "最適な環境が整っています",
    "リピーター多数",
]
_QUALITY_CTA_NG = ["詳細・予約", "詳細↓", "公式サイト（共通）"]


def quality_check_article(html: str, title: str) -> None:
    """生成済みHTMLの品質チェック（WARNING ログ出力のみ）"""
    issues = []

    # 禁止フレーズ
    for phrase in _QUALITY_BANNED:
        if phrase in html:
            issues.append(f"禁止フレーズ残留: 「{phrase}」")

    # 同一説明文フレーズの3回以上使い回し検出（<meta>タグ内は除外）
    body_only = re.sub(r'<head>.*?</head>', '', html, flags=re.DOTALL)
    sentences = re.findall(r'[^。！？]{15,}[。！？]', body_only)
    from collections import Counter
    for sent, cnt in Counter(sentences).items():
        if cnt >= 3 and "<" not in sent:
            issues.append(f"説明文フレーズ{cnt}回重複: 「{sent[:30]}…」")

    # NGなCTA文言
    for ng in _QUALITY_CTA_NG:
        if ng in html:
            issues.append(f"CTA文言NG: 「{ng}」")

    if issues:
        log.warning(f"[品質チェック] {title} — {len(issues)}件の問題:")
        for issue in issues:
            log.warning(f"    {issue}")
    else:
        log.info(f"[品質チェック] {title} — 問題なし")


# ============================================================
# 1記事生成
# ============================================================
def generate_one_article(
    article_title: str,
    df_top15:      pd.DataFrame,
    df_studios:    pd.DataFrame,
    output_dir:    Path,
    desc_map:      dict,
    all_partners:  pd.DataFrame,
    api_map:       dict,
    title_type_map: dict[str, str] | None = None,
    dsl_data:      dict | None = None,
) -> tuple[Path, dict, list[dict]]:

    rows = df_top15[df_top15["article_title"] == article_title].copy()
    if rows.empty:
        raise ValueError(f"記事が見つかりません: {article_title}")

    article_type     = rows.iloc[0]["article_type"]
    article_priority = rows.iloc[0]["article_priority"]
    p_bonus          = PRIORITY_BONUS.get(str(article_priority), 0)
    area_or_station  = extract_area_or_station(article_title, article_type)

    # TOP15 に含まれていない提携スタジオをエリア/名前マッチで追加
    rows, injected_count = inject_missing_partners(
        rows, article_title, article_type, article_priority,
        all_partners, api_map,
    )
    if injected_count:
        log.debug(f"  提携スタジオ追加: {injected_count}件")

    # スタジオマスタとマージ（Webサイト・article_tags など）
    studio_cols = ["タイトル", "Webサイト", "電話番号", "営業時間", "article_tags"]
    rows = rows.merge(
        df_studios[studio_cols].rename(columns={"タイトル": "studio_title"}),
        on="studio_title", how="left",
    )

    # グルーピング前プレフィルタ: 個別スコアが低く提携でもない行を除外
    # （ブランドグループの高スコア店舗に引き上げられるのを防ぐ）
    pre_scores = rows.apply(calc_rental_fit_score, axis=1)
    is_partner_col = rows["dslurl"].notna() & (rows["dslurl"].astype(str).str.strip() != "")
    rows = rows[(pre_scores >= RENTAL_FIT_THRESHOLD) | is_partner_col].copy()

    candidate_count  = len(rows)

    # グルーピング → 選定
    all_groups          = build_studio_groups(rows)
    selected, removed   = select_groups(all_groups, p_bonus)

    if not selected:
        raise ValueError(f"選定スタジオが0件: {article_title}")

    # 掲載数が1件以下の場合はスキップ
    if len(selected) <= 1:
        log.warning(f"[SKIP] 掲載数{len(selected)}件のため生成スキップ: {article_title}")
        return None, {}, []

    # ログ・CSV データ収集
    stats         = log_article_stats(
        article_title, candidate_count, all_groups, selected, removed)
    selected_rows = collect_selected_rows(article_title, selected)

    # ── 提携スタジオにDSLルームデータ由来の説明文を付与 ──────────────────
    # 口コミ説明文（desc_map）が既にある場合はそちらを優先し、ない場合のみ付与。
    active_desc_map = dict(desc_map)
    if dsl_data:
        for g in all_groups:
            if not g.is_partner:
                continue
            for s in g.studios:
                title  = str(s.get("studio_title", ""))
                dslurl = g.dslurl_map.get(title, "")
                if title in active_desc_map:
                    continue   # 口コミ説明文を優先
                dsl_desc = build_dsl_partner_desc(title, dslurl, dsl_data)
                if dsl_desc:
                    active_desc_map[title] = dsl_desc
                    log.debug(f"  DSL説明文付与: {title}")

    # エリア外スタジオの一次駅を収集
    if article_type == "駅":
        target_st = area_or_station  # e.g. "中目黒駅"
        distant_stations = sorted({
            g.primary_station
            for g in selected
            if g.primary_station and g.primary_station != target_st
        })
    else:
        distant_stations = []

    # HTML 組み立て
    count      = len(selected)
    slug       = slugify(article_title)
    page_title = make_page_title(area_or_station, article_title, count=count)
    meta_desc  = make_meta_description(area_or_station, count=count)
    h1         = make_h1(area_or_station, article_title, count=count)

    intro_html   = make_intro(area_or_station, article_type, count,
                              distant_stations=distant_stations)
    toc_html     = make_toc(selected, area_or_station)
    cmp_html     = make_comparison_table(selected)
    cards_html   = "\n".join(
        make_studio_card(i + 1, g, active_desc_map, article_type, area_or_station,
                         output_dir=output_dir, dsl_data=dsl_data)
        for i, g in enumerate(selected)
    )
    points_html   = make_selection_points_section(area_or_station)
    area_html     = make_area_section(area_or_station, selected)
    equip_html    = make_equipment_section()
    faq_html, faqs    = make_faq(area_or_station)
    summary_html      = make_summary(area_or_station, selected)
    editorial_html    = make_editorial_note()

    faq_jsonld            = make_faq_jsonld(faqs)
    itemlist_jsonld       = make_itemlist_jsonld(selected, article_title)
    local_business_jsonld = make_local_business_jsonld(selected, article_title)

    from datetime import date
    updated_tag = f'<p class="updated">最終更新: <time datetime="{date.today().isoformat()}">{date.today().strftime("%Y年%m月%d日")}</time></p>\n'

    # ヒーロー画像（最初の提携スタジオ画像）+ OGP画像URL
    hero_html, ogp_img = make_hero_image_html(selected, output_dir=output_dir)

    # パンくずリスト
    breadcrumb_html = make_breadcrumb_html(page_title)

    author_box = make_author_box()

    body = (
        breadcrumb_html
        + f"\n<h1>{h1}</h1>\n"
        + updated_tag
        + hero_html
        + intro_html + "\n"
        + toc_html + "\n"
        + cmp_html + "\n"
        + cards_html + "\n"
        + points_html + "\n"
        + area_html + "\n"
        + equip_html + "\n"
        + faq_html + "\n"
        + summary_html + "\n"
        + editorial_html + "\n"
        + author_box
    )

    sidebar_html = make_sidebar_html(article_title, title_type_map or {}, article_type)
    html     = build_html(page_title, meta_desc, body, faq_jsonld, itemlist_jsonld, sidebar_html,
                          canonical_slug=slug, area_or_station=area_or_station,
                          ogp_image_url=ogp_img,
                          local_business_jsonld=local_business_jsonld)
    out_path = output_dir / f"{slug}.html"
    out_path.write_text(html, encoding="utf-8")
    log.info(f"  → {out_path.name}")

    quality_check_article(html, article_title)

    return out_path, stats, selected_rows


# ============================================================
# メイン
# ============================================================
def main():
    parser = argparse.ArgumentParser()
    grp = parser.add_mutually_exclusive_group(required=True)
    grp.add_argument("--article", help="生成する記事タイトル")
    grp.add_argument("--all",     action="store_true", help="全記事を生成")
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(exist_ok=True)

    log.info("データ読み込み中...")
    df_top15   = pd.read_csv(TOP15_CSV)
    df_studios = pd.read_csv(STUDIO_CSV)
    log.info(f"  TOP15: {len(df_top15)}行 / スタジオマスタ: {len(df_studios)}件")

    # 全提携スタジオ（TOP15未掲載分の直接注入用）
    all_partners = df_studios[
        df_studios["dslurl"].notna()
        & (df_studios["dslurl"].astype(str).str.strip() != "")
    ].copy()
    log.info(f"  提携スタジオ総数（マスタ）: {len(all_partners)}件")

    # Places API 結果から評価マップ（studio_title → {rating, review_count, place_id}）
    api_map: dict = {}
    api_path = Path(PLACES_API_CSV)
    if api_path.exists():
        df_api = pd.read_csv(api_path, usecols=[
            "studio_title", "place_id", "rating", "review_count", "match_confidence",
        ])
        for _, r in df_api.iterrows():
            if pd.notna(r.get("rating")):
                api_map[str(r["studio_title"])] = {
                    "place_id":         r.get("place_id"),
                    "rating":           r.get("rating"),
                    "review_count":     r.get("review_count"),
                    "match_confidence": r.get("match_confidence"),
                }
        log.info(f"  Places API 評価マップ: {len(api_map)}件")

    desc_map  = {}
    desc_path = Path(DESC_CSV)
    if desc_path.exists():
        df_desc  = pd.read_csv(desc_path)
        desc_map = dict(zip(df_desc["studio_title"], df_desc["description"]))
        log.info(f"  口コミ紹介文: {len(desc_map)}件ロード")
    else:
        log.warning(f"  {DESC_CSV} が見つかりません。テンプレート紹介文を使用します")

    # DSL ルームデータ読み込み（提携スタジオの詳細説明文生成に使用）
    dsl_data = load_dsl_room_data(DSL_ROOM_CSV)

    targets = (df_top15["article_title"].unique().tolist()
               if args.all else [args.article])
    title_type_map = (
        df_top15[["article_title", "article_type"]]
        .drop_duplicates("article_title")
        .set_index("article_title")["article_type"]
        .to_dict()
    )

    all_stats, all_selected = [], []
    success, fail = 0, 0

    for title in targets:
        try:
            result = generate_one_article(
                title, df_top15, df_studios, OUTPUT_DIR, desc_map,
                all_partners, api_map, title_type_map,
                dsl_data=dsl_data)
            _, stats, sel_rows = result
            if stats:
                all_stats.append(stats)
            if sel_rows:
                all_selected.extend(sel_rows)
            success += 1
        except Exception as e:
            log.error(f"失敗: {title} — {e}")
            all_stats.append({
                "article_title": title, "selected_group_count": 0,
                "warnings": str(e),
            })
            fail += 1

    # サマリ CSV 出力
    pd.DataFrame(all_stats).to_csv(SUMMARY_CSV, index=False, encoding="utf-8-sig")
    log.info(f"サマリCSV: {SUMMARY_CSV}")

    if all_selected:
        pd.DataFrame(all_selected).to_csv(SELECTED_CSV, index=False, encoding="utf-8-sig")
        log.info(f"選定スタジオCSV: {SELECTED_CSV}")

    log.info(f"=== 完了: 成功{success}件 / 失敗{fail}件 → {OUTPUT_DIR}/")


if __name__ == "__main__":
    main()
