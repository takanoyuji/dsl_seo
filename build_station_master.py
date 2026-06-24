#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_station_master.py
駅データ.jp の CSV から全国駅マスタ m_station.csv を生成する

入力 (data/master/駅データ/):
  station20260430free.csv
  line20260409free.csv
  company20260409.csv

出力:
  data/master/m_station.csv
"""

import re
import math
import logging
import pandas as pd
from pathlib import Path

# ============================================================
# 設定
# ============================================================
DATA_DIR    = Path("data/master/駅データ")
OUTPUT_PATH = Path("data/master/m_station.csv")

STATION_CSV = DATA_DIR / "station20260430free.csv"
LINE_CSV    = DATA_DIR / "line20260409free.csv"
COMPANY_CSV = DATA_DIR / "company20260409.csv"

# area グループ化: この距離(m)以内の B/C ランク駅は上位ランク駅の area を継承
# ※ S/A ランク駅は AREA_OVERRIDES に明示しない限り自分の station_name を area にする
AREA_PROXIMITY_M = 1000

# D ランク駅は除外（出力対象: S/A/B/C のみ）
OUTPUT_RANKS = {"S", "A", "B", "C"}

# 手動 area オーバーライド (station_name → area)
# 元の 49 駅マスタのマッピングを引き継ぐ + 必要な追加分
AREA_OVERRIDES: dict[str, str] = {
    # 元マスタ (49 駅) のうち area ≠ station_name のもの
    "西武新宿":    "西新宿",
    "大崎":        "品川",
    "田町":        "品川",
    "青山一丁目":  "青山",
    "外苑前":      "青山",
    "有楽町":      "銀座",
}

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
# 都道府県コード → 都道府県名
# ============================================================
PREF_MAP: dict[int, str] = {
    1: "北海道", 2: "青森県", 3: "岩手県", 4: "宮城県", 5: "秋田県",
    6: "山形県", 7: "福島県", 8: "茨城県", 9: "栃木県", 10: "群馬県",
    11: "埼玉県", 12: "千葉県", 13: "東京都", 14: "神奈川県", 15: "新潟県",
    16: "富山県", 17: "石川県", 18: "福井県", 19: "山梨県", 20: "長野県",
    21: "岐阜県", 22: "静岡県", 23: "愛知県", 24: "三重県", 25: "滋賀県",
    26: "京都府", 27: "大阪府", 28: "兵庫県", 29: "奈良県", 30: "和歌山県",
    31: "鳥取県", 32: "島根県", 33: "岡山県", 34: "広島県", 35: "山口県",
    36: "徳島県", 37: "香川県", 38: "愛媛県", 39: "高知県", 40: "福岡県",
    41: "佐賀県", 42: "長崎県", 43: "熊本県", 44: "大分県", 45: "宮崎県",
    46: "鹿児島県", 47: "沖縄県",
}

# 都道府県庁所在地（駅名と一致する可能性があるもの）
PREF_CAPITALS: set[str] = {
    "札幌", "青森", "盛岡", "仙台", "秋田", "山形", "福島",
    "水戸", "宇都宮", "前橋", "さいたま", "千葉", "新宿", "横浜",
    "新潟", "富山", "金沢", "福井", "甲府", "長野",
    "岐阜", "静岡", "名古屋", "津", "大津", "京都", "大阪",
    "神戸", "奈良", "和歌山", "鳥取", "松江", "岡山", "広島",
    "山口", "徳島", "高松", "松山", "高知", "福岡", "佐賀",
    "長崎", "熊本", "大分", "宮崎", "鹿児島", "那覇",
}

# 新幹線駅（代表的なもの）
SHINKANSEN_STATIONS: set[str] = {
    "東京", "品川", "新横浜", "小田原", "熱海", "三島", "新富士",
    "静岡", "掛川", "浜松", "豊橋", "三河安城", "名古屋", "岐阜羽島",
    "米原", "京都", "新大阪", "新神戸", "西明石", "姫路", "相生",
    "岡山", "新倉敷", "福山", "新尾道", "三原", "東広島", "広島",
    "新岩国", "徳山", "新山口", "厚狭", "新下関", "小倉", "博多",
    "新鳥栖", "久留米", "筑後船小屋", "新大牟田", "新玉名", "熊本",
    "新八代", "新水俣", "出水", "川内", "鹿児島中央",
    "上野", "大宮", "小山", "宇都宮", "那須塩原", "新白河", "郡山",
    "福島", "白石蔵王", "仙台", "古川", "くりこま高原", "一ノ関",
    "水沢江刺", "北上", "新花巻", "盛岡", "いわて沼宮内", "二戸",
    "八戸", "七戸十和田", "新青森", "新函館北斗", "新八戸",
    "高崎", "熊谷", "本庄早稲田", "上毛高原", "越後湯沢", "浦佐",
    "長岡", "燕三条", "新潟",
    "長野", "飯山", "上越妙高", "糸魚川", "黒部宇奈月温泉",
    "富山", "新高岡", "金沢", "芦原温泉", "福井", "越前たけふ", "敦賀",
    "那覇空港", "奥武山公園",
}

# 空港アクセス駅
AIRPORT_STATIONS: set[str] = {
    "新千歳空港", "千歳", "南千歳",
    "青森空港",
    "仙台空港",
    "成田空港", "空港第2ビル", "成田",
    "羽田空港第1・第2ターミナル", "羽田空港第3ターミナル",
    "羽田空港国内線ターミナル", "羽田空港国際線ターミナル",
    "中部国際空港", "常滑",
    "関西空港", "りんくうタウン",
    "伊丹空港",
    "神戸空港",
    "岡山空港",
    "広島空港",
    "高松空港",
    "松山空港",
    "福岡空港",
    "北九州空港",
    "長崎空港",
    "熊本空港",
    "大分空港",
    "宮崎空港",
    "鹿児島空港",
    "那覇空港",
    "新石垣空港",
}

# 主要駅ホワイトリスト
WHITELIST_S: set[str] = {
    "東京", "新宿", "渋谷", "池袋", "品川", "上野", "秋葉原",
    "横浜", "川崎", "大宮", "千葉",
    "名古屋", "大阪", "梅田", "京都", "三ノ宮", "神戸三宮",
    "博多", "天神", "札幌", "仙台", "広島", "岡山",
    "熊本", "鹿児島中央", "那覇空港",
}

WHITELIST_A: set[str] = {
    "吉祥寺", "中野", "高田馬場", "新橋", "有楽町", "銀座",
    "六本木", "恵比寿", "原宿", "表参道", "目黒", "五反田",
    "町田", "立川", "八王子", "北千住", "錦糸町", "蒲田",
    "藤沢", "武蔵小杉", "浦和", "船橋", "柏", "松戸",
    "静岡", "浜松", "金山",
    "なんば", "心斎橋", "天王寺", "京橋", "新大阪",
    "十三", "西宮北口", "姫路",
    "小倉", "長崎", "大分", "宮崎",
}


# ============================================================
# 市区町村抽出
# ============================================================
def parse_city(address: str) -> tuple[str, str]:
    """
    address → (city, ward_or_city)

    city: 市・区・郡町村レベル
    ward_or_city: 政令市区や東京23区など最も細かい行政区

    Returns:
        (city, ward_or_city) どちらも空文字の場合あり
    """
    if not address or pd.isna(address):
        return "", ""

    # 都道府県プレフィックスを除去
    addr = re.sub(r"^(北海道|東京都|京都府|大阪府|.{2,3}[都道府県])", "", str(address))

    # 政令指定都市の区: ○○市○○区
    m = re.match(r"([^\s]+?市)([^\s]+?区)", addr)
    if m:
        city = m.group(1)
        ward = m.group(1) + m.group(2)
        return city, ward

    # 東京23区など: ○○区 で始まる
    m = re.match(r"([^\s]+?区)", addr)
    if m:
        return m.group(1), m.group(1)

    # 通常市
    m = re.match(r"([^\s]+?市)", addr)
    if m:
        return m.group(1), m.group(1)

    # 郡+町村
    m = re.match(r"([^\s]+?郡)([^\s]+?[町村])", addr)
    if m:
        city = m.group(1) + m.group(2)
        return city, city

    # 町・村単体
    m = re.match(r"([^\s]+?[町村])", addr)
    if m:
        return m.group(1), m.group(1)

    return "", ""


# ============================================================
# 重要度スコア
# ============================================================
def calc_importance(
    station_name: str,
    line_count: int,
    prefecture: str,
    ward_or_city: str,
) -> tuple[int, str]:
    """
    (importance_score, importance_rank) を返す
    """
    score = 0

    # 路線数ベーススコア
    if line_count >= 5:
        score += 35
    elif line_count == 4:
        score += 30
    elif line_count == 3:
        score += 24
    elif line_count == 2:
        score += 15
    else:
        score += 5

    # 都道府県庁所在地名と一致
    if station_name in PREF_CAPITALS:
        score += 15

    # 市区町村名と一致
    if ward_or_city:
        city_bare = re.sub(r"[市区町村郡]$", "", ward_or_city)
        if city_bare and city_bare in station_name:
            score += 8

    # 新幹線駅
    if station_name in SHINKANSEN_STATIONS:
        score += 10

    # 空港アクセス駅
    if station_name in AIRPORT_STATIONS:
        score += 10

    score = min(score, 100)

    # ランク
    if station_name in WHITELIST_S:
        rank = "S"
        score = max(score, 80)
    elif station_name in WHITELIST_A:
        rank = "A"
        score = max(score, 60)
    elif score >= 80:
        rank = "S"
    elif score >= 60:
        rank = "A"
    elif score >= 40:
        rank = "B"
    elif score >= 20:
        rank = "C"
    else:
        rank = "D"

    return score, rank


# ============================================================
# area 割り当て
# ============================================================
def strip_eki(name: str) -> str:
    """末尾の「駅」を除去"""
    return name[:-1] if name.endswith("駅") else name


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi    = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def assign_areas(df: pd.DataFrame) -> list[str]:
    """
    各駅に area 名を割り当てる。

    優先順位:
      1. AREA_OVERRIDES に明示されていれば常にそれを使用
      2. S / A ランク駅: 自分の station_name をそのまま area にする
         (高知名度駅は近傍グループ化の対象外)
      3. B / C ランク駅: AREA_PROXIMITY_M 以内に高スコア確定済み駅があれば
         その area を継承
      4. デフォルト: station_name
    """
    n      = len(df)
    areas  = list(df["station_name"])
    ranks  = list(df["importance_rank"])
    fixed  = [False] * n  # True = AREA_OVERRIDES または S/A で確定済み

    # ① AREA_OVERRIDES による上書き
    for i, name in enumerate(df["station_name"]):
        for key in (name, name + "駅", strip_eki(name)):
            if key in AREA_OVERRIDES:
                areas[i] = AREA_OVERRIDES[key]
                fixed[i] = True
                break

    # ② S/A ランク駅は（AREA_OVERRIDESで上書きされていなければ）自身の名前で固定
    for i in range(n):
        if not fixed[i] and ranks[i] in ("S", "A"):
            fixed[i] = True  # areas[i] は既に station_name

    # ③ B/C ランク駅: 近傍グループ化（重要度スコア降順）
    scores = df["importance_score"].to_numpy()
    lats   = df["latitude"].to_numpy()
    lons   = df["longitude"].to_numpy()
    order  = scores.argsort()[::-1]  # 高スコア順インデックス

    confirmed: list[tuple[str, float, float, float]] = []
    # (area, lat, lon, score)

    for i in order:
        if not fixed[i]:
            best_area  = None
            best_score = -1.0
            for ca, clat, clon, cscore in confirmed:
                if cscore <= scores[i]:
                    continue
                dist = haversine_m(lats[i], lons[i], clat, clon)
                if dist <= AREA_PROXIMITY_M and cscore > best_score:
                    best_area  = ca
                    best_score = cscore
            if best_area is not None:
                areas[i] = best_area

        confirmed.append((areas[i], lats[i], lons[i], scores[i]))

    return areas


# ============================================================
# メイン
# ============================================================
def main():
    # --- データ読み込み ---
    log.info("データ読み込み中...")
    df_st   = pd.read_csv(STATION_CSV, dtype={"post": str, "open_ymd": str, "close_ymd": str})
    df_line = pd.read_csv(LINE_CSV)
    df_comp = pd.read_csv(COMPANY_CSV)

    log.info(f"  station.csv: {len(df_st)}行")
    log.info(f"  line.csv:    {len(df_line)}行")
    log.info(f"  company.csv: {len(df_comp)}行")

    # 廃止駅・廃止路線を除外 (e_status=0: 現役, 1:廃止予定, 2:廃止)
    df_st   = df_st[df_st["e_status"] == 0].copy()
    df_line = df_line[df_line["e_status"] == 0].copy()
    df_comp = df_comp[df_comp["e_status"] == 0].copy()

    log.info(f"  現役駅数: {len(df_st)}行")
    log.info(f"  現役路線数: {len(df_line)}行")

    # 路線 → 会社名マッピング
    line_to_company = dict(zip(df_line["line_cd"], df_line["company_cd"]))
    comp_name_map   = dict(zip(df_comp["company_cd"], df_comp["company_name"]))
    line_name_map   = dict(zip(df_line["line_cd"], df_line["line_name"]))

    # station に路線名・会社名を付与
    df_st["line_name"]    = df_st["line_cd"].map(line_name_map)
    df_st["company_name"] = df_st["line_cd"].map(line_to_company).map(comp_name_map)

    # --- station_g_cd でグループ集約 ---
    log.info("station_g_cd で統合中...")

    city_fail = 0

    records = []
    grouped = df_st.groupby("station_g_cd")

    for g_cd, grp in grouped:
        # 代表行: e_sort 最小を代表とする
        rep = grp.sort_values("e_sort").iloc[0]

        name     = rep["station_name"]
        pref_cd  = int(rep["pref_cd"])
        address  = str(rep.get("address", "") or "")
        pref     = PREF_MAP.get(pref_cd, "")

        # 座標: グループ内で全行同座標なら代表値, 異なる場合は平均
        lons = grp["lon"].dropna()
        lats = grp["lat"].dropna()
        if lons.nunique() == 1:
            lon = float(lons.iloc[0])
            lat = float(lats.iloc[0])
        else:
            lon = float(lons.mean())
            lat = float(lats.mean())
            log.debug(f"  [{name}] 座標平均 ({len(lons)}点)")

        # 市区町村
        city, ward = parse_city(address)
        if not city:
            city_fail += 1
            log.debug(f"  市区町村抽出失敗: {address!r}")

        # 路線・会社（重複除去）
        lines     = sorted({str(x) for x in grp["line_name"].dropna() if x})
        companies = sorted({str(x) for x in grp["company_name"].dropna() if x})
        line_count = len(lines)

        # 重要度
        score, rank = calc_importance(name, line_count, pref, ward)

        records.append({
            "station_id":       int(g_cd),
            "station_group_id": int(g_cd),
            "station_name":     name,
            "prefecture":       pref,
            "city":             city,
            "ward_or_city":     ward,
            "address":          address,
            "latitude":         round(lat, 6),
            "longitude":        round(lon, 6),
            "lines":            "|".join(lines),
            "companies":        "|".join(companies),
            "line_count":       line_count,
            "importance_rank":  rank,
            "importance_score": score,
            "source":           "駅データ.jp",
        })

    df_out = pd.DataFrame(records)

    # --- ログ ---
    log.info(f"station_g_cd 統合後: {len(df_out)}駅")
    log.info(f"市区町村抽出失敗: {city_fail}件")

    rank_dist = df_out["importance_rank"].value_counts().sort_index().to_dict()
    log.info(f"importance_rank 分布 (全): {rank_dist}")

    # --- Dランク除外 ---
    df_out = df_out[df_out["importance_rank"].isin(OUTPUT_RANKS)].reset_index(drop=True)
    log.info(f"D ランク除外後: {len(df_out)}駅 (S/A/B/C のみ)")

    # --- area 列割り当て ---
    log.info("area 割り当て中 (AREA_OVERRIDES + S/A固定 + B/C近傍グループ化)...")
    areas = assign_areas(df_out)
    df_out.insert(3, "area", areas)  # station_name の直後に挿入

    # --- 出力 ---
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    df_out.to_csv(OUTPUT_PATH, index=False, encoding="utf-8-sig")
    log.info(f"出力完了: {OUTPUT_PATH} ({len(df_out)}件)")

    # --- サンプル確認 ---
    print("\n" + "=" * 70)
    print("■ S/Aランク + area サンプル")
    print("=" * 70)
    sample = df_out[df_out["importance_rank"].isin(["S", "A"])].sort_values(
        "importance_score", ascending=False
    ).head(25)
    for _, r in sample.iterrows():
        area_flag = " *" if r["area"] != r["station_name"] else ""
        print(f"  [{r['importance_rank']}|{r['importance_score']:3}] "
              f"{r['station_name']:<12} area={r['area']:<12}{area_flag} "
              f"{r['prefecture']}")
    print("=" * 70)
    print()

    # --- 近傍グループ化された駅サンプル ---
    grouped = df_out[df_out["area"] != df_out["station_name"]]
    if len(grouped):
        print(f"■ 近傍グループ化された駅 ({len(grouped)}駅)")
        print(grouped[["station_name", "area", "importance_rank", "prefecture"]].head(20).to_string(index=False))
        print()


if __name__ == "__main__":
    main()
