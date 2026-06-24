"""
build_article_list.py
Build updated article list and internal link mapping for DSL SEO project.

Outputs:
  data/master/DSL記事一覧.xlsx  (overwritten)
  data/master/m_station.csv     (overwritten via m_station_tmp.csv + manual replacement
                                  if file is locked by Windows)
"""

import pandas as pd
import numpy as np
import math
import re
import shutil
import os
from collections import defaultdict

# ── paths ────────────────────────────────────────────────────────────────────
STUDIO_CSV       = "data/master/studio_with_tags_dslurl_address_filled.csv"
MSTATION_CSV     = "data/master/m_station.csv"        # original 550-row master
MSTATION_NEW_CSV = "data/master/m_station_new.csv"    # full 8766-row lookup
ARTICLE_XLSX     = "data/master/DSL記事一覧.xlsx"

OUT_XLSX         = "data/master/DSL記事一覧.xlsx"
OUT_MSTATION     = "data/master/m_station.csv"
OUT_MSTATION_TMP = "data/master/m_station_updated.csv"  # fallback if locked

# ── constants ────────────────────────────────────────────────────────────────
TARGET_PREFS     = {"東京都", "神奈川県", "埼玉県", "千葉県"}
MIN_STUDIOS      = 5
OVERLAP_THRESHOLD = 5          # stations sharing > this many studios are "heavily overlapping"
NEARBY_MIN_KM    = 2.0
NEARBY_MAX_KM    = 8.0
AREA_NEARBY_MIN_KM = 2.0
AREA_NEARBY_MAX_KM = 15.0

CONDITION_ARTICLES = [
    "24時間使えるレンタルダンススタジオ 東京",
    "駅近のレンタルダンススタジオ 東京",
    "安いレンタルダンススタジオ 東京",
    "少人数向けレンタルダンススタジオ 東京",
    "深夜利用OKのダンススタジオ 東京",
    "大人数対応ダンススタジオ 東京",
    "初心者向けダンススタジオ 東京",
    "鏡付きレンタルダンススタジオ 東京",
    "撮影可能なダンススタジオ 東京",
    "防音完備のダンススタジオ 東京",
]

COND_PRIORITIES = {
    "24時間使えるレンタルダンススタジオ 東京": "S",
    "駅近のレンタルダンススタジオ 東京": "S",
    "安いレンタルダンススタジオ 東京": "S",
    "少人数向けレンタルダンススタジオ 東京": "S",
    "深夜利用OKのダンススタジオ 東京": "A",
    "大人数対応ダンススタジオ 東京": "A",
    "初心者向けダンススタジオ 東京": "A",
    "鏡付きレンタルダンススタジオ 東京": "A",
    "撮影可能なダンススタジオ 東京": "B",
    "防音完備のダンススタジオ 東京": "B",
}

COND_METHOD = {
    "24時間使えるレンタルダンススタジオ 東京": "feature_24h=1 AND rental=1",
    "駅近のレンタルダンススタジオ 東京": "distance_to_primary_station<=500 AND rental=1",
    "安いレンタルダンススタジオ 東京": "price_low=1 AND rental=1",
    "少人数向けレンタルダンススタジオ 東京": "capacity_small=1 AND rental=1",
    "深夜利用OKのダンススタジオ 東京": "feature_midnight=1 AND rental=1",
    "大人数対応ダンススタジオ 東京": "capacity_large=1 AND rental=1",
    "初心者向けダンススタジオ 東京": "feature_beginner=1 AND rental=1",
    "鏡付きレンタルダンススタジオ 東京": "feature_mirror=1 AND rental=1",
    "撮影可能なダンススタジオ 東京": "feature_shooting=1 AND rental=1",
    "防音完備のダンススタジオ 東京": "feature_soundproof=1 AND rental=1",
}

# Always-append internal links for every article
ALWAYS_LINKS = [
    "24時間使えるレンタルダンススタジオ 東京",
    "安いレンタルダンススタジオ 東京",
]

# Special station name aliases: station CSV name -> m_station_new name
STATION_ALIAS = {
    "押上": "押上〈スカイツリー前〉",
}

# ── helpers ──────────────────────────────────────────────────────────────────

def haversine_km(lat1, lon1, lat2, lon2):
    R = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def station_clean(name):
    """Strip 駅 suffix and trailing parenthetical from station name."""
    if not isinstance(name, str):
        return name
    return re.sub(r"駅.*$", "", name).strip()


def assign_priority(importance_score, studio_count):
    if importance_score >= 80 or studio_count >= 30:
        return "S"
    elif importance_score >= 60 or studio_count >= 15:
        return "A"
    elif studio_count >= 8:
        return "B"
    else:
        return "C"


def priority_order(p):
    return {"S": 0, "A": 1, "B": 2, "C": 3, "-": 4}.get(p, 5)


def write_csv_safe(df, path, tmp_path):
    """Write CSV, trying direct write first then via temp file + move."""
    # Try direct write
    try:
        df.to_csv(path, index=False)
        print(f"  Written to {path}")
        return True
    except PermissionError:
        pass

    # Try via temp + move
    df.to_csv(tmp_path, index=False)
    try:
        shutil.move(tmp_path, path)
        print(f"  Written to {path} (via temp)")
        return True
    except (PermissionError, OSError):
        pass

    # File is locked (likely open in Windows) - leave the temp file
    print(f"  WARNING: {path} is locked by another process.")
    print(f"  Output saved to: {tmp_path}")
    print(f"  Please close the file in Windows and manually rename:")
    print(f"    {tmp_path} -> {path}")
    return False


# ── load data ────────────────────────────────────────────────────────────────
print("Loading data...")
studios_df   = pd.read_csv(STUDIO_CSV)
ms_df        = pd.read_csv(MSTATION_CSV)          # 550-row original master
ms_new_df    = pd.read_csv(MSTATION_NEW_CSV)       # 8766-row full lookup

rental = studios_df[studios_df["rental"] == 1].copy()
rental["_station"] = rental["primary_station"].apply(station_clean)

# ── build station geo lookup ───────────────────────────────────────────────────
# For stations that exist in multiple prefectures (same name), prefer TARGET_PREFS.
# Build lookup: station_name -> best matching row (TARGET_PREFS preferred)

def build_station_lookup(df_main, df_new, target_prefs):
    """
    Build geo and prefecture lookup dicts.
    When multiple rows share the same station_name, prefer TARGET_PREFS entries.
    df_main rows take precedence over df_new rows only when both are in target prefs.
    """
    geo_lookup  = {}  # name -> {lat, lon, importance_score, in_ms_main}
    pref_lookup = {}  # name -> prefecture

    # Helper: add entry, preferring TARGET_PREFS
    def add_entry(name, lat, lon, score, pref, in_main):
        current_pref = pref_lookup.get(name)
        if current_pref is None:
            # No entry yet
            geo_lookup[name]  = {"lat": lat, "lon": lon, "importance_score": score, "in_ms_main": in_main}
            pref_lookup[name] = pref
        elif current_pref not in target_prefs and pref in target_prefs:
            # Upgrade to target pref
            geo_lookup[name]  = {"lat": lat, "lon": lon, "importance_score": score, "in_ms_main": in_main}
            pref_lookup[name] = pref
        elif current_pref in target_prefs and pref in target_prefs and in_main:
            # Both in target; prefer ms_main entry (higher quality)
            geo_lookup[name]  = {"lat": lat, "lon": lon, "importance_score": score, "in_ms_main": in_main}
            pref_lookup[name] = pref
        # Otherwise keep existing entry

    # First pass: m_station_new (all stations)
    for _, row in df_new.iterrows():
        add_entry(
            row["station_name"],
            float(row["latitude"]),
            float(row["longitude"]),
            int(row["importance_score"]),
            row["prefecture"],
            False,
        )

    # Second pass: m_station (original, authoritative for included stations)
    for _, row in df_main.iterrows():
        add_entry(
            row["station_name"],
            float(row["latitude"]),
            float(row["longitude"]),
            int(row["importance_score"]),
            row["prefecture"],
            True,
        )

    return geo_lookup, pref_lookup


station_geo_lookup, station_pref_lookup = build_station_lookup(ms_df, ms_new_df, TARGET_PREFS)

# ── determine which stations qualify for articles ─────────────────────────────
print("Building station articles...")

station_counts_raw = rental[rental["_station"] != "なし"]["_station"].value_counts()

station_articles = {}   # cleaned name (no 駅 suffix) -> studio_count
station_geo      = {}   # cleaned name -> geo dict

for stn, cnt in station_counts_raw.items():
    if cnt < MIN_STUDIOS:
        continue

    # Resolve alias if needed
    lookup_name = STATION_ALIAS.get(stn, stn)

    # Get geo info
    geo = station_geo_lookup.get(lookup_name)
    if geo is None:
        # Not found in either station master
        print(f"  SKIP {stn}: not found in station masters")
        continue

    # Check prefecture
    pref = station_pref_lookup.get(lookup_name, "")
    if pref not in TARGET_PREFS:
        # If station not in any master, fall back to checking studio lat/lon
        # (some very minor stations may only be in m_station_new as D-rank)
        if not pref:
            # Check from studio data
            subset = rental[rental["_station"] == stn]
            sample_pref = subset["pref"].iloc[0] if "pref" in subset.columns and len(subset) > 0 else ""
            if sample_pref not in TARGET_PREFS:
                print(f"  SKIP {stn}: pref={pref!r} not in target")
                continue
        else:
            print(f"  SKIP {stn}: pref={pref!r} not in target")
            continue

    station_articles[stn] = cnt
    station_geo[stn] = geo

print(f"  Station articles: {len(station_articles)}")

# ── build area article set ────────────────────────────────────────────────────
print("Building area articles...")
area_counts = rental["primary_area"].value_counts()
area_articles = {}  # area_name -> studio_count

for area, cnt in area_counts.items():
    if area != "その他" and cnt >= MIN_STUDIOS:
        area_articles[area] = cnt

print(f"  Area articles: {len(area_articles)}")

# ── compute area centroids ────────────────────────────────────────────────────
area_centroids = {}
for area in area_articles:
    subset = rental[rental["primary_area"] == area].dropna(subset=["latitude", "longitude"])
    if len(subset) > 0:
        area_centroids[area] = {
            "lat": float(subset["latitude"].mean()),
            "lon": float(subset["longitude"].mean()),
        }

# ── build candidate_stations overlap map ──────────────────────────────────────
print("Building station overlap maps...")

station_to_studios = defaultdict(set)  # cleaned station name -> set of studio row indices

for idx, row in rental.iterrows():
    cands = row.get("candidate_stations", "")
    if isinstance(cands, str) and cands.strip():
        for c in cands.split("|"):
            c_clean = station_clean(c.strip())
            station_to_studios[c_clean].add(idx)


def overlap_count(a, b):
    return len(station_to_studios[a] & station_to_studios[b])


# ── assign priorities ─────────────────────────────────────────────────────────
station_priority = {}
for stn, cnt in station_articles.items():
    imp = station_geo.get(stn, {}).get("importance_score", 0)
    station_priority[stn] = assign_priority(imp, cnt)

area_priority = {}
for area, cnt in area_articles.items():
    area_priority[area] = assign_priority(0, cnt)


# ── article title helpers ─────────────────────────────────────────────────────
def station_title(stn):
    return f"{stn}駅のレンタルダンススタジオおすすめ"


def area_title(area):
    return f"{area}のレンタルダンススタジオおすすめ"


# ── primary area for each station article ─────────────────────────────────────
station_primary_area = {}
for stn in station_articles:
    subset = rental[rental["_station"] == stn]["primary_area"]
    if len(subset) > 0:
        mc = subset.value_counts()
        station_primary_area[stn] = mc.index[0] if len(mc) > 0 else None
    else:
        station_primary_area[stn] = None


# ── generate internal links for station articles ──────────────────────────────
print("Generating internal links for station articles...")


def get_station_links(stn):
    links = []

    # 1. Area article of X's primary area
    pa = station_primary_area.get(stn)
    if pa and pa in area_articles:
        links.append(area_title(pa))

    # 2. Station articles within same area, low overlap (2-3 links)
    area_stns = [
        s for s in station_articles
        if s != stn
        and station_primary_area.get(s) == pa
        and overlap_count(stn, s) < OVERLAP_THRESHOLD
    ]
    area_stns.sort(key=lambda s: (priority_order(station_priority[s]), -station_articles[s]))
    for s in area_stns[:3]:
        t = station_title(s)
        if t not in links:
            links.append(t)

    # 3. Nearby station articles (2-8km, low overlap), prioritize by importance_score desc
    stn_geo = station_geo.get(stn)
    if stn_geo:
        nearby = []
        for s in station_articles:
            if s == stn:
                continue
            if station_title(s) in links:
                continue
            s_geo = station_geo.get(s)
            if not s_geo:
                continue
            dist = haversine_km(stn_geo["lat"], stn_geo["lon"], s_geo["lat"], s_geo["lon"])
            if NEARBY_MIN_KM <= dist <= NEARBY_MAX_KM:
                ov = overlap_count(stn, s)
                if ov < OVERLAP_THRESHOLD:
                    nearby.append((s, dist, s_geo["importance_score"]))
        nearby.sort(key=lambda x: (-x[2], x[1]))
        for s, dist, imp in nearby:
            if len(links) >= 8:
                break
            t = station_title(s)
            if t not in links:
                links.append(t)

    # 4. Always append
    for a in ALWAYS_LINKS:
        if a not in links:
            links.append(a)

    return links


station_links = {}
for stn in station_articles:
    station_links[stn] = get_station_links(stn)


# ── generate internal links for area articles ─────────────────────────────────
print("Generating internal links for area articles...")


def get_area_links(area):
    links = []

    # 1. Station articles where most studios have primary_area = area (2-3 links)
    area_stns = [
        s for s in station_articles
        if station_primary_area.get(s) == area
    ]
    area_stns.sort(key=lambda s: -station_articles[s])
    for s in area_stns[:3]:
        t = station_title(s)
        if t not in links:
            links.append(t)

    # 2. Nearby area articles (2-15km centroid distance)
    centroid = area_centroids.get(area)
    if centroid:
        nearby_areas = []
        for a2 in area_articles:
            if a2 == area:
                continue
            c2 = area_centroids.get(a2)
            if not c2:
                continue
            dist = haversine_km(centroid["lat"], centroid["lon"], c2["lat"], c2["lon"])
            if AREA_NEARBY_MIN_KM <= dist <= AREA_NEARBY_MAX_KM:
                nearby_areas.append((a2, dist))
        nearby_areas.sort(key=lambda x: x[1])
        for a2, dist in nearby_areas:
            if len(links) >= 8:
                break
            t = area_title(a2)
            if t not in links:
                links.append(t)

    # 3. Always append
    for a in ALWAYS_LINKS:
        if a not in links:
            links.append(a)

    return links


area_links = {}
for area in area_articles:
    area_links[area] = get_area_links(area)


# ── article_area_link per station ─────────────────────────────────────────────
station_area_link = {}
for stn in station_articles:
    pa = station_primary_area.get(stn)
    if pa and pa in area_articles:
        station_area_link[stn] = area_title(pa)
    else:
        station_area_link[stn] = ""


# ── assemble final article rows ───────────────────────────────────────────────
print("Assembling article table...")

rows = []

# Area articles
for area in sorted(area_articles.keys()):
    cnt = area_articles[area]
    pri = area_priority[area]
    method = f"primary_area={area} AND rental=1"
    links_str = "|".join(area_links[area])
    rows.append({
        "記事タイトル": area_title(area),
        "記事タイプ": "エリア",
        "作り方（対象スタジオ選定）": method,
        "優先度": pri,
        "スタジオ数": cnt,
        "内部リンク": links_str,
    })

# Station articles
for stn in sorted(station_articles.keys()):
    cnt = station_articles[stn]
    pri = station_priority[stn]
    method = f"distance_to_{stn}駅<=1200 AND rental=1"
    links_str = "|".join(station_links[stn])
    rows.append({
        "記事タイトル": station_title(stn),
        "記事タイプ": "駅",
        "作り方（対象スタジオ選定）": method,
        "優先度": pri,
        "スタジオ数": cnt,
        "内部リンク": links_str,
    })

# Condition articles
for cond in CONDITION_ARTICLES:
    rows.append({
        "記事タイトル": cond,
        "記事タイプ": "条件",
        "作り方（対象スタジオ選定）": COND_METHOD[cond],
        "優先度": COND_PRIORITIES[cond],
        "スタジオ数": "",
        "内部リンク": "",
    })

articles_df = pd.DataFrame(rows, columns=[
    "記事タイトル", "記事タイプ", "作り方（対象スタジオ選定）", "優先度", "スタジオ数", "内部リンク"
])

# Sort: エリア → 駅 → 条件, then S→A→B→C, then title
type_order_map = {"エリア": 0, "駅": 1, "条件": 2}
articles_df["_type_ord"] = articles_df["記事タイプ"].map(type_order_map)
articles_df["_pri_ord"]  = articles_df["優先度"].map(priority_order)
articles_df.sort_values(["_type_ord", "_pri_ord", "記事タイトル"], inplace=True)
articles_df.drop(columns=["_type_ord", "_pri_ord"], inplace=True)
articles_df.reset_index(drop=True, inplace=True)


# ── write DSL記事一覧.xlsx ────────────────────────────────────────────────────
print(f"Writing {OUT_XLSX}...")
with pd.ExcelWriter(OUT_XLSX, engine="openpyxl") as writer:
    articles_df.to_excel(writer, sheet_name="Sheet1", index=False)
print("  Done.")


# ── update m_station.csv ──────────────────────────────────────────────────────
print(f"Updating {OUT_MSTATION}...")

ms_out = ms_df.copy()

def get_ms_station_data(row_name, row_pref):
    """
    row_name is the station_name from ms_df (no 駅 suffix).
    row_pref is the prefecture column value.
    Only assign article data if station is in a target prefecture.
    """
    # Only count articles for target-prefecture rows
    if row_pref not in TARGET_PREFS:
        return 0, 0, "-", "", ""

    cnt = station_articles.get(row_name, 0)
    has_article = 1 if cnt >= MIN_STUDIOS else 0
    pri = station_priority.get(row_name, "-") if has_article else "-"
    area_link = station_area_link.get(row_name, "") if has_article else ""
    links = station_links.get(row_name, []) if has_article else []
    links_str = "|".join(links) if links else ""
    return has_article, cnt, pri, area_link, links_str


has_article_col     = []
studio_count_col    = []
priority_col        = []
area_link_col       = []
internal_links_col  = []

for _, row in ms_out.iterrows():
    sname = row["station_name"]
    spref = row.get("prefecture", "")
    ha, cnt, pri, alink, ilinks = get_ms_station_data(sname, spref)
    has_article_col.append(ha)
    studio_count_col.append(cnt)
    priority_col.append(pri)
    area_link_col.append(alink)
    internal_links_col.append(ilinks)

ms_out["has_article"]            = has_article_col
ms_out["article_studio_count"]   = studio_count_col
ms_out["article_priority"]       = priority_col
ms_out["article_area_link"]      = area_link_col
ms_out["article_internal_links"] = internal_links_col

write_csv_safe(ms_out, OUT_MSTATION, OUT_MSTATION_TMP)


# ── summary ───────────────────────────────────────────────────────────────────
print()
print("=" * 60)
print("SUMMARY")
print("=" * 60)
print(f"Total articles: {len(articles_df)}")
area_count = (articles_df["記事タイプ"] == "エリア").sum()
stn_count  = (articles_df["記事タイプ"] == "駅").sum()
cond_count = (articles_df["記事タイプ"] == "条件").sum()
print(f"  Area articles:      {area_count}")
print(f"  Station articles:   {stn_count}")
print(f"  Condition articles: {cond_count}")
print()

stn_arts  = articles_df[articles_df["記事タイプ"] == "駅"]
area_arts = articles_df[articles_df["記事タイプ"] == "エリア"]

print("Station priority breakdown:")
for p in ["S", "A", "B", "C"]:
    n = (stn_arts["優先度"] == p).sum()
    print(f"  {p}: {n}")
print()
print("Area priority breakdown:")
for p in ["S", "A", "B", "C"]:
    n = (area_arts["優先度"] == p).sum()
    print(f"  {p}: {n}")
print()

print("m_station.csv has_article=1 count:",
      ms_out["has_article"].sum(), "stations in original 550-row master")
print()

# Sample 5 station articles with links (sorted by priority)
print("Sample of 5 station articles with internal links:")
print("-" * 60)
sample_stn = stn_arts.head(5)
for _, r in sample_stn.iterrows():
    links = r["内部リンク"].split("|") if r["内部リンク"] else []
    print(f"\n{r['記事タイトル']}  [{r['優先度']}]  ({r['スタジオ数']} studios)")
    for i, lk in enumerate(links, 1):
        print(f"  {i:2d}. {lk}")
