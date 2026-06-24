#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sitemap.xml 生成スクリプト

articles/ ディレクトリの HTML ファイルから sitemap.xml を生成する。
記事生成後に実行する。

使い方:
  python3 sitemap.py
"""

import urllib.parse
from pathlib import Path
from datetime import date

ARTICLES_DIR   = Path("articles")
CANONICAL_BASE = "https://dance-studio-lab.com/media/"
OUTPUT_FILE    = ARTICLES_DIR / "sitemap.xml"

# 記事ページの優先度・更新頻度
ARTICLE_PRIORITY   = "0.8"
ARTICLE_CHANGEFREQ = "monthly"

# トップページ等を追加したい場合はここに記述
STATIC_URLS = [
    {
        "loc":        "https://dance-studio-lab.com/",
        "priority":   "1.0",
        "changefreq": "weekly",
    },
    {
        "loc":        "https://dance-studio-lab.com/studio/",
        "priority":   "0.9",
        "changefreq": "weekly",
    },
]


def make_url_entry(loc: str, lastmod: str, changefreq: str, priority: str) -> str:
    return (
        "  <url>\n"
        f"    <loc>{loc}</loc>\n"
        f"    <lastmod>{lastmod}</lastmod>\n"
        f"    <changefreq>{changefreq}</changefreq>\n"
        f"    <priority>{priority}</priority>\n"
        "  </url>"
    )


def main():
    today = date.today().isoformat()

    entries = []

    # 静的URL
    for s in STATIC_URLS:
        entries.append(make_url_entry(
            loc=s["loc"],
            lastmod=today,
            changefreq=s["changefreq"],
            priority=s["priority"],
        ))

    # articles/ 内の HTML ファイル
    htmls = sorted(ARTICLES_DIR.glob("*.html"))
    for html in htmls:
        slug = urllib.parse.quote(html.stem, safe="")
        loc  = CANONICAL_BASE + slug
        entries.append(make_url_entry(
            loc=loc,
            lastmod=today,
            changefreq=ARTICLE_CHANGEFREQ,
            priority=ARTICLE_PRIORITY,
        ))

    sitemap = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "\n".join(entries)
        + "\n</urlset>\n"
    )

    OUTPUT_FILE.write_text(sitemap, encoding="utf-8")
    print(f"sitemap.xml 生成完了: 静的{len(STATIC_URLS)}件 + 記事{len(htmls)}件 → {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
