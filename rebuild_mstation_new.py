"""
rebuild_mstation_new.py
Rebuild m_station_new.csv with ALL ranks (including D) for geo lookup.
"""
import pandas as pd
import re

DATA_DIR = "data/master/駅データ"
OUTPUT_PATH = "data/master/m_station_new.csv"

PREF_MAP = {1:"北海道",2:"青森県",3:"岩手県",4:"宮城県",5:"秋田県",6:"山形県",7:"福島県",8:"茨城県",9:"栃木県",10:"群馬県",11:"埼玉県",12:"千葉県",13:"東京都",14:"神奈川県",15:"新潟県",16:"富山県",17:"石川県",18:"福井県",19:"山梨県",20:"長野県",21:"岐阜県",22:"静岡県",23:"愛知県",24:"三重県",25:"滋賀県",26:"京都府",27:"大阪府",28:"兵庫県",29:"奈良県",30:"和歌山県",31:"鳥取県",32:"島根県",33:"岡山県",34:"広島県",35:"山口県",36:"徳島県",37:"香川県",38:"愛媛県",39:"高知県",40:"福岡県",41:"佐賀県",42:"長崎県",43:"熊本県",44:"大分県",45:"宮崎県",46:"鹿児島県",47:"沖縄県"}
WHITELIST_S = {"東京","新宿","渋谷","池袋","品川","上野","秋葉原","横浜","川崎","大宮","千葉","名古屋","大阪","梅田","京都","三ノ宮","神戸三宮","博多","天神","札幌","仙台","広島","岡山","熊本","鹿児島中央","那覇空港"}
WHITELIST_A = {"吉祥寺","中野","高田馬場","新橋","有楽町","銀座","六本木","恵比寿","原宿","表参道","目黒","五反田","町田","立川","八王子","北千住","錦糸町","蒲田","藤沢","武蔵小杉","浦和","船橋","柏","松戸","静岡","浜松","金山","なんば","心斎橋","天王寺","京橋","新大阪","十三","西宮北口","姫路","小倉","長崎","大分","宮崎"}
SHINKANSEN = {"東京","品川","新横浜","小田原","熱海","三島","新富士","静岡","掛川","浜松","豊橋","三河安城","名古屋","岐阜羽島","米原","京都","新大阪","新神戸","西明石","姫路","相生","岡山","新倉敷","福山","新尾道","三原","東広島","広島","新岩国","徳山","新山口","厚狭","新下関","小倉","博多","上野","大宮","小山","宇都宮","那須塩原","新白河","郡山","福島","白石蔵王","仙台","古川","一ノ関","盛岡","八戸","新青森","新函館北斗","高崎","熊谷","長岡","新潟","長野","上越妙高","富山","新高岡","金沢","敦賀"}
PREF_CAPS = {"札幌","青森","盛岡","仙台","秋田","山形","福島","水戸","宇都宮","前橋","さいたま","千葉","新宿","横浜","新潟","富山","金沢","福井","甲府","長野","岐阜","静岡","名古屋","津","大津","京都","大阪","神戸","奈良","和歌山","鳥取","松江","岡山","広島","山口","徳島","高松","松山","高知","福岡","佐賀","長崎","熊本","大分","宮崎","鹿児島","那覇"}


def parse_city(address):
    if not address or pd.isna(address):
        return "", ""
    addr = re.sub(r"^(北海道|東京都|京都府|大阪府|.{2,3}[都道府県])", "", str(address))
    m = re.match(r"([^\s]+?市)([^\s]+?区)", addr)
    if m:
        return m.group(1), m.group(1) + m.group(2)
    m = re.match(r"([^\s]+?区)", addr)
    if m:
        return m.group(1), m.group(1)
    m = re.match(r"([^\s]+?市)", addr)
    if m:
        return m.group(1), m.group(1)
    return "", ""


def calc_importance(name, line_count, ward):
    score = 35 if line_count >= 5 else 30 if line_count == 4 else 24 if line_count == 3 else 15 if line_count == 2 else 5
    if name in PREF_CAPS:
        score += 15
    if ward:
        cb = re.sub(r"[市区町村郡]$", "", ward)
        if cb and cb in name:
            score += 8
    if name in SHINKANSEN:
        score += 10
    score = min(score, 100)
    if name in WHITELIST_S:
        rank = "S"
        score = max(score, 80)
    elif name in WHITELIST_A:
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


df_st = pd.read_csv(f"{DATA_DIR}/station20260430free.csv", dtype={"post": str, "open_ymd": str, "close_ymd": str})
df_st = df_st[df_st["e_status"] == 0].copy()
df_line = pd.read_csv(f"{DATA_DIR}/line20260409free.csv")
df_line = df_line[df_line["e_status"] == 0].copy()
df_comp = pd.read_csv(f"{DATA_DIR}/company20260409.csv")
df_comp = df_comp[df_comp["e_status"] == 0].copy()

line_name_map = dict(zip(df_line["line_cd"], df_line["line_name"]))
line_to_company = dict(zip(df_line["line_cd"], df_line["company_cd"]))
comp_name_map = dict(zip(df_comp["company_cd"], df_comp["company_name"]))
df_st["line_name"] = df_st["line_cd"].map(line_name_map)
df_st["company_name"] = df_st["line_cd"].map(line_to_company).map(comp_name_map)

records = []
for g_cd, grp in df_st.groupby("station_g_cd"):
    rep = grp.sort_values("e_sort").iloc[0]
    name = rep["station_name"]
    pref_cd = int(rep["pref_cd"])
    address = str(rep.get("address", "") or "")
    pref = PREF_MAP.get(pref_cd, "")
    lons = grp["lon"].dropna()
    lats = grp["lat"].dropna()
    lon = float(lons.mean())
    lat = float(lats.mean())
    city, ward = parse_city(address)
    lines = sorted({str(x) for x in grp["line_name"].dropna() if x})
    companies = sorted({str(x) for x in grp["company_name"].dropna() if x})
    line_count = len(lines)
    score, rank = calc_importance(name, line_count, ward)
    records.append({
        "station_id": int(g_cd),
        "station_group_id": int(g_cd),
        "station_name": name,
        "prefecture": pref,
        "city": city,
        "ward_or_city": ward,
        "address": address,
        "latitude": round(lat, 6),
        "longitude": round(lon, 6),
        "lines": "|".join(lines),
        "companies": "|".join(companies),
        "line_count": line_count,
        "importance_rank": rank,
        "importance_score": score,
        "source": "駅データ.jp",
    })

df_all = pd.DataFrame(records)
# Write ALL stations (including D rank) so we can do geo lookups
df_all.to_csv(OUTPUT_PATH, index=False)
print(f"Written {len(df_all)} rows to {OUTPUT_PATH}")

target_prefs_set = {"東京都", "神奈川県", "埼玉県", "千葉県"}
tok = df_all[df_all["prefecture"].isin(target_prefs_set)]
print(f"Target pref rows: {len(tok)}")

check_stns = ["西武新宿", "赤坂", "新大久保", "自由が丘", "三軒茶屋", "押上", "田町", "下北沢"]
for s in check_stns:
    rows = tok[tok["station_name"] == s]
    if len(rows) > 0:
        r = rows.iloc[0]
        print(f"  {s}: score={r['importance_score']}, rank={r['importance_rank']}, lat={r['latitude']:.4f}, lon={r['longitude']:.4f}")
    else:
        print(f"  {s}: NOT FOUND")
