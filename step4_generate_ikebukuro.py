#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
池袋駅レンタルダンススタジオ記事生成 (Step 4 v3)
入力: data/pipeline/池袋駅のレンタルダンススタジオおすすめ_selected_studios.csv
      data/master/studio_with_tags_dslurl_address_filled.csv
      data/pipeline/places_api_results.csv
      data/pipeline/studio_descriptions.csv
出力: articles/池袋駅のレンタルダンススタジオおすすめ.html  (--filename-mode ja, default)
      articles/ikebukuro-station-rental-dance-studio.html  (--filename-mode slug)
"""

import argparse
import datetime
import json
import math
import re
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import pandas as pd
from generate_articles import CSS, build_html

# ============================================================
# 設定
# ============================================================
STATION_CSV  = "data/pipeline/池袋駅のレンタルダンススタジオおすすめ_selected_studios.csv"
STUDIO_CSV   = "data/master/studio_with_tags_dslurl_address_filled.csv"
PLACES_CSV   = "data/pipeline/places_api_results.csv"
DESC_CSV     = "data/pipeline/studio_descriptions.csv"
DETAILS_CSV  = "data/pipeline/studio_site_details.csv"
DSL_SITE_URL = "https://dance-studio-lab.com"
EXCLUDED_CSV     = "data/pipeline/business_status_excluded_studios.csv"
URL_CHECK_REPORT = "data/pipeline/official_url_check_report.csv"
CANDIDATES_CSV   = "data/pipeline/池袋駅のレンタルダンススタジオおすすめ_selection_score_debug.csv"
TARGET_GROUP_COUNT = 15

STATION_NAME  = "池袋駅"
STATION_FREE  = "池袋"
ARTICLE_TITLE = "池袋駅のレンタルダンススタジオおすすめ15選"
FILENAME_BASE = "池袋駅のレンタルダンススタジオおすすめ"
SLUG_NAME     = "ikebukuro-station-rental-dance-studio"

RELATED_ARTICLES = [
    ("池袋のレンタルダンススタジオおすすめ", "池袋のレンタルダンススタジオおすすめ.html"),
    ("高田馬場駅のレンタルダンススタジオおすすめ", "高田馬場駅のレンタルダンススタジオおすすめ.html"),
    ("新宿駅のレンタルダンススタジオおすすめ", "新宿駅のレンタルダンススタジオおすすめ.html"),
    ("中野駅のレンタルダンススタジオおすすめ", "中野駅のレンタルダンススタジオおすすめ.html"),
    ("飯田橋駅のレンタルダンススタジオおすすめ", "飯田橋駅のレンタルダンススタジオおすすめ.html"),
    ("新宿三丁目駅のレンタルダンススタジオおすすめ", "新宿三丁目駅のレンタルダンススタジオおすすめ.html"),
    ("市ケ谷駅のレンタルダンススタジオおすすめ", "市ケ谷駅のレンタルダンススタジオおすすめ.html"),
    ("王子駅のレンタルダンススタジオおすすめ", "王子駅のレンタルダンススタジオおすすめ.html"),
    ("24時間使えるレンタルダンススタジオ 東京", "24時間使えるレンタルダンススタジオ 東京.html"),
    ("安いレンタルダンススタジオ 東京", "安いレンタルダンススタジオ 東京.html"),
]

STUDIO_REGISTRATION_URL = "https://dance-studio-lab.com/studio-registration/lp/"

# ポジティブキーワード: (keywords, japanese_desc, tag)
POSITIVE_FEATURES = [
    (["big enough", "spacious", "large", "wide", "roomy"],
     "フロアが広く、腕を思い切り伸ばした動きや隊形移動も窮屈にならずに確認できる", "広め"),
    (["tripod", "三脚"],
     "三脚が常備されているので、自撮りで動きをチェックしたい日もすぐ撮影に入れる", "三脚あり"),
    (["amazing led", "led", "lighting", "great for taking video"],
     "照明が本格的で、作品映えする動画・写真を撮るのにも向いている", "LED照明"),
    (["speaker", "sound system", "audio", "music system", "スピーカー"],
     "スピーカーがしっかり機能して、音楽の中に入り込むように練習できる環境がある", "音響○"),
    (["clean", "beautiful", "neat", "綺麗", "清潔"],
     "床や鏡の手入れが行き届いていて、気持ちよく集中できる空間", "清潔感"),
    (["comfortable", "comfortably", "easy to use", "使いやすい", "快適"],
     "予約から入室まで使い勝手がよく、練習に集中できると利用者から評価されている", "使いやすい"),
    (["rehearsal", "rehearse", "event", "venue", "リハ", "イベント"],
     "本番前リハーサルやショーケースの場として使った口コミも多く、本格利用にも対応している", "リハーサル向き"),
    (["one person", "solo", "alone", "1 person", "for one", "for myself"],
     "ひとり練習での利用実績が多く、個人で黙々と動きを磨きたい日にも向いている", "個人利用○"),
    (["staff", "friendly", "helpful", "service", "スタッフ"],
     "スタッフの対応がよく、初めて使う人でも戸惑わず利用できるという声がある", "スタッフ対応"),
    (["affordable", "cheapest", "cheap", "value", "コスパ", "安い"],
     "料金が手ごろで、週複数回練習するダンサーでも継続して使いやすいと評判", "コスパ"),
    (["instrument", "楽器", "musical instrument"],
     "楽器演奏にも対応しており、歌やバンドとのコラボ練習など多様な用途で使われている", "楽器可"),
]

# ネガティブキーワード: (keywords, display_tag, body_sentence)
CAUTION_FEATURES = [
    (
        ["wifi", "wi-fi", "internet", "weak"],
        "通信環境確認",
        "Wi-Fi環境は予約前に確認しておくか、モバイル回線を持参しよう。",
    ),
    (
        ["confusing", "hard to find", "difficult to find", "分かりにくい", "confusion"],
        "場所確認推奨",
        "初回訪問は入口を地図で確認してから向かうとスムーズ。",
    ),
    (
        ["small restroom", "restroom was", "toilet"],
        "設備確認推奨",
        "付帯設備は予約前に公式サイトか問い合わせで確認しておこう。",
    ),
    (
        ["took a while", "9 minutes", "far from", "long walk"],
        "アクセス確認",
        "最寄り駅からの実際の所要時間を地図で確認してから向かうとスムーズ。",
    ),
    (
        ["didn't work", "not working", "broken"],
        "設備確認推奨",
        "設備の状態は予約前に問い合わせておくと安心。",
    ),
]

# 追加 CSS
EXTRA_CSS = """<style>
  .dancer-points {
    margin: .7em 0 .8em; padding: 0; list-style: none;
    border-left: 3px solid var(--pink); padding-left: 1em;
  }
  .dancer-points li {
    margin: .4em 0; font-size: .92em; color: var(--text); line-height: 1.7;
  }
  .dancer-points li::before { content: "▶ "; color: var(--pink-dark); font-size: .85em; }
  .review-tag {
    display: inline-block; background: var(--pink-light); color: var(--pink-dark);
    border: 1px solid var(--pink-border); border-radius: 12px;
    padding: .1em .55em; font-size: .82em; margin: .1em .15em; white-space: nowrap;
  }
  .addr-warn {
    color: #c0392b; font-size: .8em; cursor: help;
    border-bottom: 1px dashed #c0392b;
  }
</style>"""

# 生成後チェック: 自動修正パターン (pattern, replacement)
AUTO_FIX_PATTERNS = [
    (r"という声がありますため", "という声もあるため"),
    (r"ありますため", "あるため"),
    (r"レンタット", "レンタル"),
    (r'(<a [^>]+>)詳細↓(</a>)', r'\1詳細を見る\2'),
    (r"詳細・予約</a>", "詳細を見る</a>"),
    (r"Google(\d\.\d)点", r"Google評価\1"),
]

# 生成後チェック: 警告パターン
WARN_PATTERNS = [
    (r">([^<]*?nan[^<]*?)<(?!script)", "nan in text content"),
    (r"★nan", "★nan"),
    (r"要データ確認", "要データ確認"),
    (r"Dance Studio Labで空き状況をリアルタイム確認", "DSL宣伝文"),
    (r"提携スタジオ", "提携スタジオ (本文で使用)"),
    (r"[^\x00-\x7F]\?[^\x00-\x7F]", "住所等に文字化け疑い(?)あり"),
    (r"[^\x00-\x7F]\?<", "住所末尾に文字化け疑い(?)あり"),
]

# 禁止語 (ERROR レベル)
FORBIDDEN = ["要データ確認", "確認が必要", "データ確認中", "要差し替え"]

# 表示名オーバーライド: group_key → 記事向け表示名
GROUP_KEY_DISPLAY_MAP: dict[str, str] = {}

# エリア判定: (住所 or primary_station に含まれるキーワード, 付与するタグリスト)
AREA_TAGS: list[tuple[list[str], list[str]]] = [
    (["東池袋", "南池袋"],               ["東口方面"]),
    (["西池袋"],                          ["西口方面"]),
    (["池袋本町", "北池袋"],              ["北池袋方面"]),
    (["向原"],                            ["向原エリア"]),
]

# 池袋エリア別 説明文 (make_area_section用)
IKEBUKURO_AREA_DESC: dict[str, str] = {
    "池袋駅":   "JR・東武・西武・東京メトロが集まる池袋の中心。東口・西口で雰囲気が変わるため、"
                "スタジオ案内に記載の出口番号を事前に確認して向かうとスムーズです。",
    "北池袋駅": "東武東上線の北池袋。池袋本町エリアで、池袋駅から北へ徒歩10〜15分ほどの落ち着いた立地です。",
    "向原駅":   "東京メトロ有楽町線・副都心線の向原。池袋駅東口から徒歩15分ほどの東池袋方面です。",
}


# ============================================================
# データクラス
# ============================================================
@dataclass
class BranchInfo:
    title: str
    address: str = ""
    primary_station: str = ""
    distance_m: Optional[float] = None
    dslurl: str = ""
    website: str = ""


@dataclass
class Group:
    rank: int
    group_key: str
    rep_title: str
    is_partner: bool
    dslurls: dict = field(default_factory=dict)
    address: str = ""
    primary_station: str = ""
    distance_m: Optional[float] = None
    official_url: str = ""
    branches: list = field(default_factory=list)   # list[BranchInfo]
    google_rating: Optional[float] = None
    google_review_count: Optional[int] = None
    reviews_text: str = ""
    review_count_fetched: int = 0
    desc_src_title: str = ""
    rental_fit_score: int = 0
    low_rating_suppressed: bool = False   # True=評価を非表示にすべき低スコア
    address_quality_ok: bool = True       # False=住所に要確認文字列あり
    url_suppressed: bool = False          # True=公式URLがERRORのため非表示
    # 公式サイトスクレイピング由来の設備情報
    site_area_sqm: Optional[float] = None
    site_ceiling_m: Optional[float] = None
    site_capacity: Optional[int] = None
    site_floor_type: str = ""             # "フローリング"/"リノリウム"/"カーペット"等
    site_has_mirror: Optional[bool] = None
    site_has_speaker: Optional[bool] = None
    site_has_tripod: Optional[bool] = None
    site_has_shower: Optional[bool] = None
    site_has_changing_room: Optional[bool] = None
    site_hourly_rate: Optional[int] = None
    site_dancer_summary: str = ""


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


def _safe_bool(val) -> Optional[bool]:
    """CSV の true/false/null 文字列を Python bool/None に変換"""
    if val is None:
        return None
    try:
        if pd.isna(val):
            return None
    except (TypeError, ValueError):
        pass
    s = str(val).strip().lower()
    if s in ("true", "1", "yes"):
        return True
    if s in ("false", "0", "no"):
        return False
    return None


def safe_str(val) -> str:
    if val is None:
        return ""
    try:
        if pd.isna(val):
            return ""
    except (TypeError, ValueError):
        pass
    s = str(val).strip()
    return "" if s.lower() == "nan" else s


def dist_to_minutes(m: Optional[float]) -> str:
    if m is None:
        return ""
    n = max(1, round(m / 80))
    return f"徒歩約{n}分"


def fmt_rating_html(rating, review_count, suppress: bool = False) -> str:
    if suppress:
        return ""
    r = safe_float(rating)
    if r is None:
        return ""
    rc = safe_int(review_count)
    if rc:
        return f'<div class="studio-rating">Google評価 {r:.1f}（{rc}件）</div>'
    return f'<div class="studio-rating">Google評価 {r:.1f}</div>'


def fmt_rating_text(rating, review_count, suppress: bool = False) -> tuple:
    if suppress:
        return "—", "—"
    r = safe_float(rating)
    if r is None:
        return "—", "—"
    rc = safe_int(review_count)
    return f"{r:.1f}", (str(rc) if rc else "—")


def should_hide_rating(rating, review_count) -> bool:
    """
    口コミが極端に少なく評価が低い場合は表示しない。
    google_review_count <= 1 かつ rating < 3.0
    """
    r  = safe_float(rating)
    rc = safe_int(review_count)
    if r is None:
        return False
    if rc is None or rc <= 1:
        return r < 3.0
    return False


def _get_area_tags(address: str, primary_station: str) -> list[str]:
    """住所 + primary_station からエリアタグを返す"""
    combined = f"{address} {primary_station}"
    for keywords, tags in AREA_TAGS:
        if any(kw in combined for kw in keywords):
            return tags[:]
    return []


def maps_url(address: str) -> str:
    if not address:
        return ""
    return "https://www.google.com/maps/search/?api=1&query=" + urllib.parse.quote(address)


def _extract_area(address: str) -> str:
    """住所から町名を抽出 (例: '東京都新宿区歌舞伎町' → '歌舞伎町')"""
    m = re.search(r'区(.{2,6}?)(?:[0-9０-９]|\d|丁目)', address)
    if m:
        return m.group(1).strip()
    return ""


def get_display_name(g: Group) -> str:
    """
    見出し・目次・比較表用の短い表示名。
    GROUP_KEY_DISPLAY_MAP > group_key > rep_title の優先順位で決定。
    """
    # 手動オーバーライドが最優先
    if g.group_key in GROUP_KEY_DISPLAY_MAP:
        return GROUP_KEY_DISPLAY_MAP[g.group_key]
    # group_key を優先 (パイプラインで正規化済み)
    name = g.group_key or g.rep_title
    # 長いスタジオ説明は区切り文字で切り詰める
    for sep in [" - ", "　-　", " — ", "　"]:
        if sep in name:
            name = name.split(sep)[0].strip()
    # 30文字超は末尾を切る
    if len(name) > 30:
        name = name[:30].rstrip()
    return name



# ============================================================
# 検索URL
# ============================================================
def build_search_url() -> str:
    today = datetime.date.today()
    selected_date = (today + datetime.timedelta(days=5)).strftime("%Y-%m-%d")
    params = [
        ("selected_date", selected_date),
        ("start_hour", "18"),
        ("start_minute", "0"),
        ("end_hour", "19"),
        ("end_minute", "0"),
        ("free", STATION_FREE),
        ("freeword_type", "or"),
    ]
    qs = "&".join(f"{k}={urllib.parse.quote(v, safe='')}" for k, v in params)
    return f"{DSL_SITE_URL}/search?{qs}"


# ============================================================
# 出力パス
# ============================================================
def make_output_path(mode: str) -> Path:
    if mode == "slug":
        return Path(f"articles/{SLUG_NAME}.html")
    # "ja" または "both" の場合は日本語ファイル名
    safe = re.sub(r'[/\\:*?"<>|&]', "", FILENAME_BASE)
    return Path("articles") / (safe + ".html")


# ============================================================
# 口コミ特徴抽出
# ============================================================
def summarize_reviews_for_dancers(reviews_text: str) -> dict:
    """
    Returns:
      review_positive_points: list[str]
      review_caution_points:  list[dict] ← {"tag":str, "sentence":str}
      dancer_relevant_points: list[str]
      feature_tags: list[str]
    """
    text = reviews_text.lower()

    pos_points: list[str] = []
    pos_tags: list[str] = []
    caut_points: list[dict] = []
    caut_tags: list[str] = []

    for keywords, desc, tag in POSITIVE_FEATURES:
        if any(kw in text for kw in keywords):
            pos_points.append(desc)
            pos_tags.append(tag)

    for keywords, tag, sentence in CAUTION_FEATURES:
        if any(kw in text for kw in keywords):
            caut_points.append({"tag": tag, "sentence": sentence})
            caut_tags.append(tag)

    # ダンサー目線の特記事項
    dancer_points: list[str] = []
    if "tripod" in text or "三脚" in text:
        dancer_points.append("三脚が備え付けられており、ソロ動画撮影にも活用できます")
    if "led" in text and ("video" in text or "photo" in text):
        dancer_points.append("LED照明を活かした動画・写真撮影にも向いています")
    if any(kw in text for kw in ["one person", "solo", "for one", "for myself", "alone"]):
        dancer_points.append("1人での個人練習での利用実績があります")
    if any(kw in text for kw in ["rehearsal", "rehearse"]):
        dancer_points.append("本番前リハーサルなど本格的な練習用途の口コミもあります")

    return {
        "review_positive_points": pos_points,
        "review_caution_points":  caut_points,
        "dancer_relevant_points": dancer_points,
        "feature_tags": pos_tags + [d["tag"] for d in caut_points],
    }


# ============================================================
# 特徴タグ (比較表用) ─ ネガティブタグは出さない
# ============================================================
def get_feature_tags(g: Group) -> list[str]:
    """
    比較表の「特徴・確認ポイント」列用タグ。
    - 提携スタジオ: ポジティブ系タグ優先
    - 口コミあり: ポジティブタグのみ
    - 口コミなし: 評価 or デフォルト「設備確認推奨」
    ネガティブタグ (WiFi弱め等) は一切出さない。
    """
    # ポジティブタグ候補
    positive_only_tags: list[str] = []

    if g.reviews_text and g.review_count_fetched > 0:
        summary = summarize_reviews_for_dancers(g.reviews_text)
        positive_only_tags = summary["feature_tags"][:3]
        # ネガティブタグを除去
        negative_tag_kws = {"通信環境確認", "場所確認推奨", "設備確認推奨", "アクセス確認"}
        positive_only_tags = [t for t in positive_only_tags if t not in negative_tag_kws]

    # 提携スタジオ: ポジティブ優先
    if g.is_partner:
        tags = positive_only_tags[:]
        if 1 + len(g.branches) > 1:
            tags.append("複数店舗あり")
        tags.append("予約導線あり")
        return tags[:3]

    # 口コミ由来タグがある
    if positive_only_tags:
        return positive_only_tags[:3]

    # 評価が良好 (5件以上かつ4.0以上)
    r  = g.google_rating
    rc = g.google_review_count
    if r is not None and rc and rc >= 5:
        if r >= 4.3:
            return ["評価高め"]
        elif r >= 3.8:
            return [f"Google評価{r:.1f}"]

    # エリアタグ (デフォルトの代わりに使えるものがあれば活用)
    area_tags = _get_area_tags(g.address, g.primary_station)

    if 1 + len(g.branches) > 1:
        base = area_tags[:1] if area_tags else ["設備確認推奨"]
        return (["複数店舗あり"] + base)[:3]

    return (area_tags[:2] + ["設備確認推奨"])[:2] if area_tags else ["設備確認推奨"]


# ============================================================
# 口コミなし時の説明文
# ============================================================
def _build_equipment_sentence(g: Group) -> str:
    """
    公式サイト由来の設備情報からダンサー向け説明文を1文生成。
    情報がなければ空文字を返す。
    """
    parts: list[str] = []

    # 床材
    ft = g.site_floor_type
    if ft in ("フローリング", "リノリウム"):
        parts.append(f"{ft}床")

    # 面積 + 定員
    if g.site_area_sqm and g.site_area_sqm >= 20:
        if g.site_capacity:
            parts.append(f"面積{g.site_area_sqm:.0f}㎡・最大{g.site_capacity}人規模")
        else:
            parts.append(f"面積{g.site_area_sqm:.0f}㎡")
    elif g.site_capacity:
        parts.append(f"最大{g.site_capacity}人対応")

    # 設備
    if g.site_has_tripod:
        parts.append("三脚常備")
    if g.site_has_speaker:
        parts.append("スピーカー完備")
    if g.site_has_mirror:
        parts.append("全身鏡あり")
    if g.site_has_shower and g.site_has_changing_room:
        parts.append("シャワー・更衣室あり")
    elif g.site_has_changing_room:
        parts.append("更衣室あり")

    if not parts:
        # dancer_summary だけあれば使う
        if g.site_dancer_summary:
            return f"{g.site_dancer_summary}。"
        return ""

    # 読点で繋げて1文に
    body = "、".join(parts)
    suffix = g.site_dancer_summary
    if suffix:
        return f"{body}。{suffix}。"
    return f"{body}で練習できる環境が整っている。"


def _make_rich_fallback(g: Group) -> str:
    """
    口コミテキストがない場合の説明文。
    ①エリア/タイプ特徴  ②設備(site data) or 駅近  ③利用アドバイス(具体的な場合のみ)
    評価文・汎用「設備確認しよう」は出力しない。
    """
    area  = _extract_area(g.address)
    name  = g.rep_title
    total = 1 + len(g.branches)
    dist_m = g.distance_m

    area_tags     = _get_area_tags(g.address, g.primary_station)
    has_east  = "東口方面" in area_tags
    has_west  = "西口方面" in area_tags
    has_kita  = "北池袋方面" in area_tags
    has_mukai = "向原エリア" in area_tags

    is_dance  = any(kw in name for kw in ["ダンス", "dance", "Dance", "Beat", "beat"])
    is_rental = any(kw in name for kw in ["レンタル", "rental", "Rental"])
    area_str  = f"{area}エリア" if area else f"{g.primary_station or STATION_NAME}周辺"

    # ① S1: エリア/タイプ特徴
    # (total > 1 のとき make_desc() が「X店舗を展開」と出しているため繰り返さない)
    if total > 1:
        s1 = f"集合場所や空き状況に合わせて{area_str}の複数拠点から選びやすいのが利点。"
    elif has_kita:
        s1 = "池袋本町・北池袋エリアの落ち着いた住宅街にある、練習に集中しやすいスタジオ。"
    elif has_mukai:
        s1 = "東池袋・向原方面に位置し、池袋駅からのアクセスも良好なエリアのスタジオ。"
    elif has_east:
        s1 = "池袋東口側の立地で、東武・西武の乗り換えもしやすいエリアのスタジオ。"
    elif has_west:
        s1 = "池袋西口側の立地で、商業施設が集まるエリアに近い利便性の高いスタジオ。"
    elif is_dance and not is_rental:
        s1 = f"ダンス練習・振付確認に特化した{area_str}のスタジオ。"
    elif is_rental:
        s1 = f"個人練習から少人数グループまで幅広い用途で使える{area_str}のレンタルスタジオ。"
    elif g.rental_fit_score >= 40:
        s1 = f"ダンス練習での利用実績もある{area_str}のレンタルスタジオ。"
    else:
        s1 = f"個人練習・グループ練習に使える{area_str}のレンタルスタジオ。"

    # ② S2: 設備情報(公式サイト由来) > 駅近 > 提携
    s2 = _build_equipment_sentence(g)
    if not s2:
        if dist_m is not None and dist_m <= 600:
            st = g.primary_station or STATION_NAME
            n  = max(1, round(dist_m / 80))
            s2 = f"{st}から徒歩約{n}分と駅近で、仕事帰りの練習にも使いやすい立地。"
        elif g.is_partner:
            s2 = "オンラインで空き状況を確認・予約できる提携スタジオ。"

    # ③ S3: 具体的な利用アドバイス (汎用文は出さない)
    s3 = ""
    s2_mentions_partner = (s2 == "オンラインで空き状況を確認・予約できる提携スタジオ。")
    if g.is_partner and not s2_mentions_partner:
        s3 = "空き状況はオンラインでリアルタイムに確認・予約できる。"
    elif g.site_hourly_rate:
        s3 = f"1時間{g.site_hourly_rate:,}円〜で利用できる。"
    elif has_kita:
        s3 = "北池袋・池袋本町エリアで練習場所を探している場合の候補。池袋駅北口からバス利用も便利。"
    elif has_mukai:
        s3 = "東池袋方面ならではの静かな環境で、振付をじっくり固めたいときにおすすめ。"
    # それ以外は S3 なし（汎用文を出さない）

    parts = [s1]
    if s2:
        parts.append(s2)
    if s3:
        parts.append(s3)
    return "".join(parts)


# ============================================================
# 説明文生成 (優先順位: 口コミ > 評価 > rich fallback)
# ============================================================
def make_desc(g: Group) -> str:
    parts: list[str] = []
    dist  = dist_to_minutes(g.distance_m)
    total = 1 + len(g.branches)

    # --- 1. 立地 ---
    if total > 1:
        parts.append(
            f"{STATION_NAME}周辺に{total}店舗を展開するレンタルスタジオです。"
        )
    elif dist:
        parts.append(f"{g.primary_station or STATION_NAME}{dist}の立地にあるレンタルダンススタジオです。")
    else:
        area = _extract_area(g.address)
        parts.append(
            f"{area + 'エリアに位置する' if area else STATION_NAME + '周辺で利用できる'}"
            f"レンタルダンススタジオです。"
        )

    # --- 2. 口コミ由来の特徴 ---
    if g.reviews_text and g.review_count_fetched > 0:
        summary  = summarize_reviews_for_dancers(g.reviews_text)
        pos_pts  = summary["review_positive_points"]
        # 提携スタジオはネガティブを出さない
        caut_pts = summary["review_caution_points"] if not g.is_partner else []

        if pos_pts:
            p1 = pos_pts[0].rstrip("。")
            if len(pos_pts) >= 2:
                p2 = pos_pts[1].rstrip("。")
                parts.append(f"{p1}。{p2}。")
            else:
                parts.append(f"{p1}。")

        if caut_pts:
            # 自然な文章をそのまま使う (「〜がありますため」にならない)
            parts.append(caut_pts[0]["sentence"])

    # --- 3. 口コミなし → rich fallback (評価はS2で含まれる) ---
    else:
        parts.append(_make_rich_fallback(g))

    return "".join(parts[:3])


# ============================================================
# dancer-points HTML
# ============================================================
def make_dancer_points_html(g: Group) -> str:
    points: list[str] = []

    # 口コミ由来ポイント（ダンサー目線の観察）
    if g.reviews_text and g.review_count_fetched > 0:
        summary = summarize_reviews_for_dancers(g.reviews_text)
        for p in summary["dancer_relevant_points"][:2]:
            points.append(p)

    # 床材（ダンサーにとって最重要の設備のひとつ）
    ft = g.site_floor_type
    if ft == "フローリング":
        points.append("フローリング床で、ヒールやスニーカーどちらでも動きやすい環境です")
    elif ft == "リノリウム":
        points.append("リノリウム床で、スピン・ターン系の動きをしっかりコントロールできます")

    # アクセス（移動のしやすさはダンサーにとって切実）
    if g.distance_m:
        dist = dist_to_minutes(g.distance_m)
        st   = g.primary_station or STATION_NAME
        if g.distance_m <= 400:
            points.append(f"{st}から{dist}と駅直近——荷物が多い日でも移動負担が少なくて助かります")
        elif g.distance_m <= 700:
            points.append(f"{st}から{dist}圏内で、仕事帰りや移動前後の練習にも組み込みやすい立地です")
        elif g.distance_m <= 1200:
            points.append(f"{st}から{dist}ほど。初回は地図で入口を確認してから向かうとスムーズです")
        else:
            points.append(f"{st}周辺のスタジオです。初回は入口の場所を事前に確認しておくことをおすすめします")

    # 設備ポイント（スタジオ固有の強みを具体的に）
    if g.site_has_tripod:
        points.append("三脚が常備されているので、ソロ動画の撮影もすぐ始められます")
    if g.site_has_speaker and not g.site_has_tripod:
        # 三脚のポイントと重複しないように
        points.append("スピーカー完備で、自分の音源を大きな音で流しながら練習できます")
    if g.site_has_shower and g.site_has_changing_room:
        points.append("シャワー・更衣室完備なので、本番・撮影前の準備や練習後のケアも一か所で済みます")
    elif g.site_has_changing_room:
        points.append("更衣室があるため、衣装に着替えてから練習・撮影したいときも安心です")
    if g.site_area_sqm and g.site_area_sqm >= 50:
        points.append(f"床面積{g.site_area_sqm:.0f}㎡と広めのスペースで、隊形移動を含む振付確認にも向いています")
    elif g.site_area_sqm and g.site_area_sqm >= 30:
        points.append(f"床面積{g.site_area_sqm:.0f}㎡。少人数グループの振付合わせや個人練習にちょうどいいサイズ感です")

    # 料金（コスパ感を伝える）
    if g.site_hourly_rate:
        points.append(f"1時間{g.site_hourly_rate:,}円〜の料金設定で、頻繁に使いたい人にも手が届きやすい価格帯です")

    # 予約導線 (DSL名は出さない)
    if g.is_partner and g.dslurls:
        points.append("空き状況はオンラインでリアルタイムに確認・予約できます")

    # 注意点 (非提携のみ)
    if not g.is_partner and g.reviews_text and g.review_count_fetched > 0:
        summary = summarize_reviews_for_dancers(g.reviews_text)
        for c in summary["review_caution_points"][:1]:
            points.append(f"確認ポイント：{c['sentence'].rstrip('。')}")

    if not points:
        return ""

    items = "\n".join(f"    <li>{p}</li>" for p in points[:5])
    return f'  <ul class="dancer-points">\n{items}\n  </ul>'


# ============================================================
# HTML パーツ
# ============================================================
def make_h1() -> str:
    return f"<h1>{ARTICLE_TITLE}</h1>"


def make_intro(count: int) -> str:
    return (
        f"<p>池袋でスタジオを探すとき、「東口と西口どっちのエリア？」「駅近で使いやすいところがいい」"
        f"「一人でも気兼ねなく使えるところがいい」——ダンサーによって優先するポイントはさまざまだと思います。</p>\n"
        f"<p>この記事では、{STATION_NAME}周辺で実際に練習場所として使えるレンタルダンススタジオ{count}か所を、"
        f"アクセス・設備・利用者の口コミをもとに紹介します。"
        f"「今日の練習はここにしよう」と決めるときの参考にしてもらえると嬉しいです。</p>\n"
        f"<p>池袋はJR・東武・西武・東京メトロが交わる交通の要所で、東口・西口それぞれのエリアにスタジオが点在しています。"
        f"目的地や集合場所に合わせて、自分に合ったスタジオを見つけてみてください。</p>"
    )


def make_toc(groups: list[Group]) -> str:
    items = []
    for g in groups:
        dn    = get_display_name(g)
        total = 1 + len(g.branches)
        if total > 1:
            all_names   = [g.rep_title] + [b.title for b in g.branches]
            branch_note = "　".join(all_names[:4])
            if total > 4:
                branch_note += f"　他{total - 4}店舗"
            items.append(
                f'    <li><a href="#group-{g.rank}">{g.rank}. {dn}</a>\n'
                f'      <ul class="sub-note"><li>{branch_note}</li></ul></li>'
            )
        else:
            items.append(f'    <li><a href="#group-{g.rank}">{g.rank}. {dn}</a></li>')

    items += [
        f'    <li><a href="#points">{STATION_NAME}でスタジオを選ぶポイント</a></li>',
        '    <li><a href="#areas">エリア・駅別の特徴</a></li>',
        '    <li><a href="#equipment">個人練習・グループ練習で確認すべき設備</a></li>',
        '    <li><a href="#related">関連記事</a></li>',
        '    <li><a href="#summary">まとめ</a></li>',
        '    <li><a href="#faq">よくある質問</a></li>',
    ]
    return (
        '\n<nav class="toc">\n  <strong>目次</strong>\n  <ol>\n'
        + "\n".join(items)
        + "\n  </ol>\n</nav>"
    )


def make_comparison_table(groups: list[Group]) -> str:
    rows = []
    for g in groups:
        dn            = get_display_name(g)
        rating_t, rc_t = fmt_rating_text(g.google_rating, g.google_review_count, g.low_rating_suppressed)
        dist  = dist_to_minutes(g.distance_m) if g.distance_m else "—"
        st    = g.primary_station or "—"

        if g.is_partner and g.dslurls:
            first_url = next(iter(g.dslurls.values()))
            link = f'<a href="{first_url}" target="_blank" rel="noopener">詳細を見る</a>'
        elif g.official_url:
            link = f'<a href="{g.official_url}" target="_blank" rel="noopener">公式サイトを見る</a>'
        else:
            link = f'<a href="#group-{g.rank}">詳細を見る</a>'

        tags_html = " ".join(
            f'<span class="review-tag">{t}</span>' for t in get_feature_tags(g)
        )
        rows.append(
            f"  <tr>\n"
            f'    <td><a href="#group-{g.rank}">{dn}</a></td>\n'
            f"    <td>{st}</td><td>{dist}</td>\n"
            f"    <td>{rating_t}</td><td>{rc_t}</td>\n"
            f"    <td>{tags_html}</td>\n"
            f'    <td><span class="ct-ok">○</span></td>\n'
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
        '      <th>特徴・確認ポイント</th>\n'
        '      <th>個人練習</th><th>詳細</th>\n'
        '    </tr></thead>\n    <tbody>\n'
        + "\n".join(rows)
        + "\n    </tbody>\n  </table>\n  </div>\n</section>"
    )


def _cta_label_for_branch(group_key: str, store_title: str) -> str:
    """
    branch の CTA ラベルを作る。
    group_key を除去した残りが短すぎる場合 (例: 'BUZZ新宿' → '新宿') は
    group_key を先頭に付けて 'BUZZ新宿' にする。
    """
    stripped = store_title.replace(group_key, "").strip(" 　・-")
    if not stripped:
        return store_title
    # 残りが短い (3文字以下) か地名のみなら group_key を前置
    if len(stripped) <= 3 or re.match(r'^[\u3000-\u9fff]{1,4}$', stripped):
        return group_key + stripped
    return stripped


def _make_cta_single(g: Group) -> str:
    if g.is_partner and g.dslurls:
        dslurl = next(iter(g.dslurls.values()))
        dn = get_display_name(g)
        label = f"{dn}の空き状況を確認する" if len(dn) <= 20 else "空き状況を確認する"
        return (
            '\n  <div class="dsl-booking">\n'
            f'    <a href="{dslurl}" target="_blank" rel="noopener" class="dsl-btn">'
            f'{label}</a>\n  </div>'
        )
    if g.official_url:
        return (
            '\n  <div class="dsl-booking">\n'
            f'    <a href="{g.official_url}" target="_blank" rel="noopener" class="dsl-btn">'
            f"公式サイトを見る</a>\n  </div>"
        )
    return ""


def _make_cta_multi(g: Group) -> str:
    if not g.is_partner or not g.dslurls:
        return ""
    btns = []
    for title, url in g.dslurls.items():
        label = _cta_label_for_branch(g.group_key, title)
        btns.append(
            f'    <a href="{url}" target="_blank" rel="noopener" class="dsl-btn">'
            f"{label}の空き状況を確認する</a>"
        )
    return (
        '\n  <div class="dsl-booking">\n'
        + "\n".join(btns)
        + "\n  </div>"
    )


def make_studio_card(g: Group) -> str:
    desc          = make_desc(g)
    dn            = get_display_name(g)
    rating_html   = fmt_rating_html(g.google_rating, g.google_review_count, g.low_rating_suppressed)
    dancer_points = make_dancer_points_html(g)
    total         = 1 + len(g.branches)

    if total == 1:
        map_lnk = ""
        if g.address:
            mu      = maps_url(g.address)
            map_lnk = f' <a href="{mu}" target="_blank" rel="noopener">地図</a>'

        info = ""
        if g.address:
            addr_disp = _display_address(g.address, g.address_quality_ok)
            info += f"      <tr><th>住所</th><td>{addr_disp}{map_lnk}</td></tr>\n"
        dist = dist_to_minutes(g.distance_m)
        if g.primary_station and dist:
            info += f"      <tr><th>アクセス</th><td>{g.primary_station} {dist}</td></tr>\n"
        elif g.primary_station:
            info += f"      <tr><th>アクセス</th><td>{g.primary_station}</td></tr>\n"
        if g.official_url:
            info += (
                f'      <tr><th>公式サイト</th><td>'
                f'<a href="{g.official_url}" target="_blank" rel="noopener">公式サイト</a>'
                f"</td></tr>\n"
            )

        cta = _make_cta_single(g)
        return (
            f'\n<section class="studio-card" id="group-{g.rank}">\n'
            f"  <h2>{g.rank}. {dn}</h2>\n"
            f"{rating_html}\n"
            f'  <table class="studio-info"><tbody>\n{info}  </tbody></table>\n'
            f'  <p class="studio-desc">{desc}</p>\n'
            f"{dancer_points}\n"
            f"{cta}\n</section>"
        )
    else:
        all_entries = [
            BranchInfo(
                title=g.rep_title, address=g.address,
                primary_station=g.primary_station, distance_m=g.distance_m,
                dslurl=g.dslurls.get(g.rep_title, ""), website=g.official_url,
            )
        ] + g.branches

        branch_items = []
        for bi in all_entries:
            dist   = dist_to_minutes(bi.distance_m)
            access = (
                f"{bi.primary_station} {dist}"
                if bi.primary_station and dist
                else (bi.primary_station or "—")
            )
            map_lnk = ""
            if bi.address:
                mu      = maps_url(bi.address)
                map_lnk = f'<a href="{mu}" target="_blank" rel="noopener">地図</a> '

            if bi.dslurl:
                site_lnk = f'<a href="{bi.dslurl}" target="_blank" rel="noopener">詳細を見る</a>'
            elif bi.website:
                site_lnk = f'<a href="{bi.website}" target="_blank" rel="noopener">公式サイトを見る</a>'
            else:
                site_lnk = ""

            branch_items.append(
                f"    <li>\n"
                f'      <span class="branch-name">{bi.title}</span>\n'
                f'      <span class="branch-access">{access}　{map_lnk}{site_lnk}</span>\n'
                f"    </li>"
            )

        cta = _make_cta_multi(g)
        return (
            f'\n<section class="studio-card" id="group-{g.rank}">\n'
            f"  <h2>{g.rank}. {dn}（{total}店舗）</h2>\n"
            f'  <p class="studio-desc">{desc}</p>\n'
            f"{dancer_points}\n"
            f'  <ul class="branch-list">\n'
            + "\n".join(branch_items)
            + f"\n  </ul>\n"
            f"{cta}\n</section>"
        )


def make_selection_points() -> str:
    return (
        f'\n<section class="info-section" id="points">\n'
        f"  <h2>{STATION_NAME}でレンタルダンススタジオを選ぶポイント</h2>\n"
        f"  <ul>\n"
        f"    <li><strong>駅からの距離と出口：</strong>池袋は東口・西口で方向が変わるため、"
        f"「どの出口から何分」かを公式サイトや案内で確認しておくとスムーズです。</li>\n"
        f"    <li><strong>床材の確認：</strong>ダンス練習にはフローリングやリノリウム床が向いています。"
        f"カーペット床はターンやスライドに不向きな場合があるため、予約前に確認しておきましょう。</li>\n"
        f"    <li><strong>鏡の有無・サイズ：</strong>振付確認には全身が映る大きな鏡が便利です。"
        f"鏡の枚数や配置は公式サイトの写真で事前に確認できることが多いです。</li>\n"
        f"    <li><strong>部屋の広さ：</strong>個人練習なら10〜20㎡程度、グループ練習には30㎡以上が目安です。"
        f"人数に合わせた広さを確認しておきましょう。</li>\n"
        f"    <li><strong>音響設備：</strong>Bluetooth対応スピーカーや音楽再生環境が整っているか確認しましょう。"
        f"持ち込み可否についても確認しておくと安心です。</li>\n"
        f"    <li><strong>予約のしやすさ：</strong>オンライン予約・直前予約への対応、"
        f"キャンセルポリシーも事前に確認しておくと利用しやすくなります。</li>\n"
        f"    <li><strong>複数店舗の活用：</strong>周辺に複数店舗を持つブランドは、"
        f"満室時に別店舗で予約できる柔軟性があります。</li>\n"
        f"  </ul>\n</section>"
    )


def make_area_section(groups: list[Group]) -> str:
    # 掲載グループから使用駅をカウント
    station_counts: dict[str, int] = {}
    for g in groups:
        st = g.primary_station
        if st:
            station_counts[st] = station_counts.get(st, 0) + 1

    if not station_counts:
        return ""

    rows = []
    # IKEBUKURO_AREA_DESC に記載された駅を優先 (件数の多い順)
    ordered_stations = sorted(
        station_counts.keys(),
        key=lambda s: (s not in IKEBUKURO_AREA_DESC, -station_counts[s]),
    )
    for st in ordered_stations:
        cnt  = station_counts[st]
        desc = IKEBUKURO_AREA_DESC.get(
            st,
            f"掲載スタジオ{cnt}主体。空き状況・設備は各スタジオの公式サイトでご確認ください。",
        )
        rows.append(
            f"  <tr><td><strong>{st}周辺</strong></td>"
            f"<td>（{cnt}主体）{desc}</td></tr>"
        )

    return (
        '\n<section class="info-section" id="areas">\n'
        "  <h2>エリア・駅別の特徴</h2>\n"
        "  <p>池袋駅は東武・西武・JR・東京メトロが交わるターミナルで、"
        "東口・西口それぞれのエリアによって雰囲気や利用者層が異なります。"
        "スタジオ選びの際はエリア特性も参考にしてください。</p>\n"
        '  <table class="area-table">\n'
        "    <thead><tr><th>エリア（駅）</th><th>特徴</th></tr></thead>\n"
        "    <tbody>\n"
        + "\n".join(rows)
        + "\n    </tbody>\n  </table>\n</section>"
    )


def make_equipment_section() -> str:
    return (
        '\n<section class="info-section" id="equipment">\n'
        "  <h2>個人練習・グループ練習で確認すべき設備</h2>\n"
        "  <h3>個人練習の場合</h3>\n"
        "  <ul>\n"
        "    <li>全身が映る鏡（フォームや振付を確認できるサイズ）</li>\n"
        "    <li>音楽再生環境（スピーカー・Bluetooth接続など）</li>\n"
        "    <li>1時間単位で予約できる柔軟な料金体系</li>\n"
        "    <li>荷物を置けるスペースや更衣室の有無</li>\n"
        "  </ul>\n"
        "  <h3>グループ練習の場合</h3>\n"
        "  <ul>\n"
        "    <li>4〜10人が動ける広さ（30㎡以上が目安）</li>\n"
        "    <li>フォーメーション確認しやすい鏡の配置</li>\n"
        "    <li>スピーカーの音量が十分であること</li>\n"
        "    <li>複数時間まとめて予約できる料金プランの有無</li>\n"
        "    <li>着替えスペース・荷物置き場の確保</li>\n"
        "  </ul>\n</section>"
    )


def make_related_articles() -> str:
    items = "\n".join(
        f'    <li><a href="{href}">{label}</a></li>'
        for label, href in RELATED_ARTICLES
    )
    return (
        '\n<section class="info-section" id="related">\n'
        "  <h2>関連記事</h2>\n"
        "  <ul>\n"
        f"{items}\n"
        "  </ul>\n</section>"
    )


def make_sidebar() -> str:
    """サイドバーHTML: 掲載CTAウィジェット + 関連記事ウィジェット"""
    cta_widget = (
        '<div class="sidebar-widget sidebar-cta">\n'
        '  <p class="sidebar-cta-title">スタジオ掲載・集客のご相談</p>\n'
        f'  <a href="{STUDIO_REGISTRATION_URL}" target="_blank" rel="noopener sponsored"'
        ' class="sidebar-cta-btn">掲載について問い合わせる</a>\n'
        '</div>'
    )
    items = "\n".join(
        f'    <li><a href="{href}">{label}</a></li>'
        for label, href in RELATED_ARTICLES
    )
    related_widget = (
        '<div class="sidebar-widget sidebar-related">\n'
        '  <h3>関連記事</h3>\n'
        '  <ul>\n'
        f'{items}\n'
        '  </ul>\n'
        '</div>'
    )
    return cta_widget + "\n" + related_widget


def make_summary(groups: list[Group], search_url: str) -> str:
    top_names = "、".join(get_display_name(g) for g in groups[:3])
    return (
        '\n<div id="summary">\n'
        '<section class="article-summary">\n'
        "  <h2>まとめ</h2>\n"
        f"  <p>{STATION_NAME}周辺には、{top_names}などのレンタルダンススタジオがあります。"
        f"いずれも駅から近い立地にあり、個人練習・グループ練習・振付確認など幅広い用途に使えます。</p>\n"
        f"  <p>気になるスタジオがあれば、空き状況や設備条件を確認しながら、"
        f"練習スタイルに合う場所を選んでみてください。"
        f"鏡の有無・床の種類・部屋の広さなど、用途に合わせた条件で比較するのが選びやすい方法です。</p>\n"
        f'  <div class="dsl-search-link">\n'
        f'    <a href="{search_url}" target="_blank" rel="noopener">'
        f"{STATION_NAME}周辺のレンタルダンススタジオを検索する</a>\n"
        f"  </div>\n"
        f"</section>\n</div>"
    )


def make_faq() -> tuple[str, list[dict]]:
    faqs = [
        {
            "q": "池袋駅の東口と西口、スタジオはどちらに多いですか？",
            "a": (
                "掲載スタジオでは、東口側（東池袋・南池袋方面）と西口側（西池袋方面）の両方にスタジオがあります。"
                "各スタジオの案内に記載の出口番号を事前に確認してから向かいましょう。"
            ),
        },
        {
            "q": "夜遅い時間帯でも利用できるスタジオはありますか？",
            "a": (
                "池袋周辺は夜間も交通が便利なエリアです。"
                "各スタジオの営業時間は公式サイトやオンライン予約画面で確認できます。"
                "深夜帯まで利用できるスタジオもありますが、予約可能な時間枠は日によって異なるため、"
                "利用したい時間帯の空き状況を事前にチェックしてください。"
            ),
        },
        {
            "q": f"{STATION_NAME}周辺でオンライン予約できるスタジオは？",
            "a": (
                "BUZZ レンタルスタジオ（池袋東口BASE・池袋西口PARK店）、"
                "レンタルスタジオAivic（池袋西口・東口1号店・東口2号店）、"
                "スタジオピアーチェ池袋店はオンラインで空き状況を確認・予約できます。"
                "各スタジオのページから日程と時間を選んで予約してください。"
            ),
        },
        {
            "q": "予約前に確認しておくべきことは？",
            "a": (
                "①空き状況・料金体系、②キャンセルポリシー、"
                "③持ち込み機材の可否（スピーカー・三脚など）、"
                "④シャワーや更衣室の有無、⑤複数人で使う場合の人数上限、"
                "の5点を確認しておくと安心です。"
                "池袋は出口が多いため、案内に記載の出口番号も合わせて確認しておきましょう。"
            ),
        },
    ]

    items = []
    for f in faqs:
        items.append(
            f'  <div class="faq-item">\n'
            f'    <div class="faq-q">{f["q"]}</div>\n'
            f'    <div class="faq-a">{f["a"]}</div>\n'
            f"  </div>"
        )

    html = (
        '\n<section class="faq-section" id="faq">\n'
        "  <h2>よくある質問</h2>\n"
        + "\n".join(items)
        + "\n</section>"
    )
    return html, faqs


def make_faq_jsonld(faqs: list[dict]) -> str:
    data = {
        "@context": "https://schema.org",
        "@type": "FAQPage",
        "mainEntity": [
            {
                "@type": "Question",
                "name": f["q"],
                "acceptedAnswer": {"@type": "Answer", "text": f["a"]},
            }
            for f in faqs
        ],
    }
    return (
        '<script type="application/ld+json">\n'
        + json.dumps(data, ensure_ascii=False, indent=2)
        + "\n</script>"
    )


def make_itemlist_jsonld(groups: list[Group]) -> str:
    items = []
    for g in groups:
        dn  = get_display_name(g)
        url = (
            next(iter(g.dslurls.values())) if g.dslurls
            else (g.official_url or DSL_SITE_URL)
        )
        item: dict = {"@type": "ListItem", "position": g.rank, "name": dn, "url": url}
        if g.address:
            item["address"] = g.address
        if g.google_rating is not None:
            item["aggregateRating"] = {
                "@type":       "AggregateRating",
                "ratingValue": g.google_rating,
                "ratingCount": g.google_review_count or 1,
            }
        items.append(item)

    data = {
        "@context":        "https://schema.org",
        "@type":           "ItemList",
        "name":            ARTICLE_TITLE,
        "numberOfItems":   len(groups),
        "itemListElement": items,
    }
    return (
        '<script type="application/ld+json">\n'
        + json.dumps(data, ensure_ascii=False, indent=2)
        + "\n</script>"
    )


# ============================================================
# データ読み込み
# ============================================================
def _load_places_map() -> dict:
    try:
        df = pd.read_csv(PLACES_CSV)
    except FileNotFoundError:
        return {}
    result = {}
    for _, row in df.iterrows():
        t = safe_str(row.get("studio_title", ""))
        if t:
            result[t] = {
                "rating":       safe_float(row.get("rating")),
                "review_count": safe_int(row.get("review_count")),
            }
    return result


def _load_desc_map() -> dict:
    try:
        df = pd.read_csv(DESC_CSV)
    except FileNotFoundError:
        return {}
    result = {}
    for _, row in df.iterrows():
        t = safe_str(row.get("studio_title", ""))
        if t:
            result[t] = {
                "reviews_text":         safe_str(row.get("reviews_text", "")),
                "review_count_fetched": safe_int(row.get("review_count_fetched")) or 0,
            }
    return result


def _load_details_map() -> dict:
    """studio_site_details.csv → {studio_title: {field: value}} マップを返す"""
    try:
        df = pd.read_csv(DETAILS_CSV)
    except FileNotFoundError:
        return {}
    result = {}
    for _, row in df.iterrows():
        t = safe_str(row.get("studio_title", ""))
        status = safe_str(row.get("fetch_status", ""))
        if not t or status != "ok":
            continue
        result[t] = {
            "area_sqm":          safe_float(row.get("area_sqm")),
            "ceiling_m":         safe_float(row.get("ceiling_height_m")),
            "capacity":          safe_int(row.get("capacity")),
            "floor_type":        safe_str(row.get("floor_type", "")),
            "has_mirror":        _safe_bool(row.get("has_mirror")),
            "has_speaker":       _safe_bool(row.get("has_speaker")),
            "has_tripod":        _safe_bool(row.get("has_tripod")),
            "has_shower":        _safe_bool(row.get("has_shower")),
            "has_changing_room": _safe_bool(row.get("has_changing_room")),
            "hourly_rate":       safe_int(row.get("hourly_rate_yen")),
            "dancer_summary":    safe_str(row.get("dancer_summary", "")),
        }
    return result


def _pick_best_rating(titles: list[str], pmap: dict) -> tuple:
    candidates = []
    for t in titles:
        d = pmap.get(t, {})
        r, rc = d.get("rating"), d.get("review_count")
        if r is not None and r >= 3.0 and rc and rc >= 3:
            candidates.append((r, rc, t))
    if candidates:
        candidates.sort(key=lambda x: (x[1], x[0]), reverse=True)
        return candidates[0]
    for t in titles:
        d = pmap.get(t, {})
        r, rc = d.get("rating"), d.get("review_count")
        if r is not None:
            return r, rc, t
    return None, None, ""


def _pick_best_reviews(titles: list[str], dmap: dict) -> dict:
    best = {"reviews_text": "", "review_count_fetched": 0, "title": ""}
    for t in titles:
        d  = dmap.get(t, {})
        rc = d.get("review_count_fetched", 0)
        if rc > best["review_count_fetched"] and d.get("reviews_text"):
            best = {"reviews_text": d["reviews_text"], "review_count_fetched": rc, "title": t}
    return best


def load_excluded_studios() -> set[str]:
    """
    check_business_status.py が出力した除外CSVを読み込み、
    studio_title の集合を返す。ファイルがない場合は空セット。
    """
    p = Path(EXCLUDED_CSV)
    if not p.exists():
        return set()
    try:
        df = pd.read_csv(p)
        titles = set()
        for _, row in df.iterrows():
            t = safe_str(row.get("studio_title", ""))
            if t:
                titles.add(t)
        return titles
    except Exception:
        return set()


def load_url_alert_map() -> dict[str, str]:
    """
    check_official_urls.py が出力したレポートCSVを読み込み、
    url → alert_level のマップを返す。ファイルがない場合は空dict。
    """
    p = Path(URL_CHECK_REPORT)
    if not p.exists():
        return {}
    try:
        df = pd.read_csv(p, dtype=str).fillna("")
        return {
            str(r["url"]).strip(): str(r["alert_level"]).strip()
            for _, r in df.iterrows()
            if r.get("url") and r.get("alert_level")
        }
    except Exception:
        return {}


def _check_address_quality(address: str) -> bool:
    """
    住所に疑わしい文字列が含まれていないか確認。
    True=OK, False=要確認
    """
    if not address:
        return False
    if "?" in address or "？" in address:
        return False
    # 丁目・番地がなく10文字未満は不完全の可能性
    if len(address) < 12 and not re.search(r"[0-9０-９]", address):
        return False
    return True


def _display_address(addr: str, quality_ok: bool) -> str:
    """
    住所の表示用文字列を返す。
    文字化け(?)が検出された場合は ? を除去し、要確認の注記を付ける。
    """
    if quality_ok or not addr:
        return addr
    cleaned = re.sub(r"\?+", "", addr).strip()
    note = (' <span class="addr-warn" title="住所に文字化けが検出されました。'
            '正確な住所は公式サイトでご確認ください。">※要確認</span>')
    return cleaned + note


def load_groups() -> list[Group]:
    df         = pd.read_csv(STATION_CSV)
    master     = pd.read_csv(STUDIO_CSV)
    places_map  = _load_places_map()
    desc_map    = _load_desc_map()
    details_map = _load_details_map()

    master_map: dict[str, pd.Series] = {}
    for _, row in master.iterrows():
        t = safe_str(row.get("タイトル", ""))
        if t:
            master_map[t] = row

    excluded_titles = load_excluded_studios()
    if excluded_titles:
        import logging as _log
        _log.getLogger(__name__).warning(
            f"除外スタジオ {len(excluded_titles)} 件: {excluded_titles}"
        )

    url_alert_map = load_url_alert_map()
    if url_alert_map:
        import logging as _log2
        _log2.getLogger(__name__).info(
            f"URL チェックレポート読み込み: {len(url_alert_map)} 件"
        )

    groups: list[Group] = []
    seen_titles: set[str] = set()  # STATION_CSV で処理済みタイトル（除外含む）
    for _, row in df.sort_values("rank").iterrows():
        rank       = int(row["rank"])
        rep_title  = safe_str(row["studio_title"])
        seen_titles.add(rep_title)
        group_key  = safe_str(row.get("group_key", "")) or rep_title
        is_partner = bool(safe_int(row.get("is_partner", 0)))
        dslurl_rep = safe_str(row.get("dslurl", ""))
        address    = safe_str(row.get("address", ""))
        primary_station = safe_str(row.get("primary_station", ""))
        distance_m      = safe_float(row.get("distance_to_article_station_m"))
        website         = safe_str(row.get("website", ""))
        rental_fit      = safe_int(row.get("rental_fit_score")) or 0

        # 閉業スタジオを除外
        if rep_title in excluded_titles:
            continue

        branches_raw  = safe_str(row.get("branches", ""))
        branch_titles = (
            [b.strip() for b in branches_raw.split("|") if b.strip()]
            if branches_raw else []
        )
        # 支店名も seen_titles に登録し、補欠候補として再登場しないようにする
        seen_titles.update(branch_titles)

        dslurls: dict[str, str] = {}
        if dslurl_rep:
            dslurls[rep_title] = dslurl_rep

        branch_infos: list[BranchInfo] = []
        for bt in branch_titles:
            mrow = master_map.get(bt)
            if mrow is not None:
                bd  = safe_str(mrow.get("dslurl", ""))
                bw  = safe_str(mrow.get("Webサイト", ""))
                ba  = safe_str(mrow.get("住所", ""))
                bst = safe_str(mrow.get("primary_station", ""))
                bdm = safe_float(mrow.get("distance_to_primary_station_m"))
                if bd:
                    dslurls[bt] = bd
                info = BranchInfo(title=bt, address=ba, primary_station=bst,
                                  distance_m=bdm, dslurl=bd, website=bw)
            else:
                info = BranchInfo(title=bt)
            branch_infos.append(info)

        all_titles = [rep_title] + branch_titles
        best_r, best_rc, _ = _pick_best_rating(all_titles, places_map)
        best_rev = _pick_best_reviews(all_titles, desc_map)

        addr_ok = _check_address_quality(address)
        if not addr_ok:
            import logging as _log
            _log.getLogger(__name__).warning(
                f"[住所品質] {rep_title}: 住所に要確認文字列あり → {address!r}"
            )

        # 公式URLのアラートレベル確認
        url_suppressed = False
        official_url   = website
        if website and url_alert_map:
            url_level = url_alert_map.get(website, "")
            if url_level == "ERROR":
                import logging as _log3
                _log3.getLogger(__name__).warning(
                    f"[URL ERROR] {rep_title}: {website} → 公式URLを非表示"
                )
                official_url   = ""
                url_suppressed = True

        # 公式URL無効かつ非提携スタジオは掲載除外
        if url_suppressed and not is_partner:
            import logging as _log4
            _log4.getLogger(__name__).warning(
                f"[除外] {rep_title}: 公式URL無効かつ非提携"
            )
            continue

        sd = details_map.get(rep_title, {})
        groups.append(Group(
            rank=rank, group_key=group_key, rep_title=rep_title,
            is_partner=is_partner, dslurls=dslurls,
            address=address, primary_station=primary_station,
            distance_m=distance_m, official_url=official_url,
            branches=branch_infos,
            google_rating=best_r, google_review_count=best_rc,
            reviews_text=best_rev["reviews_text"],
            review_count_fetched=best_rev["review_count_fetched"],
            desc_src_title=best_rev["title"],
            rental_fit_score=rental_fit,
            low_rating_suppressed=should_hide_rating(best_r, best_rc),
            address_quality_ok=addr_ok,
            url_suppressed=url_suppressed,
            site_area_sqm=sd.get("area_sqm"),
            site_ceiling_m=sd.get("ceiling_m"),
            site_capacity=sd.get("capacity"),
            site_floor_type=sd.get("floor_type", ""),
            site_has_mirror=sd.get("has_mirror"),
            site_has_speaker=sd.get("has_speaker"),
            site_has_tripod=sd.get("has_tripod"),
            site_has_shower=sd.get("has_shower"),
            site_has_changing_room=sd.get("has_changing_room"),
            site_hourly_rate=sd.get("hourly_rate"),
            site_dancer_summary=sd.get("dancer_summary", ""),
        ))

    # ── 補欠候補で不足分を補充 ──────────────────────────────────────
    import logging as _logr
    _logger = _logr.getLogger(__name__)
    if len(groups) < TARGET_GROUP_COUNT and Path(CANDIDATES_CSV).exists():
        reserve_df = pd.read_csv(CANDIDATES_CSV)
        reserve_df = reserve_df[~reserve_df["studio_title"].isin(seen_titles)]
        reserve_df = reserve_df.sort_values("selection_score", ascending=False)

        for _, crow in reserve_df.iterrows():
            if len(groups) >= TARGET_GROUP_COUNT:
                break

            rep_title = safe_str(crow.get("studio_title", ""))
            if not rep_title or rep_title in excluded_titles:
                continue

            is_partner  = bool(safe_int(crow.get("is_partner", 0)))
            dslurl_rep  = safe_str(crow.get("dslurl", ""))
            address     = safe_str(crow.get("address", ""))
            primary_station = safe_str(crow.get("primary_station", ""))
            distance_m  = safe_float(crow.get("distance_to_article_station_m"))
            rental_fit  = safe_int(crow.get("rental_fit_score")) or 0

            mrow = master_map.get(rep_title)
            website = safe_str(mrow.get("Webサイト", "")) if mrow is not None else ""

            url_suppressed = False
            official_url   = website
            if website and url_alert_map:
                if url_alert_map.get(website, "") == "ERROR":
                    _logger.warning(f"[URL ERROR] {rep_title}: {website} → 公式URLを非表示")
                    official_url   = ""
                    url_suppressed = True

            if url_suppressed and not is_partner:
                _logger.warning(f"[除外] {rep_title}: 公式URL無効かつ非提携")
                continue

            dslurls: dict[str, str] = {}
            if dslurl_rep:
                dslurls[rep_title] = dslurl_rep

            all_titles = [rep_title]
            best_r, best_rc, _ = _pick_best_rating(all_titles, places_map)

            # 非提携 + 口コミ1件のみ: step3 と同じ方針で補欠でも除外
            if not is_partner and best_rc is not None and 0 < best_rc <= 1:
                _logger.warning(f"[補欠除外] {rep_title}: 非提携・口コミ{best_rc}件のみ")
                continue

            best_rev = _pick_best_reviews(all_titles, desc_map)
            addr_ok  = _check_address_quality(address)
            if not addr_ok:
                _logger.warning(f"[住所品質] {rep_title}: 住所に要確認文字列あり → {address!r}")

            sd = details_map.get(rep_title, {})
            new_rank = len(groups) + 1
            _logger.info(f"[補充] {rep_title} を rank={new_rank} で追加 (selection_score={crow.get('selection_score')})")
            groups.append(Group(
                rank=new_rank, group_key=rep_title, rep_title=rep_title,
                is_partner=is_partner, dslurls=dslurls,
                address=address, primary_station=primary_station,
                distance_m=distance_m, official_url=official_url,
                branches=[],
                google_rating=best_r, google_review_count=best_rc,
                reviews_text=best_rev["reviews_text"],
                review_count_fetched=best_rev["review_count_fetched"],
                desc_src_title=best_rev["title"],
                rental_fit_score=rental_fit,
                low_rating_suppressed=should_hide_rating(best_r, best_rc),
                address_quality_ok=addr_ok,
                url_suppressed=url_suppressed,
                site_area_sqm=sd.get("area_sqm"),
                site_ceiling_m=sd.get("ceiling_m"),
                site_capacity=sd.get("capacity"),
                site_floor_type=sd.get("floor_type", ""),
                site_has_mirror=sd.get("has_mirror"),
                site_has_speaker=sd.get("has_speaker"),
                site_has_tripod=sd.get("has_tripod"),
                site_has_shower=sd.get("has_shower"),
                site_has_changing_room=sd.get("has_changing_room"),
                site_hourly_rate=sd.get("hourly_rate"),
                site_dancer_summary=sd.get("dancer_summary", ""),
            ))

    # 最終ランクを 1..N で振り直す
    for i, g in enumerate(groups, 1):
        g.rank = i

    return groups


# ============================================================
# HTML後処理: 自動修正 + 警告
# ============================================================
def sanitize_html(html: str) -> tuple[str, list[str]]:
    """
    既知の不自然表現を自動修正し、残った問題を警告リストで返す。
    Returns: (fixed_html, warnings)
    """
    fixed    = html
    warnings = []

    for pattern, replacement in AUTO_FIX_PATTERNS:
        new = re.sub(pattern, replacement, fixed)
        if new != fixed:
            warnings.append(f"[auto-fix] {pattern!r}")
            fixed = new

    for pattern, label in WARN_PATTERNS:
        if re.search(pattern, fixed):
            warnings.append(f"[warn] {label}")

    for kw in FORBIDDEN:
        if kw in fixed:
            warnings.append(f"[ERROR] 禁止語: {kw!r}")

    return fixed, warnings


# ============================================================
# main
# ============================================================
def _make_dynamic_titles(count: int) -> tuple[str, str, str]:
    """
    掲載件数に応じてタイトル・h1・メタ概要を返す。
    count >= 15 → "おすすめ15選｜ダンサー目線..."
    10-14       → "おすすめN選"
    < 10        → "周辺で使えるN件"
    Returns: (page_title, article_title, meta_desc)
    """
    if count >= 15:
        article_title = f"{STATION_NAME}のレンタルダンススタジオおすすめ15選"
        page_title    = (
            f"{STATION_NAME}のレンタルダンススタジオおすすめ15選｜"
            f"ダンサー目線で選ぶ駅近スタジオ"
        )
        meta_desc = (
            f"ダンサー目線で選んだ{STATION_NAME}周辺のレンタルダンススタジオ15選。"
            f"駅からの距離・口コミ評価・予約導線を比較して、"
            f"個人練習やグループ練習に最適なスタジオを探せます。"
        )
    elif count >= 10:
        article_title = f"{STATION_NAME}のレンタルダンススタジオおすすめ{count}選"
        page_title    = (
            f"{STATION_NAME}のレンタルダンススタジオおすすめ{count}選｜"
            f"駅近・個人練習・グループ練習向け"
        )
        meta_desc = (
            f"{STATION_NAME}周辺で、ダンス練習に使いやすいレンタルスタジオを{count}選紹介。"
            f"駅からの距離、口コミ評価、予約導線を比較できます。"
        )
    else:
        article_title = f"{STATION_NAME}周辺で使えるレンタルダンススタジオ{count}件"
        page_title    = article_title
        meta_desc = (
            f"{STATION_NAME}周辺のレンタルダンススタジオ{count}件を紹介。"
            f"個人練習・グループ練習に活用できる情報をまとめています。"
        )
    return page_title, article_title, meta_desc


def main():
    parser = argparse.ArgumentParser(description="池袋駅ダンススタジオ記事生成")
    parser.add_argument(
        "--filename-mode",
        default="ja",
        choices=["ja", "slug", "both"],
        help="ja=日本語ファイル名(default), slug=英字スラッグ, both=両方出力",
    )
    args = parser.parse_args()

    groups     = load_groups()
    search_url = build_search_url()

    # 件数に応じた動的タイトル
    page_title, article_title, meta_desc = _make_dynamic_titles(len(groups))

    # h1 はモジュールレベルの ARTICLE_TITLE を動的に置き換える
    global ARTICLE_TITLE
    ARTICLE_TITLE = article_title

    body_parts = [
        EXTRA_CSS,
        make_h1(),
        make_intro(len(groups)),
        make_toc(groups),
        make_comparison_table(groups),
    ]
    for g in groups:
        body_parts.append(make_studio_card(g))
    body_parts += [
        make_selection_points(),
        make_area_section(groups),
        make_equipment_section(),
        make_summary(groups, search_url),
    ]
    faq_html, faqs = make_faq()
    body_parts.append(faq_html)

    body_html       = "\n".join(body_parts)
    faq_jsonld      = make_faq_jsonld(faqs)
    itemlist_jsonld = make_itemlist_jsonld(groups)

    html = build_html(page_title, meta_desc, body_html, faq_jsonld, itemlist_jsonld,
                      sidebar_html=make_sidebar())

    # 後処理
    html, warnings = sanitize_html(html)

    # 出力
    output_paths = []
    if args.filename_mode == "both":
        output_paths = [make_output_path("ja"), make_output_path("slug")]
    else:
        output_paths = [make_output_path(args.filename_mode)]

    for path in output_paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(html, encoding="utf-8")

    # ============================================================
    # 検証レポート
    # ============================================================
    branch_groups      = [g for g in groups if g.branches]
    dslurl_cta_count   = sum(1 for g in groups if g.is_partner and g.dslurls)
    with_reviews       = [g for g in groups if g.review_count_fetched > 0]
    with_rating        = [g for g in groups if g.google_rating is not None]
    rating_suppressed  = [g for g in groups if g.low_rating_suppressed]
    addr_warn          = [g for g in groups if not g.address_quality_ok]
    url_suppressed_gs  = [g for g in groups if g.url_suppressed]
    excluded_count     = len(load_excluded_studios())
    url_report_exists  = Path(URL_CHECK_REPORT).exists()

    print("\n[検証レポート]")
    for p in output_paths:
        print(f"生成ファイル          : {p}")
    print(f"h1タイトル            : {ARTICLE_TITLE}")
    print(f"掲載主体数            : {len(groups)}")
    print(f"除外済みスタジオ      : {excluded_count} 件 ({EXCLUDED_CSV})")
    print(f"branch付きグループ    : {len(branch_groups)}")
    print(f"dslurlありCTA数       : {dslurl_cta_count}")
    print(f"口コミテキストあり    : {len(with_reviews)}主体")
    print(f"Google評価あり        : {len(with_rating)}主体")
    print(f"評価非表示(低スコア)  : {len(rating_suppressed)}主体")
    if rating_suppressed:
        for g in rating_suppressed:
            print(f"  - {get_display_name(g)} (評価{g.google_rating}, {g.google_review_count}件)")
    print(f"住所品質警告          : {len(addr_warn)}主体")
    if addr_warn:
        for g in addr_warn:
            print(f"  - {get_display_name(g)}: {g.address!r}")
    url_report_label = "あり" if url_report_exists else "なし (check_official_urls.py 未実行)"
    print(f"URLチェックレポート   : {url_report_label}")
    print(f"URL非表示(ERROR)      : {len(url_suppressed_gs)}主体")
    if url_suppressed_gs:
        for g in url_suppressed_gs:
            print(f"  - {get_display_name(g)}: URL非表示 (dslurl fallback)")
    print(f"CTA検索URL            : {search_url}")
    print(f"関連記事リンク数      : {len(RELATED_ARTICLES)}")
    print(f"FAQPage JSON-LD       : あり")
    print(f"ItemList JSON-LD      : あり")

    # キーワード出現チェック
    check_kws = {
        "ありますため": "不自然表現",
        "口コミ情報は多くないため": "繰り返し文",
        "口コミ少なめ": "ネガティブタグ",
        "詳細↓": "古いCTA",
        "詳細・予約": "古いCTA",
        "Dance Studio Lab": "DSL名称",
        "提携スタジオ": "提携明示",
        "STUDIO COCORO（スタジオ": "長いスタジオ名",
        "レンタット": "誤字",
    }
    print()
    print("[コンテンツチェック]")
    all_ok = True
    for kw, label in check_kws.items():
        cnt = html.count(kw)
        mark = "✓" if cnt == 0 else f"× ({cnt}回) ← {label}"
        print(f"  {kw[:30]:30s}: {mark}")
        if cnt > 0:
            all_ok = False

    if warnings:
        print()
        print("[sanitize ログ]")
        for w in warnings:
            print(f"  {w}")

    print()
    if all_ok and not any("[ERROR]" in w for w in warnings):
        print("✓ すべてのチェックが通過しました。")
    else:
        print("⚠ 一部チェックに引っかかりました。上記を確認してください。")


if __name__ == "__main__":
    main()
