"""S2 检查：品牌购物链接能否打开、每个品牌的 Google News 搜索有没有结果。

用法：.venv/bin/python scripts/check_sources.py [--out 报告.md]
"""
from __future__ import annotations

import argparse
import socket
import sys
import time
from pathlib import Path
from urllib.parse import quote_plus, urlparse

import feedparser
import httpx
import yaml

ROOT = Path(__file__).resolve().parents[1]
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
EVENTS = '("black friday" OR "cyber monday" OR "boxing day")'
GNEWS = {"US": "hl=en-US&gl=US&ceid=US:en", "CA": "hl=en-CA&gl=CA&ceid=CA:en"}


def news_url(brand: dict, region: str) -> str:
    names = [brand["name"], *brand.get("aliases", [])]
    who = " OR ".join(f'"{n}"' for n in names)
    q = f"({who}) {EVENTS} when:14d"
    return f"https://news.google.com/rss/search?q={quote_plus(q)}&{GNEWS[region]}"


def on_domain(host: str, domains: list[str]) -> bool:
    host = host.lower().removeprefix("www.")
    return any(host == d or host.endswith("." + d) for d in domains)


def check_shop(client: httpx.Client, url: str, region: str, domains: list[str]) -> str:
    """只判断三件事：域名存在、不是 404、最终没有跳出品牌域名。打不开页面不等于链接错（反爬）。"""
    host = urlparse(url).hostname or ""
    try:
        socket.getaddrinfo(host, 443)
    except socket.gaierror:
        return "❌ 域名不存在"
    try:
        r = client.get(url)
    except httpx.TimeoutException:
        return "⚠️ 超时（疑似拦截，需人工确认）"
    except httpx.HTTPError as e:
        return f"⚠️ 连不上（{type(e).__name__}，需人工确认）"
    final = urlparse(str(r.url))
    moved = "" if final.netloc == urlparse(url).netloc else f" → {r.url}"
    if not on_domain(final.hostname or "", domains):
        return f"⚠️ 跳出了品牌域名{moved}"
    if region == "US" and (final.hostname.endswith(".ca") or "/ca" in final.path.lower()):
        return f"⚠️ 美国链接被跳到加拿大站（本机 IP 在加拿大？）{moved}"
    if r.status_code in (404, 410):
        return f"❌ {r.status_code}{moved}"
    if r.status_code == 200:
        return f"✅ 200{moved}"
    return f"⚠️ {r.status_code} 疑似拦截（需人工确认）{moved}"


def news_count(client: httpx.Client, url: str) -> str:
    """来源失败要和“没有新闻”分开：只有 200 且能解析才报条数。"""
    try:
        r = client.get(url)
    except httpx.HTTPError as e:
        return f"⚠️ {type(e).__name__}"
    feed = feedparser.parse(r.content)
    if r.status_code != 200 or feed.bozo:
        return f"⚠️ {r.status_code}"
    return str(len(feed.entries))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    cfg = yaml.safe_load((ROOT / "config/brands.yaml").read_text(encoding="utf-8"))
    rows = ["| 品牌 | 地区 | 购物链接 | 链接检查 | 新闻条数(14天) |", "|---|---|---|---|---|"]
    with httpx.Client(headers={"User-Agent": UA}, follow_redirects=True, timeout=20) as client:
        for bid, b in cfg["brands"].items():
            for region, url in b["shop"].items():
                shop = check_shop(client, url, region, b["domains"])
                news = news_count(client, news_url(b, region))
                rows.append(f"| {b['name']} | {region} | {url} | {shop} | {news} |")
                print(rows[-1], flush=True)
                time.sleep(1.5)
    if args.out:
        Path(args.out).write_text("\n".join(rows) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
