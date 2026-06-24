# -*- coding: utf-8 -*-
"""
studio_with_tags.csv と DSL記事一覧.xlsx を突き合わせ、各記事の対象スタジオ件数を検証する。
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from typing import Callable

import numpy as np
import pandas as pd
from tqdm import tqdm

# =========================
# 設定（CLI で上書き可）
# =========================
DEFAULT_STUDIO_CSV = "studio_with_tags.csv"
DEFAULT_ARTICLE_XLSX = "DSL記事一覧.xlsx"
DEFAULT_OUTPUT_CSV = "article_check.csv"

TITLE_CANDIDATES = ["記事タイトル", "タイトル", "title"]
TYPE_CANDIDATES = ["記事タイプ", "タイプ", "type", "記事タイプ名"]
PRIORITY_CANDIDATES = ["優先度", "priority"]
HOW_CANDIDATES = [
    "作り方",
    "作り方（対象スタジオ選定）",
    "抽出条件",
    "フィルタ",
    "filter",
]

STUDIO_TITLE_CANDIDATES = ["タイトル", "title", "スタジオ名", "name"]

# createcsv の駅探索半径と揃える（この距離以内のみ candidate_stations に載る）
NEAR_STATION_RADIUS_M = 1200

# col=value の列名エイリアス（作り方 → スタジオCSV）
COL_EQ_ALIASES = {
    "distance_to_primary_station": "distance_to_primary_station_m",
}

# スタジオCSVに列が無いが、条件を無視して件数を出したいフラグ（WARNING を出す）
SKIP_EQ_IF_NO_COLUMN = frozenset(
    {
        "price_low",
        "capacity_small",
        "capacity_large",
        "feature_midnight",
        "feature_beginner",
        "feature_mirror",
        "feature_shooting",
        "feature_soundproof",
    }
)

# 作り方: DSL 記事一覧形式
_DIST_TO_STATION_RE = re.compile(
    r"^distance_to_(.+?)\s*(<=|>=|==|!=|<|>)\s*([-+]?\d+(?:\.\d*)?)\s*$",
    re.IGNORECASE,
)
_EQ_RE = re.compile(
    r"^\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*(.+?)\s*$",
    re.IGNORECASE,
)

# 作り方: 従来の行ベース（改行 / ; 区切り）
_CONTAINS_RE = re.compile(
    r"^\s*([a-zA-Z_][a-zA-Z0-9_]*)\s+contains\s+(.+?)\s*$",
    re.IGNORECASE,
)
_CMP_RE = re.compile(
    r"^\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*(<=|>=|==|!=|<|>)\s*([-+]?\d+(?:\.\d*)?)\s*$",
    re.IGNORECASE,
)


def setup_logging(verbose: bool) -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError, ValueError):
        pass
    level = logging.DEBUG if verbose else logging.INFO
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(level)
    handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)


def read_studio_csv(path: str) -> pd.DataFrame:
    for enc in ("utf-8", "utf-8-sig", "cp932", "shift_jis", "utf-16", "utf-16le", "utf-16be"):
        try:
            return pd.read_csv(path, encoding=enc)
        except UnicodeDecodeError:
            continue
    raise ValueError(f"CSVの文字コードを判定できませんでした: {path}")


def first_existing_column(df: pd.DataFrame, candidates: list[str]) -> str | None:
    for col in candidates:
        if col in df.columns:
            return col
    return None


def resolve_studio_column(name: str, studio_columns: list[str]) -> str | None:
    """作り方の列名を、スタジオCSV上の実際の列名に合わせる（大小無視）。"""
    by_lower = {str(c).lower(): c for c in studio_columns}
    return by_lower.get(str(name).lower())


def split_how_lines(text: str | float | None) -> list[str]:
    if text is None or (isinstance(text, float) and np.isnan(text)):
        return []
    s = str(text).strip()
    if not s:
        return []
    parts: list[str] = []
    for raw in re.split(r"[\n\r;]+", s):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts.append(line)
    return parts


def split_how_clauses(text: str | float | None) -> list[str]:
    """作り方を AND 区切りの子条件に分割する（DSL 記事一覧形式）。AND が無ければ行分割にフォールバック。"""
    if text is None or (isinstance(text, float) and np.isnan(text)):
        return []
    s = str(text).strip()
    if not s:
        return []
    if re.search(r"\s+AND\s+", s, flags=re.IGNORECASE):
        return [c.strip() for c in re.split(r"\s+AND\s+", s, flags=re.IGNORECASE) if c.strip()]
    return split_how_lines(text)


def cmp_num_series(
    num: pd.Series, op: str, val: float
) -> pd.Series:
    if op == "<=":
        return num <= val
    if op == ">=":
        return num >= val
    if op == "==":
        return num == val
    if op == "!=":
        return num != val
    if op == "<":
        return num < val
    if op == ">":
        return num > val
    return pd.Series(False, index=num.index)


def parse_one_predicate(
    line: str, studio_columns: list[str]
) -> tuple[Callable[[pd.DataFrame], pd.Series] | None, str | None]:
    """
    1行を解釈し、(スタジオDataFrame全体に対するマスクを返す関数, エラーメッセージ) を返す。
    成功時は (callable, None)、失敗時は (None, msg)。
    """
    m = _CONTAINS_RE.match(line)
    if m:
        raw_col = m.group(1)
        col = resolve_studio_column(raw_col, studio_columns)
        needle = m.group(2).strip()
        if not col:
            return None, f"列が存在しません: {raw_col!r} (行: {line!r})"

        def _contains_mask(d: pd.DataFrame) -> pd.Series:
            return d[col].astype(str).str.contains(needle, regex=False, na=False)

        return _contains_mask, None

    m = _CMP_RE.match(line)
    if m:
        raw_col = m.group(1)
        col = resolve_studio_column(raw_col, studio_columns)
        op = m.group(2)
        val = float(m.group(3))
        if not col:
            return None, f"列が存在しません: {raw_col!r} (行: {line!r})"

        def _cmp_mask(d: pd.DataFrame) -> pd.Series:
            num = pd.to_numeric(d[col], errors="coerce")
            return cmp_num_series(num, op, val)

        return _cmp_mask, None

    return None, f"解釈できない行: {line!r}"


def parse_eq_clause(
    clause: str,
    studio_columns: list[str],
    log: logging.Logger,
    article_idx: object,
) -> tuple[Callable[[pd.DataFrame], pd.Series] | None, str | None]:
    m = _EQ_RE.match(clause.strip())
    if not m:
        return None, None
    raw_col = m.group(1)
    raw_val = m.group(2).strip().strip("'\"")
    mapped = COL_EQ_ALIASES.get(raw_col, raw_col)
    col = resolve_studio_column(mapped, studio_columns)

    if raw_col == "feature_24h":
        want_on = str(raw_val).strip() in ("1", "1.0", "true", "True", "yes")

        def _f24(d: pd.DataFrame) -> pd.Series:
            tags = resolve_studio_column("article_tags", studio_columns)
            if not tags:
                return pd.Series(False, index=d.index)
            has = d[tags].astype(str).str.contains("feature:24h", regex=False, na=False)
            return has if want_on else ~has

        return _f24, None

    if not col:
        if raw_col in SKIP_EQ_IF_NO_COLUMN:

            def _neutral(_d: pd.DataFrame) -> pd.Series:
                return pd.Series(True, index=_d.index)

            log.warning(
                "記事 index=%s: 列 %s はスタジオCSVに無いため条件をスキップします (%s)",
                article_idx,
                raw_col,
                clause,
            )
            return _neutral, None
        return None, f"列が存在しません: {raw_col!r} (条件: {clause!r})"

    def _eq_mask(d: pd.DataFrame) -> pd.Series:
        ser = d[col]
        if re.fullmatch(r"[-+]?\d+(?:\.\d*)?", str(raw_val).strip()):
            lhs = pd.to_numeric(ser, errors="coerce")
            rhs = float(str(raw_val).strip())
            return lhs == rhs
        return ser.astype(str).str.strip().eq(str(raw_val).strip())

    return _eq_mask, None


def parse_distance_to_station_clause(
    clause: str, studio_columns: list[str]
) -> tuple[Callable[[pd.DataFrame], pd.Series] | None, str | None]:
    m = _DIST_TO_STATION_RE.match(clause.strip())
    if not m:
        return None, None
    key = m.group(1).strip()
    op = m.group(2)
    val = float(m.group(3))

    col_dist = resolve_studio_column("distance_to_primary_station_m", studio_columns)
    col_primary = resolve_studio_column("primary_station", studio_columns)
    col_cand = resolve_studio_column("candidate_stations", studio_columns)
    if not col_dist or not col_primary or not col_cand:
        miss = [x for x in [col_dist, col_primary, col_cand] if not x]
        return None, f"distance_to 条件に必要な列がありません: {miss}"

    if key.lower() == "primary_station":

        def _primary_dist(d: pd.DataFrame) -> pd.Series:
            num = pd.to_numeric(d[col_dist], errors="coerce")
            return cmp_num_series(num, op, val)

        return _primary_dist, None

    station = key

    def _station_radius(d: pd.DataFrame) -> pd.Series:
        cand = d[col_cand].astype(str).str.contains(station, regex=False, na=False)
        prim = d[col_primary].astype(str).str.strip().eq(station)
        dist = pd.to_numeric(d[col_dist], errors="coerce")
        # candidate_stations に載る駅は NEAR_STATION_RADIUS_M 以内（createcsv と同じ前提）
        if val >= NEAR_STATION_RADIUS_M - 0.5:
            return cand
        return prim & cmp_num_series(dist, op, val)

    return _station_radius, None


def parse_one_clause(
    clause: str,
    studio_columns: list[str],
    log: logging.Logger,
    article_idx: object,
) -> tuple[Callable[[pd.DataFrame], pd.Series] | None, str | None]:
    """1 つの AND 子句を解釈する。"""
    clause = clause.strip()
    if not clause:
        return None, "空の条件"

    pred, err = parse_distance_to_station_clause(clause, studio_columns)
    if err:
        return None, err
    if pred is not None:
        return pred, None

    pred, err = parse_eq_clause(clause, studio_columns, log, article_idx)
    if err:
        return None, err
    if pred is not None:
        return pred, None

    return parse_one_predicate(clause, studio_columns)


def build_predicates(
    how_text: str | float | None,
    studio_columns: list[str],
    log: logging.Logger,
    article_idx: object,
) -> tuple[list[Callable[[pd.DataFrame], pd.Series]], list[str]]:
    """作り方全文から述語リストとエラーリストを構築する。エラーがあれば述語は空。"""
    errors: list[str] = []
    clauses = split_how_clauses(how_text)
    if not clauses:
        return [], []

    predicates: list[Callable[[pd.DataFrame], pd.Series]] = []
    for clause in clauses:
        pred, err = parse_one_clause(clause, studio_columns, log, article_idx)
        if err:
            errors.append(err)
        elif pred is not None:
            predicates.append(pred)
        else:
            errors.append(f"解釈できない条件: {clause!r}")

    if errors:
        return [], errors
    return predicates, []


def studio_example_titles(
    studio_df: pd.DataFrame, mask: pd.Series, title_col: str | None, max_n: int = 3
) -> str:
    if not title_col or title_col not in studio_df.columns:
        return ""
    sub = studio_df.loc[mask, title_col].astype(str).str.strip()
    sub = sub[sub.ne("") & sub.ne("nan")]
    take = sub.head(max_n).tolist()
    return " / ".join(take)


def verdict(count: int) -> str:
    if count >= 10:
        return "OK"
    if count >= 1:
        return "少ない"
    return "要修正"


def run(
    studio_path: str,
    article_path: str,
    output_path: str,
) -> pd.DataFrame:
    log = logging.getLogger(__name__)
    log.info("スタジオCSV読み込み: %s", studio_path)
    studio_df = read_studio_csv(studio_path)
    studio_cols = list(studio_df.columns)

    title_col = first_existing_column(studio_df, STUDIO_TITLE_CANDIDATES)
    if not title_col:
        log.warning(
            "スタジオ側のタイトル列が見つかりません。対象スタジオ例は空になります。"
        )

    log.info("記事一覧読み込み: %s", article_path)
    try:
        articles_df = pd.read_excel(article_path, engine="openpyxl")
    except ImportError as e:
        raise ImportError(
            "Excel を読むには `pip install openpyxl` が必要です。"
        ) from e

    col_title = first_existing_column(articles_df, TITLE_CANDIDATES)
    col_type = first_existing_column(articles_df, TYPE_CANDIDATES)
    col_priority = first_existing_column(articles_df, PRIORITY_CANDIDATES)
    col_how = first_existing_column(articles_df, HOW_CANDIDATES)

    missing = [
        name
        for name, c in [
            ("記事タイトル系", col_title),
            ("記事タイプ系", col_type),
            ("優先度系", col_priority),
            ("作り方系", col_how),
        ]
        if c is None
    ]
    if missing:
        raise ValueError(
            "記事一覧に必要列が見つかりません: "
            + ", ".join(missing)
            + f"。列一覧: {list(articles_df.columns)}"
        )

    assert col_title and col_type and col_priority and col_how

    rows_out: list[dict[str, object]] = []
    for idx, row in tqdm(
        articles_df.iterrows(),
        total=len(articles_df),
        desc="articles",
        unit="row",
    ):
        how_val = row[col_how]
        predicates, errs = build_predicates(how_val, studio_cols, log, idx)

        if errs:
            for e in errs:
                log.warning("記事 index=%s: %s", idx, e)
            mask = pd.Series(False, index=studio_df.index)
        elif not predicates:
            log.warning(
                "記事 index=%s: 作り方が空または条件なし → 0件扱い",
                idx,
            )
            mask = pd.Series(False, index=studio_df.index)
        else:
            mask = pd.Series(True, index=studio_df.index)
            for pred in predicates:
                mask &= pred(studio_df)

        count = int(mask.sum())
        v = verdict(count)
        example = studio_example_titles(studio_df, mask, title_col)

        rows_out.append(
            {
                "記事タイトル": row[col_title],
                "記事タイプ": row[col_type],
                "優先度": row[col_priority],
                "対象件数": count,
                "対象スタジオ例": example,
                "判定": v,
            }
        )

    out_df = pd.DataFrame(rows_out)
    out_df.to_csv(output_path, index=False, encoding="utf-8-sig")
    log.info("出力完了: %s (%s 行)", output_path, len(out_df))

    ok_n = (out_df["判定"] == "OK").sum()
    low_n = (out_df["判定"] == "少ない").sum()
    bad_n = (out_df["判定"] == "要修正").sum()
    log.info("集計: OK=%s, 少ない=%s, 要修正=%s", ok_n, low_n, bad_n)

    return out_df


def main() -> None:
    p = argparse.ArgumentParser(description="記事とスタジオCSVの突合せ検証")
    p.add_argument("--studio", default=DEFAULT_STUDIO_CSV, help="スタジオCSV")
    p.add_argument("--articles", default=DEFAULT_ARTICLE_XLSX, help="DSL記事一覧.xlsx")
    p.add_argument("--output", default=DEFAULT_OUTPUT_CSV, help="出力 article_check.csv")
    p.add_argument("-v", "--verbose", action="store_true", help="DEBUGログ")
    args = p.parse_args()

    setup_logging(args.verbose)
    log = logging.getLogger(__name__)
    log.info("article_check 開始")

    run(args.studio, args.articles, args.output)

    log.info("article_check 終了")


if __name__ == "__main__":
    main()
