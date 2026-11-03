"""抓取信息源：Google News RSS 和 Bing News RSS（每个品牌 × 地区一条查询）+ 三个 deal 网站 RSS。

每个源单独判定成败：只有 HTTP 200 且 feedparser 解析无误（bozo 为假）才算成功；
失败不当成 0 条，结果里单独列出，供告警使用。

Bing 的链接是 bing.com/news/apiclick.aspx?...&url=<原文>，从 url 参数里取出原文链接，
来源域名用原文的域名（Google News 的链接是加密跳转，拿不到原文）。
不同网站的请求分开排队、同时进行；同一个网站的两次请求之间仍然间隔 request_delay_seconds。
"""
from __future__ import annotations

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import parse_qs, quote_plus, urlparse

import feedparser
import httpx

log = logging.getLogger(__name__)

EVENTS = '("black friday" OR "cyber monday" OR "boxing day")'
GNEWS = {"US": "hl=en-US&gl=US&ceid=US:en", "CA": "hl=en-CA&gl=CA&ceid=CA:en"}
BING_MKT = {"US": "en-US", "CA": "en-CA"}
NEWS_SOURCES = ("gnews", "bing")   # 新闻搜索类的源；其余是 deal 网站
SUMMARY_MAX = 1000


CANADA = "(Canada OR Canadian)"   # 加拿大版查询多加这一组词，不然结果大多是美国新闻


def news_url(brand: dict, region: str) -> str:
    """一个品牌一条查询，不和其他品牌合并。美国版照抄 scripts/check_sources.py；
    加拿大版（S3 v3）："品牌" (Canada OR Canadian) ("black friday" OR ...) when:14d。"""
    names = [brand["name"], *brand.get("aliases", [])]
    who = " OR ".join(f'"{n}"' for n in names)
    ca = f" {CANADA}" if region == "CA" else ""
    q = f"({who}){ca} {EVENTS} when:14d"
    return f"https://news.google.com/rss/search?q={quote_plus(q)}&{GNEWS[region]}"


def bing_query(brand: dict, year: int, region: str = "US") -> str:
    """"品牌" ("black friday" OR "cyber monday" OR "boxing day") 2026；有别名时用 OR 并上；
    加拿大版在品牌后面加 (Canada OR Canadian)。"""
    names = [brand["name"], *brand.get("aliases", [])]
    who = " OR ".join(f'"{n}"' for n in names)
    if len(names) > 1:
        who = f"({who})"
    ca = f" {CANADA}" if region == "CA" else ""
    return f"{who}{ca} {EVENTS} {year}"


def bing_url(brand: dict, region: str, year: int) -> str:
    return (f"https://www.bing.com/news/search?q={quote_plus(bing_query(brand, year, region))}"
            f"&format=rss&mkt={BING_MKT[region]}")


def bing_original_url(link: str) -> str:
    """bing.com/news/apiclick.aspx?...&url=<原文> → 原文链接；不是这种形式的原样返回。"""
    if not link:
        return link
    u = urlparse(link)
    host = (u.hostname or "").lower()
    if host == "bing.com" or host.endswith(".bing.com"):
        target = (parse_qs(u.query).get("url") or [""])[0].strip()
        if target.lower().startswith(("http://", "https://")):
            return target
    return link


def _origin(url: str) -> Optional[str]:
    u = urlparse(url or "")
    return f"{u.scheme}://{u.hostname}" if u.scheme in ("http", "https") and u.hostname else None


@dataclass
class Feed:
    id: str                       # gnews-US-nike / bing-CA-nike / slickdeals / dealnews / gnews-camedia1
    source: str                   # gnews / bing / slickdeals / dealnews / gnews-camedia1
    name: str
    region: str                   # 地区提示 US / CA
    url: str
    brand_id: Optional[str] = None


@dataclass
class FetchResult:
    feed: Feed
    ok: bool
    status: Optional[int] = None
    error: Optional[str] = None
    items: list = field(default_factory=list)

    def describe(self) -> str:
        return f"成功 {len(self.items)} 条" if self.ok else f"失败（{self.error}）"


def build_feeds(brands: dict, deal_feeds: list, year: Optional[int] = None, bing: bool = False) -> list:
    """Google News 只查品牌有官网的地区（和 check_sources.py 一致）；Bing News 同样（需要给 year）。"""
    feeds = []
    for bid, b in brands.items():
        for region in b.get("shop", {}):
            if region in GNEWS:
                feeds.append(Feed(f"gnews-{region}-{bid}", "gnews", "Google News", region,
                                  news_url(b, region), bid))
    if bing and year:
        for bid, b in brands.items():
            for region in b.get("shop", {}):
                if region in BING_MKT:
                    feeds.append(Feed(f"bing-{region}-{bid}", "bing", "Bing News", region,
                                      bing_url(b, region, year), bid))
    for d in deal_feeds:
        feeds.append(Feed(d["id"], d["id"], d.get("name", d["id"]), d.get("region", "unknown"), d["url"]))
    return feeds


# ---------- 文本清理 ----------
class _TextOnly(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)

    def handle_starttag(self, tag, attrs):
        if tag in ("br", "p", "div", "li"):
            self.parts.append(" ")


def strip_html(s: Optional[str]) -> str:
    if not s:
        return ""
    p = _TextOnly()
    p.feed(s)
    p.close()
    text = "".join(p.parts).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def _published(entry) -> Optional[str]:
    t = entry.get("published_parsed") or entry.get("updated_parsed")
    if not t:
        return None
    return datetime(*t[:6], tzinfo=timezone.utc).isoformat()


def normalize_entry(entry, feed: Feed) -> dict:
    """把一条 RSS / Atom 条目整理成统一结构。"""
    summary = entry.get("summary") or ""
    if not summary and entry.get("content"):
        summary = entry["content"][0].get("value", "")
    src = entry.get("source") or {}
    link = entry.get("link") or ""
    guid = entry.get("id") or link
    source_name, source_url = src.get("title") or feed.name, src.get("href")
    if feed.source == "bing":
        # 链接换成原文；同一篇文章在美国、加拿大两次查询里的 guid 也就一样了
        link = bing_original_url(link)
        guid = link
        source_url = _origin(link)
        source_name = entry.get("news_source") or (urlparse(link).hostname or feed.name)
    return {
        "source": feed.source,
        "feed": feed.id,
        "region_hint": feed.region,
        "brand_hints": [feed.brand_id] if feed.brand_id else [],
        "title": strip_html(entry.get("title")),
        "summary": strip_html(summary)[:SUMMARY_MAX],
        "link": link,
        "published": _published(entry),
        "guid": guid,
        # Google News 的 link 是跳转链接，真正的来源在 <source url=...>；Bing 的来源取原文域名
        "source_name": source_name,
        "source_url": source_url,
    }


class FeedError(Exception):
    pass


def parse_feed(content: bytes, feed: Feed) -> list:
    parsed = feedparser.parse(content)
    if parsed.get("bozo"):
        exc = parsed.get("bozo_exception")
        raise FeedError(f"解析出错 {type(exc).__name__ if exc else 'bozo'}")
    return [normalize_entry(e, feed) for e in parsed.entries]


# ---------- 联网抓取 ----------
def retry_after_seconds(value: Optional[str], default: float = 30, cap: float = 120) -> float:
    if not value:
        return min(default, cap)
    value = value.strip()
    if value.isdigit():
        return min(float(value), cap)
    try:
        when = parsedate_to_datetime(value)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return max(0.0, min((when - datetime.now(timezone.utc)).total_seconds(), cap))
    except (TypeError, ValueError):
        return min(default, cap)


def fetch_feed(client: httpx.Client, feed: Feed, max_wait: float = 120,
               sleep: Callable[[float], None] = time.sleep) -> FetchResult:
    """抓一个源。429 时按 Retry-After 等一次再试一次；其他任何问题都记成失败。"""
    try:
        r = client.get(feed.url)
        if r.status_code == 429:
            wait = retry_after_seconds(r.headers.get("Retry-After"), cap=max_wait)
            log.warning("%s 返回 429，等待 %.0f 秒后重试一次", feed.id, wait)
            sleep(wait)
            r = client.get(feed.url)
    except httpx.HTTPError as e:
        return FetchResult(feed, False, None, type(e).__name__)
    except Exception as e:  # 单个源出任何问题都不能拖垮整体
        return FetchResult(feed, False, None, type(e).__name__)
    if r.status_code != 200:
        return FetchResult(feed, False, r.status_code, f"HTTP {r.status_code}")
    try:
        items = parse_feed(r.content, feed)
    except FeedError as e:
        return FetchResult(feed, False, r.status_code, str(e))
    except Exception as e:
        return FetchResult(feed, False, r.status_code, f"解析出错 {type(e).__name__}")
    return FetchResult(feed, True, r.status_code, None, items)


def fetch_all(feeds: list, settings: dict, client: Optional[httpx.Client] = None,
              sleep: Callable[[float], None] = time.sleep, transport=None) -> list:
    """按网站分组：同一网站依次抓、每次间隔 request_delay_seconds；不同网站同时进行。结果按 feeds 原顺序返回。"""
    delay = float(settings.get("request_delay_seconds", 1.5))
    max_wait = float(settings.get("max_retry_after_seconds", 120))
    own = client is None
    if own:
        # Bing 第一次请求会跳转一次并设置 cookie；httpx.Client 会自动保存并带上
        client = httpx.Client(headers={"User-Agent": settings["user_agent"]}, follow_redirects=True,
                              timeout=float(settings.get("request_timeout_seconds", 20)), transport=transport)
    groups: dict = {}
    for i, feed in enumerate(feeds):
        groups.setdefault((urlparse(feed.url).hostname or "").lower(), []).append(i)
    results: list = [None] * len(feeds)

    def run_group(indices: list) -> None:
        for n, i in enumerate(indices):
            if n:
                sleep(delay)
            feed = feeds[i]
            res = fetch_feed(client, feed, max_wait, sleep)
            level = logging.INFO if res.ok else logging.WARNING
            log.log(level, "抓取 %s：%s", feed.id, res.describe())
            results[i] = res

    try:
        if len(groups) <= 1:
            for indices in groups.values():
                run_group(indices)
        else:
            with ThreadPoolExecutor(max_workers=len(groups)) as pool:
                for f in [pool.submit(run_group, idx) for idx in groups.values()]:
                    f.result()
    finally:
        if own:
            client.close()
    return results


def load_fixture(fixture_dir: Path, feeds: list) -> list:
    """测试用：从 fixture_dir/feeds/<feed_id>.xml 读输入，代替联网。没有文件的源跳过。"""
    by_id = {f.id: f for f in feeds}
    results = []
    for path in sorted((fixture_dir / "feeds").glob("*.xml")):
        feed = by_id.get(path.stem)
        if feed is None:
            log.warning("fixture %s 对不上任何信息源，跳过", path.name)
            continue
        try:
            results.append(FetchResult(feed, True, 200, None, parse_feed(path.read_bytes(), feed)))
        except FeedError as e:
            results.append(FetchResult(feed, False, 200, str(e)))
    return results


# ---------- 来源健康记录 ----------
def update_health(health: dict, results: list, now_iso: str) -> dict:
    """记录每个源最近一次成功的时间和连续失败次数（网页以后用来标红）。"""
    for r in results:
        h = health.setdefault(r.feed.id, {"last_success": None, "consecutive_failures": 0})
        h["last_attempt"] = now_iso
        if r.ok:
            h.update(last_success=now_iso, consecutive_failures=0, last_count=len(r.items), last_error=None)
        else:
            h["consecutive_failures"] = int(h.get("consecutive_failures") or 0) + 1
            h["last_error"] = r.error
    return health
