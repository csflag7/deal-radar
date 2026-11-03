"""探针：确认运行环境（例如 GitHub Actions 的数据中心 IP）会不会被信息源拦。不调 AI，不写任何文件。

用法（在 app 目录下）：.venv/bin/python -m dealradar.probe
请求：2 条 Google News（美国、加拿大）、2 条 Bing News（美国、加拿大）、Slickdeals、Dealnews，
再抓 3 篇正文样本（macrumors、mashable、ign 各 1 篇：先用 Bing 搜 site:域名 找一篇，再按正式规则抓：
DealRadar UA、核对 robots.txt、自己跟跳转）。每个请求打印状态码和条目数 / 正文词数。
"""
from __future__ import annotations

import sys
import time
from urllib.parse import quote_plus

import httpx

from .articles import ArticleError, Pacer, RobotsCache, extract_paragraphs, fetch_html
from .config import Paths, load_config
from .sources import Feed, build_feeds, fetch_feed
from .status import domain_matches, host_of

# RFD 已停用（2026-11-01：改为 TollBit 付费授权）
FEED_IDS = ["gnews-US-nike", "gnews-CA-bestbuy", "bing-US-nike", "bing-CA-bestbuy", "slickdeals", "dealnews"]
ARTICLE_SITES = ["macrumors.com", "mashable.com", "ign.com"]


def _line(*cols) -> str:
    return "  ".join(str(c) for c in cols)


def probe(settings: dict, brands: dict, out=print, sleep=time.sleep) -> int:
    """返回成功的请求数。"""
    feeds = {f.id: f for f in build_feeds(brands, settings.get("deal_feeds") or [], year=int(settings["event_year"]),
                                          bing=True)}
    delay = float(settings.get("request_delay_seconds", 1.5))
    ok = 0
    out("== 信息源（RSS）==")
    with httpx.Client(headers={"User-Agent": settings["user_agent"]}, follow_redirects=True,
                      timeout=float(settings.get("request_timeout_seconds", 20))) as client:
        for i, fid in enumerate(FEED_IDS):
            if i:
                sleep(delay)
            t = time.monotonic()
            r = fetch_feed(client, feeds[fid])
            ok += r.ok
            out(_line("OK " if r.ok else "ERR", r.status or "-", f"{fid:18s}",
                      f"条目 {len(r.items)}" if r.ok else r.error, f"{time.monotonic() - t:.1f}s"))
        # 用 Bing 找每个网站的一篇文章
        samples = []
        for site in ARTICLE_SITES:
            sleep(delay)
            q = f'site:{site} "black friday"'
            url = f"https://www.bing.com/news/search?q={quote_plus(q)}&format=rss&mkt=en-US"
            r = fetch_feed(client, Feed(f"bing-site-{site}", "bing", "Bing News", "US", url))
            link = next((it["link"] for it in r.items if domain_matches(host_of(it["link"]), site)), None)
            out(_line("OK " if link else "ERR", r.status or "-", f"bing site:{site:12s}",
                      f"条目 {len(r.items)}" if r.ok else r.error, "找到文章" if link else "没找到文章"))
            if link:
                samples.append((site, link))

    out("== 正文样本 ==")
    timeout = float(settings.get("article_timeout_seconds", 15))
    agent = settings.get("robots_agent", "DealRadar")
    with httpx.Client(headers={"User-Agent": settings.get("article_user_agent") or f"{agent}/0.1"},
                      follow_redirects=False, timeout=timeout) as client:
        pacer = Pacer(float(settings.get("article_delay_seconds", 1)),
                      float(settings.get("article_site_delay_seconds", 5)), sleep, time.monotonic)
        blocked: set = set()
        robots = RobotsCache(client, agent, timeout, pacer, blocked, int(settings.get("article_max_redirects", 3)))
        for site, link in samples:
            t = time.monotonic()
            if not robots.allowed(link):
                out(_line("SKIP", "-", f"{site:14s}", "robots.txt 不允许", link))
                continue
            try:
                html = fetch_html(client, link, timeout, robots, pacer, blocked,
                                  int(settings.get("article_max_redirects", 3)))
                _, words = extract_paragraphs(html, int(settings.get("article_min_words", 150)))
                ok += 1
                out(_line("OK ", 200, f"{site:14s}", f"正文 {words} 词", f"{time.monotonic() - t:.1f}s", link))
            except ArticleError as e:
                out(_line("ERR", "-", f"{site:14s}", str(e), link))
    total = len(FEED_IDS) + len(samples)
    out(f"== 合计：{ok}/{total} 个请求成功（另有 {len(ARTICLE_SITES)} 次 Bing 搜索用来找正文样本）==")
    return ok


def main(argv=None) -> int:
    cfg = load_config(Paths())
    ok = probe(cfg.settings, cfg.brands)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
