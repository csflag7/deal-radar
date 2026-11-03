from __future__ import annotations

import importlib.util
from urllib.parse import parse_qs, urlparse

import httpx

from conftest import APP, FIXTURES
from dealradar.sources import (Feed, build_feeds, fetch_all, fetch_feed, news_url, parse_feed,
                               retry_after_seconds, update_health)

UA = "DealRadar/0.1 (personal deal calendar; contact via GitHub)"
GOOD = (FIXTURES / "run" / "feeds" / "slickdeals.xml").read_bytes()
FEED = Feed("slickdeals", "slickdeals", "Slickdeals", "US", "https://example.test/rss")


def client_for(handler):
    return httpx.Client(transport=httpx.MockTransport(handler), headers={"User-Agent": UA})


def test_news_url_same_as_check_sources(cfg):
    spec = importlib.util.spec_from_file_location("check_sources", APP / "scripts" / "check_sources.py")
    cs = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cs)
    for b in cfg.brands.values():
        if "US" in b["shop"]:   # 美国版和 S2 检查脚本一样；加拿大版 S3 v3 起多了 (Canada OR Canadian)
            assert news_url(b, "US") == cs.news_url(b, "US")


def test_one_query_per_brand_and_region(cfg):
    feeds = build_feeds(cfg.brands, cfg.settings["deal_feeds"])
    g = [f for f in feeds if f.source == "gnews"]
    assert len(g) == sum(len(b["shop"]) for b in cfg.brands.values())
    for f in g:
        q = parse_qs(urlparse(f.url).query)["q"][0]
        brand = cfg.brands[f.brand_id]
        # 查询里只有这一个品牌的名字，不和别的品牌合并
        others = [b["name"] for bid, b in cfg.brands.items() if bid != f.brand_id and b["name"] not in brand["name"]]
        assert brand["name"] in q
        assert not any(f'"{o}"' in q for o in others)
    assert {f.id for f in feeds if f.source != "gnews"} == {"slickdeals", "dealnews",
                                                             "gnews-camedia1", "gnews-camedia2"}


def test_parse_google_news_uses_source_href():
    feed = Feed("gnews-US-nike", "gnews", "Google News", "US", "u", "nike")
    items = parse_feed((FIXTURES / "run" / "feeds" / "gnews-US-nike.xml").read_bytes(), feed)
    it = items[0]
    assert it["source_url"] == "https://www.cnbc.com"
    assert it["source_name"] == "CNBC"
    assert it["link"].startswith("https://news.google.com/")
    assert "<" not in it["summary"] and "&nbsp;" not in it["summary"]
    assert it["guid"] == "CBMiNIKE001"
    assert it["published"].startswith("2026-10-28")
    assert it["region_hint"] == "US" and it["brand_hints"] == ["nike"]


def test_parse_atom_feed():
    feed = Feed("atom-sample", "atom-sample", "Atom sample", "CA", "u")
    items = parse_feed((FIXTURES / "atom_sample.xml").read_bytes(), feed)
    assert len(items) == 2
    it = items[0]
    assert it["summary"] == "Leaked flyer shows up to 60% off tools and kitchen, Nov 20 - Nov 27."
    assert it["link"].startswith("https://deals.example.ca/")
    assert it["published"].startswith("2026-10-31")
    assert it["source_url"] is None


def test_fetch_ok_and_user_agent():
    seen = {}

    def handler(req):
        seen["ua"] = req.headers["User-Agent"]
        return httpx.Response(200, content=GOOD)

    r = fetch_feed(client_for(handler), FEED)
    assert r.ok and r.status == 200 and len(r.items) == 2
    assert seen["ua"] == UA


def test_fetch_all_sets_honest_user_agent(cfg):
    seen = []
    transport = httpx.MockTransport(lambda req: seen.append(req.headers["User-Agent"]) or httpx.Response(200, content=GOOD))
    res = fetch_all([FEED], cfg.settings, sleep=lambda s: None, transport=transport)
    assert res[0].ok
    assert seen == [cfg.settings["user_agent"]] and seen[0].startswith("DealRadar/")


def test_http_error_is_failure_not_zero_items():
    r = fetch_feed(client_for(lambda req: httpx.Response(503)), FEED)
    assert not r.ok and r.status == 503 and r.error == "HTTP 503"
    assert "失败" in r.describe()


def test_bozo_is_failure():
    r = fetch_feed(client_for(lambda req: httpx.Response(200, content=b"<html><body>not a feed")), FEED)
    assert not r.ok and r.status == 200 and "解析出错" in r.error


def test_exception_is_failure():
    def boom(req):
        raise httpx.ConnectError("down", request=req)
    r = fetch_feed(client_for(boom), FEED)
    assert not r.ok and r.error == "ConnectError"


def test_429_waits_retry_after_then_retries_once():
    calls, waits = [], []

    def handler(req):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "7"})
        return httpx.Response(200, content=GOOD)

    r = fetch_feed(client_for(handler), FEED, sleep=waits.append)
    assert r.ok and len(calls) == 2 and waits == [7.0]


def test_429_twice_gives_up():
    calls, waits = [], []

    def handler(req):
        calls.append(1)
        return httpx.Response(429, headers={"Retry-After": "5"})

    r = fetch_feed(client_for(handler), FEED, sleep=waits.append)
    assert not r.ok and r.error == "HTTP 429" and len(calls) == 2 and waits == [5.0]


def test_retry_after_parsing():
    assert retry_after_seconds("12") == 12
    assert retry_after_seconds("9999", cap=120) == 120
    assert retry_after_seconds(None, default=30) == 30
    assert retry_after_seconds("Wed, 21 Oct 2015 07:28:00 GMT") == 0  # 已经过去的时间
    assert retry_after_seconds("garbage", default=10) == 10


def test_one_failure_does_not_stop_others(cfg):
    feeds = [Feed(f"f{i}", "slickdeals", "S", "US", f"https://example.test/{i}") for i in range(3)]

    def handler(req):
        if req.url.path == "/1":
            raise httpx.ReadTimeout("slow", request=req)
        return httpx.Response(200, content=GOOD)

    sleeps = []
    res = fetch_all(feeds, cfg.settings, client=client_for(handler), sleep=sleeps.append)
    assert [r.ok for r in res] == [True, False, True]
    assert res[1].error == "ReadTimeout"
    assert sleeps == [1.5, 1.5]  # 请求之间间隔 1.5 秒


def test_update_health_records_last_success():
    ok = fetch_feed(client_for(lambda req: httpx.Response(200, content=GOOD)), FEED)
    bad = fetch_feed(client_for(lambda req: httpx.Response(500)), FEED)
    h = update_health({}, [ok], "2026-11-01T12:00:00+00:00")
    assert h["slickdeals"]["last_success"] == "2026-11-01T12:00:00+00:00"
    assert h["slickdeals"]["consecutive_failures"] == 0
    h = update_health(h, [bad], "2026-11-02T12:00:00+00:00")
    h = update_health(h, [bad], "2026-11-03T12:00:00+00:00")
    assert h["slickdeals"]["last_success"] == "2026-11-01T12:00:00+00:00"
    assert h["slickdeals"]["consecutive_failures"] == 2
    assert h["slickdeals"]["last_error"] == "HTTP 500"
    h = update_health(h, [ok], "2026-11-04T12:00:00+00:00")
    assert h["slickdeals"]["consecutive_failures"] == 0


# ---------- Bing News（S3 v2） ----------
from dealradar.sources import bing_original_url, bing_query  # noqa: E402

BING = Feed("bing-CA-bestbuy", "bing", "Bing News", "CA", "u", "bestbuy")


def test_bing_link_resolves_to_original_article():
    link = ("http://www.bing.com/news/apiclick.aspx?ref=FexRss&aid=&tid=x"
            "&url=https%3a%2f%2fwww.nj.com%2fshopping-deals%2f2026%2f10%2fwhen.html%3fa%3d1&c=99&mkt=en-us")
    assert bing_original_url(link) == "https://www.nj.com/shopping-deals/2026/10/when.html?a=1"
    # 不是 Bing 跳转链接、或没有 url 参数的，原样返回
    assert bing_original_url("https://www.cnbc.com/x") == "https://www.cnbc.com/x"
    assert bing_original_url("http://www.bing.com/news/apiclick.aspx?ref=FexRss") == \
        "http://www.bing.com/news/apiclick.aspx?ref=FexRss"
    assert bing_original_url("https://www.bing.com/x?url=javascript%3aalert(1)").startswith("https://www.bing.com/")


def test_parse_bing_feed_uses_original_link_and_domain():
    items = parse_feed((FIXTURES / "bing-CA-bestbuy.xml").read_bytes(), BING)
    assert len(items) == 2
    it = items[0]
    assert it["source"] == "bing" and it["feed"] == "bing-CA-bestbuy"
    assert it["link"] == "https://www.usatoday.com/story/shopping/deals/best-buy-black-friday-2026/"
    assert it["guid"] == it["link"]                 # 美国、加拿大两次查询里同一篇的 guid 一样
    assert it["source_url"] == "https://www.usatoday.com"
    assert it["source_name"] == "USA Today"
    assert it["region_hint"] == "CA" and it["brand_hints"] == ["bestbuy"]
    assert it["published"].startswith("2026-10-30")
    assert "DoorBOOsters" in it["summary"]
    assert items[1]["source_url"] == "https://www.msn.com"


def test_bing_queries_per_brand_and_region(cfg):
    feeds = build_feeds(cfg.brands, cfg.settings["deal_feeds"], year=2026, bing=True)
    b = [f for f in feeds if f.source == "bing"]
    assert len(b) == sum(len(x["shop"]) for x in cfg.brands.values())
    nike_ca = next(f for f in b if f.id == "bing-CA-nike")
    q = parse_qs(urlparse(nike_ca.url).query)
    assert q["q"][0] == '"Nike" (Canada OR Canadian) ("black friday" OR "cyber monday" OR "boxing day") 2026'
    assert q["format"] == ["rss"] and q["mkt"] == ["en-CA"]
    us = parse_qs(urlparse(next(f for f in b if f.id == "bing-US-nike").url).query)
    assert us["mkt"] == ["en-US"] and us["q"][0] == '"Nike" ("black friday" OR "cyber monday" OR "boxing day") 2026'
    assert bing_query(cfg.brands["levis"], 2026) == \
        '("Levi\'s" OR "Levis") ("black friday" OR "cyber monday" OR "boxing day") 2026'
    # 不开 Bing 时和以前一样
    assert not [f for f in build_feeds(cfg.brands, [], year=2026) if f.source == "bing"]


def test_fetch_all_runs_sites_in_parallel_but_spaces_requests_per_site(cfg):
    feeds = [Feed(f"g{i}", "gnews", "G", "US", f"https://news.google.test/{i}") for i in range(3)] + \
            [Feed(f"b{i}", "bing", "B", "US", f"https://www.bing.test/{i}") for i in range(2)]
    sleeps = []
    res = fetch_all(feeds, cfg.settings, client=client_for(lambda req: httpx.Response(200, content=GOOD)),
                    sleep=sleeps.append)
    assert [r.feed.id for r in res] == ["g0", "g1", "g2", "b0", "b1"]   # 按原顺序返回
    assert all(r.ok for r in res)
    assert sorted(sleeps) == [1.5, 1.5, 1.5]   # 每个网站内部：n 个请求之间 n-1 次间隔


def test_canada_queries_and_media_feeds(cfg):
    g = parse_qs(urlparse(news_url(cfg.brands["levis"], "CA")).query)
    assert g["q"][0] == ('("Levi\'s" OR "Levis") (Canada OR Canadian) ("black friday" OR "cyber monday" OR '
                         '"boxing day") when:14d')
    assert g["ceid"] == ["CA:en"]
    assert "Canada" not in parse_qs(urlparse(news_url(cfg.brands["levis"], "US")).query)["q"][0]
    feeds = {f.id: f for f in build_feeds(cfg.brands, cfg.settings["deal_feeds"], year=2026, bing=True)}
    for fid in ("gnews-camedia1", "gnews-camedia2"):
        f = feeds[fid]
        q = parse_qs(urlparse(f.url).query)["q"][0]
        assert f.region == "CA" and f.source == fid and f.brand_id is None   # 和 deal 源一样要命中品牌
        assert "site:" in q and "when:7d" in q
    assert "site:retail-insider.com" in parse_qs(urlparse(feeds["gnews-camedia1"].url).query)["q"][0]


def test_rfd_disabled(cfg):
    """2026-11-01：RFD 改为 TollBit 付费授权，停用；redflagdeals.com 只留在加拿大来源名单里用于分级。"""
    feeds = build_feeds(cfg.brands, cfg.settings["deal_feeds"], year=2026, bing=True)
    assert not any("redflagdeals" in f.url or f.id == "rfd" for f in feeds)
    assert "redflagdeals.com" in cfg.settings["canadian_media_domains"]
