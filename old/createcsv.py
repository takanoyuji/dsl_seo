# -*- coding: utf-8 -*-
# CSVにエリア・駅・タグを付与するスクリプト

from datetime import datetime

import numpy as np
import pandas as pd
from tqdm import tqdm

# =========================
# 設定
# =========================
INPUT_FILE = "studioall.csv"
STATION_FILE = "m_station.csv"
OUTPUT_FILE = "studio_with_tags.csv"
MAX_STATIONS = 3
STATION_RADIUS_M = 1200

# 区 → エリア変換
AREA_MAP = {
    # 23区
    "千代田区": "秋葉原",
    "中央区": "銀座",
    "港区": "六本木",
    "新宿区": "新宿",
    "文京区": "後楽園",
    "台東区": "上野",
    "墨田区": "錦糸町",
    "江東区": "豊洲",
    "品川区": "品川",
    "目黒区": "中目黒",
    "大田区": "蒲田",
    "世田谷区": "下北沢",
    "渋谷区": "渋谷",
    "中野区": "中野",
    "杉並区": "高円寺",
    "豊島区": "池袋",
    "北区": "赤羽",
    "荒川区": "日暮里",
    "板橋区": "成増",
    "練馬区": "練馬",
    "足立区": "北千住",
    "葛飾区": "新小岩",
    "江戸川区": "西葛西",

    # 市部
    "八王子市": "八王子",
    "立川市": "立川",
    "武蔵野市": "吉祥寺",
    "三鷹市": "三鷹",
    "府中市": "府中",
    "調布市": "調布",
    "町田市": "町田",
    "小金井市": "武蔵小金井",
    "小平市": "小平",
    "日野市": "日野",
    "東村山市": "東村山",
    "国分寺市": "国分寺",
    "国立市": "国立",
    "福生市": "福生",
    "狛江市": "狛江",
    "東大和市": "東大和",
    "清瀬市": "清瀬",
    "東久留米市": "東久留米",
    "武蔵村山市": "武蔵村山",
    "多摩市": "多摩センター",
    "稲城市": "稲城",
    "羽村市": "羽村",
    "あきる野市": "秋川",
    "西東京市": "ひばりヶ丘",
    "青梅市": "青梅",

    # 住所揺れ・例外で拾いたいもの
    "東京23区": "東京",
    "東京都": "東京"
}

# wardごとの候補エリア（未定義はprimary_areaのみを使う）
CANDIDATE_AREAS_MAP = {
    "新宿区": ["新宿", "西新宿", "高田馬場", "新大久保"],
    "渋谷区": ["渋谷", "恵比寿", "代官山", "原宿"],
    "豊島区": ["池袋", "目白", "大塚", "巣鴨"],
    "港区": ["六本木", "赤坂", "麻布", "新橋"],
    "中央区": ["銀座", "日本橋", "八丁堀", "月島"],
    "台東区": ["上野", "浅草", "御徒町", "蔵前"],
    "品川区": ["品川", "五反田", "大井町", "目黒"],
    "世田谷区": ["下北沢", "三軒茶屋", "二子玉川", "自由が丘"],
}

# =========================
# 処理
# =========================

def read_input_csv(path):
    # UTF-8系→日本語Windows系→UTF-16系の順で再試行
    for enc in ("utf-8", "utf-8-sig", "cp932", "shift_jis", "utf-16", "utf-16le", "utf-16be"):
        try:
            return pd.read_csv(path, encoding=enc)
        except UnicodeDecodeError:
            continue
    raise ValueError(f"CSVの文字コードを判定できませんでした: {path}")


def is_zip_excel(path):
    try:
        with open(path, "rb") as f:
            return f.read(2) == b"PK"
    except OSError:
        return False


def first_existing_column(df_obj, candidates):
    for col in candidates:
        if col in df_obj.columns:
            return col
    return None


def drop_empty_columns(df_obj):
    """末尾カンマ由来の Unnamed 列と、全行欠損の列を除去する。"""
    col_str = df_obj.columns.astype(str)
    mask = ~col_str.str.startswith("Unnamed")
    out = df_obj.loc[:, mask]
    return out.dropna(axis=1, how="all")


def resolve_lat_lng_columns(df_obj, lat_col, lng_col):
    """
    列名と中身が逆のCSVに対応する。
    東京付近では緯度~35、経度~139。「latitude」列が130台なら実際は経度。
    戻り値: (実際の緯度が入っている列名, 実際の経度が入っている列名)
    """
    a = pd.to_numeric(df_obj[lat_col], errors="coerce")
    b = pd.to_numeric(df_obj[lng_col], errors="coerce")
    med_a, med_b = a.median(), b.median()
    if pd.isna(med_a) or pd.isna(med_b):
        return lat_col, lng_col
    # 名ばかりlatitudeの中身が経度（日本 130台）、名ばかりlongitudeの中身が緯度（35台）
    if med_a > 50 and med_b < 50:
        return lng_col, lat_col
    if abs(med_a) > 90 and abs(med_b) <= 90:
        return lng_col, lat_col
    return lat_col, lng_col


def haversine_distance_m(lat1, lng1, lat2, lng2):
    # 入力: 度, 出力: メートル
    r = 6371000.0
    phi1 = np.radians(lat1)
    phi2 = np.radians(lat2)
    d_phi = np.radians(lat2 - lat1)
    d_lam = np.radians(lng2 - lng1)
    a = np.sin(d_phi / 2.0) ** 2 + np.cos(phi1) * np.cos(phi2) * np.sin(d_lam / 2.0) ** 2
    c = 2.0 * np.arctan2(np.sqrt(a), np.sqrt(1.0 - a))
    return r * c


def load_station_master(path):
    if is_zip_excel(path):
        try:
            station_df = pd.read_excel(path)
        except ImportError as e:
            raise ImportError(
                "駅マスタがExcel形式です。`pip install openpyxl` を実行してください。"
            ) from e
    else:
        station_df = read_input_csv(path)

    name_col = first_existing_column(station_df, ["station_name", "駅名", "name"])
    lat_col = first_existing_column(station_df, ["latitude", "lat", "緯度"])
    lng_col = first_existing_column(station_df, ["longitude", "lng", "経度"])
    if not name_col or not lat_col or not lng_col:
        raise ValueError(
            "m_station.csv の列が不足しています。"
            " station_name / latitude / longitude（または同義列）を用意してください。"
        )

    station_df = station_df[[name_col, lat_col, lng_col]].copy()
    station_df.columns = ["station_name", "latitude", "longitude"]
    station_df["latitude"] = pd.to_numeric(station_df["latitude"], errors="coerce")
    station_df["longitude"] = pd.to_numeric(station_df["longitude"], errors="coerce")
    station_df = station_df.dropna(subset=["station_name", "latitude", "longitude"]).reset_index(drop=True)
    return station_df


df = read_input_csv(INPUT_FILE)
df = drop_empty_columns(df)
stations_df = load_station_master(STATION_FILE)

ADDRESS_COL = first_existing_column(df, ["住所", "address"])
HOURS_COL = first_existing_column(df, ["営業時間", "hours"])
RENTAL_COL = first_existing_column(df, ["rental", "レンタル"])
PREF_COL = first_existing_column(df, ["pref", "都道府県"])
LAT_COL = first_existing_column(df, ["latitude", "lat", "緯度"])
LNG_COL = first_existing_column(df, ["longitude", "lng", "経度"])

if not ADDRESS_COL:
    raise ValueError("住所列が見つかりません。'住所' または 'address' 列を用意してください。")
if not LAT_COL or not LNG_COL:
    raise ValueError("緯度経度列が見つかりません。'latitude/longitude' または同義列を用意してください。")
if not RENTAL_COL:
    raise ValueError("出力絞り込みのため rental 列が必要です。")
if not PREF_COL:
    raise ValueError("出力絞り込みのため pref 列が必要です。")

df[LAT_COL] = pd.to_numeric(df[LAT_COL], errors="coerce")
df[LNG_COL] = pd.to_numeric(df[LNG_COL], errors="coerce")
LAT_COL, LNG_COL = resolve_lat_lng_columns(df, LAT_COL, LNG_COL)

# 出力は rental=1 かつ pref=東京 のみ
rental_one = pd.to_numeric(df[RENTAL_COL], errors="coerce").eq(1)
pref_tokyo = df[PREF_COL].astype(str).str.strip().eq("東京")
df = df[rental_one & pref_tokyo].copy().reset_index(drop=True)


def extract_ward(address):
    for ward in AREA_MAP.keys():
        if ward in str(address):
            return ward
    return "その他"

def map_area(ward):
    return AREA_MAP.get(ward, "その他")


def build_candidate_areas(ward, primary_area):
    if primary_area == "その他":
        return "その他"
    areas = CANDIDATE_AREAS_MAP.get(ward, [primary_area])
    # 重複除去しつつ順序維持
    uniq = list(dict.fromkeys([a for a in areas if a]))
    return "|".join(uniq)


def is_rental_value(value):
    text = str(value).strip().lower()
    return text in {"1", "true", "yes", "y", "レンタル可", "可"}


def nearest_stations(lat, lng, station_lat_arr, station_lng_arr, station_name_arr):
    if pd.isna(lat) or pd.isna(lng):
        return "なし", "", "", 0

    distances = haversine_distance_m(lat, lng, station_lat_arr, station_lng_arr)
    within_idx = np.where(distances <= STATION_RADIUS_M)[0]
    if len(within_idx) == 0:
        return "なし", "", "", 0

    sorted_idx = within_idx[np.argsort(distances[within_idx])]
    top_idx = sorted_idx[:MAX_STATIONS]

    names = [str(station_name_arr[i]).strip() for i in top_idx if str(station_name_arr[i]).strip()]
    names = list(dict.fromkeys(names))
    if not names:
        return "なし", "", "", 0

    primary_station = names[0]
    primary_distance = int(round(float(distances[top_idx[0]])))
    candidate_stations = "|".join(names)
    station_count = len(names)
    return primary_station, primary_distance, candidate_stations, station_count


station_lat_arr = stations_df["latitude"].to_numpy(dtype=float)
station_lng_arr = stations_df["longitude"].to_numpy(dtype=float)
station_name_arr = stations_df["station_name"].to_numpy(dtype=str)

tqdm.pandas(desc="stations")
station_result = df.progress_apply(
    lambda row: nearest_stations(
        row[LAT_COL],
        row[LNG_COL],
        station_lat_arr,
        station_lng_arr,
        station_name_arr,
    ),
    axis=1,
)
station_result_df = pd.DataFrame(
    station_result.tolist(),
    columns=[
        "primary_station",
        "distance_to_primary_station_m",
        "candidate_stations",
        "station_count",
    ],
    index=df.index,
)
df = pd.concat([df, station_result_df], axis=1)


def build_tags(row):
    tags = []

    if row["primary_area"] != "その他":
        tags.append(f"area:{row['primary_area']}")

    if row["candidate_stations"]:
        for station in str(row["candidate_stations"]).split("|"):
            station = station.strip()
            if station:
                tags.append(f"station:{station}")

    if not RENTAL_COL or is_rental_value(row.get(RENTAL_COL, "")):
        tags.append("type:レンタル")

    if HOURS_COL and "24" in str(row.get(HOURS_COL, "")):
        tags.append("feature:24h")

    return "|".join(tags)

# ward作成
df["ward"] = df[ADDRESS_COL].apply(extract_ward)

# area作成
df["primary_area"] = df["ward"].apply(map_area)

# candidate_areas作成
df["candidate_areas"] = df.apply(
    lambda row: build_candidate_areas(row["ward"], row["primary_area"]),
    axis=1
)

# タグ生成
df["article_tags"] = df.apply(build_tags, axis=1)

# 保存
try:
    df.to_csv(OUTPUT_FILE, index=False, encoding="utf-8-sig")
    saved_file = OUTPUT_FILE
except PermissionError:
    # 出力先をExcel等で開いている場合のフォールバック
    saved_file = f"studio_with_tags_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    df.to_csv(saved_file, index=False, encoding="utf-8-sig")

print("完了:", saved_file)