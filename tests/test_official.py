"""S15 官网监控：全部用假的 crawler / sitemap，离线跑。"""
from __future__ import annotations

import asyncio
import json
import shutil
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest
import yaml

from conftest import APP, FIXTURES
from dealradar import official as off
from dealradar import run as runmod

NOW = datetime(2026, 11, 1, 16, 0, tzinfo=timezone.utc)
BB_CA = ("From November 2 to 5, shop Black Friday prices on select products when you're signed in to your free "
         "My Best Buy account.")


def page_md(*lines):
    return "\n\n".join(["[Skip to content](https://x/#main)", "* [Deals](https://x/deals)", *lines,
                        "Shop by category", "© 2026 Store. All rights reserved."])


def ok(md, title="Store"):
    return SimpleNamespace(success=True, status_code=200, error_message="",
                           markdown=SimpleNamespace(raw_markdown=md, fit_markdown=""), metadata={"title": title})


def blocked_403():
    return SimpleNamespace(success=False, status_code=403, error_message="", markdown=None, metadata={})


class FakeCrawler:
    """按 URL 返回预设结果；记录调用次数、同时在跑的数量。"""

    def __init__(self, results):
        self.results = results
        self.calls = []
        self.active = self.peak = 0

    async def arun(self, url, config=None):
        self.calls.append(url)
        self.active += 1
        self.peak = max(self.peak, self.active)
        await asyncio.sleep(0.001)
        self.active -= 1
        r = self.results.get(url)
        if isinstance(r, Exception):
            raise r
        return r if r is not None else ok(page_md("Welcome"))


class FakeSleep:
    def __init__(self):
        self.calls = []

    async def __call__(self, s):
        self.calls.append(s)


# ---------- 地区路径、单品页 ----------
def test_region_path_filter():
    assert off.region_ok("https://www.bestbuy.ca/en-ca/collection/black-friday-deals/16074", "CA")
    assert off.region_ok("https://www.bestbuy.ca/fr-ca/collection/vendredi-fou/16074", "CA")         # 加拿大可以用法语
    assert off.region_ok("https://www.nike.com/ca/w/sale-3yaep", "CA")
    assert not off.region_ok("https://www.nike.com/w/sale-3yaep", "CA")                              # 没有加拿大标记
    assert off.region_ok("https://www.nike.com/w/sale-3yaep", "US")
    assert not off.region_ok("https://www.nike.com/ca/w/sale-3yaep", "US")
    assert not off.region_ok("https://www.nike.com/gb/w/sale-3yaep", "US")                           # 别国
    assert not off.region_ok("https://www.walmart.ca/en/cp/black-friday", "US")                      # .ca 域名
    assert off.region_ok("https://www.adidas.com/us/black-friday", "US")
    assert not off.region_ok("https://www.levi.com/GB/en_GB/sale", "US")
    assert off.region_ok("https://www.levi.com/CA/en_CA/sale", "CA")


def test_product_pages_excluded():
    for u in ["https://www.bestbuy.com/site/apple-airpods/6447382.p?skuId=6447382",
              "https://www.target.com/p/lego-set/-/A-12345678", "https://www.amazon.com/dp/B0CHX1W1XY",
              "https://www.walmart.com/ip/Some-TV/123456789", "https://www.nike.com/t/air-max/product/DQ1234",
              "https://shop.lululemon.com/p/womens-leggings/Align/_/prod2020012", "https://x.com/sale.pdf",
              "https://www.costco.ca/Holiday-Wagon-Gift-Set.product.4000286863.html"]:
        assert off.is_product_url(u), u
    for u in ["https://www.bestbuy.com/site/misc/black-friday-deals/pcmcat225600050002.c",
              "https://www.costco.com/holiday-savings.html", "https://www.nike.com/w/sale-3yaep"]:
        assert not off.is_product_url(u), u


def test_pick_pages_scores_region_and_limit():
    shop = "https://www.adidas.com/us"
    urls = ["https://www.adidas.com/us/black-friday", "https://www.adidas.com/us/cyber-monday-deals",
            "https://www.adidas.com/us/holiday-gift-guide", "https://www.adidas.com/us/sale",
            "https://www.adidas.com/us/events", "https://www.adidas.com/us/about",            # 没关键词
            "https://www.adidas.com/ca/black-friday", "https://www.adidas.com/black-friday",  # 不在 /us 下
            "https://www.adidas.com/us/samba-shoes/product/B75806", "https://other.com/us/black-friday-sale"]
    picked = off.pick_pages(urls, shop, "US", max_pages=4)
    assert [u for u, _ in picked] == ["https://www.adidas.com/us/cyber-monday-deals",
                                      "https://www.adidas.com/us/black-friday", "https://www.adidas.com/us/sale",
                                      "https://www.adidas.com/us/holiday-gift-guide"]
    assert picked[0][1] == 7 and picked[1][1] == 5


def test_discover_keeps_manual_entries_and_marks_origin(cfg):
    brands = {"bestbuy": cfg.brands["bestbuy"]}
    sitemap = {"www.bestbuy.ca": ["https://www.bestbuy.ca/en-ca/collection/black-friday-deals/16074",
                                  "https://www.bestbuy.ca/fr-ca/collection/vendredi-fou/16074",
                                  "https://www.bestbuy.ca/en-ca/product/tv/12345678"],
               "www.bestbuy.com": []}

    async def seed(host):
        return sitemap.get(host, [])
    new, st = asyncio.run(off.discover_async(brands, {}, seed))
    old = [{"brand_id": "bestbuy", "region": "CA", "url": "https://www.bestbuy.ca/en-ca/event/top-deals",
            "origin": "manual"},
           {"brand_id": "bestbuy", "region": "CA", "url": "https://www.bestbuy.ca/en-ca/old", "origin": "sitemap"}]
    pages = off.merge_discovered(old, new)
    by = {(p["url"]): p["origin"] for p in pages}
    assert by["https://www.bestbuy.ca/en-ca/event/top-deals"] == "manual"          # 人工条目保留
    assert "https://www.bestbuy.ca/en-ca/old" not in by                            # 旧的 sitemap 条目被替换
    assert by["https://www.bestbuy.ca"] == "homepage" and by["https://www.bestbuy.com"] == "homepage"
    assert by["https://www.bestbuy.ca/en-ca/collection/black-friday-deals/16074"] == "sitemap"
    assert st.sites == 2 and st.sitemap_pages == 1 and st.homepage_pages == 2
    assert [f[:2] for f in st.sitemap_failed] == [("bestbuy", "US")]


def test_pages_file_roundtrip(tmp_path):
    p = tmp_path / "official_pages.yaml"
    pages = [{"brand_id": "costco", "region": "US", "url": "https://www.costco.com", "origin": "homepage"}]
    off.save_pages(p, pages, NOW.isoformat())
    text = p.read_text(encoding="utf-8")
    assert text.startswith("# 官网监控的页面清单") and "origin: manual" in text
    doc = off.load_pages(p)
    assert doc["pages"] == pages and not off.needs_discovery(doc, NOW)
    assert off.needs_discovery(doc, datetime(2026, 11, 9, tzinfo=timezone.utc))      # 超过 7 天


# ---------- 发现阶段的请求 ----------
# ---------- 抓取 ----------
def test_page_excerpt_keeps_activity_and_dates():
    md = page_md("Holiday Deals", BB_CA, "Available starting November 6.", "Free shipping on orders over $35",
                 "Careers at Store")
    ex = off.page_excerpt(md)
    assert BB_CA.replace("you're", "you're") in ex and "Available starting November 6." in ex
    assert "Careers" not in ex and "](" not in ex and len(ex) <= 2500


def test_classify_blocked_pages():
    assert off.classify(blocked_403()) == ("blocked", "HTTP 403")
    robots = SimpleNamespace(success=False, status_code=403, error_message="Access denied by robots.txt",
                             markdown=None, metadata={})
    assert off.classify(robots) == ("blocked", "robots.txt 不允许")
    px = SimpleNamespace(success=False, status_code=200, error_message="Blocked by anti-bot protection: PerimeterX block",
                         markdown=None, metadata={})
    assert off.classify(px)[0] == "blocked"
    chal = ok("Just a moment...\n\nChecking your browser before accessing the site.")
    assert off.classify(chal)[0] == "blocked"
    assert off.classify(ok("# Access Denied\n\nYou don't have permission to access this server."))[0] == "blocked"
    assert off.classify(ok(page_md(BB_CA))) == ("ok", "")
    assert off.classify(SimpleNamespace(success=False, status_code=None, error_message="Timeout 30000ms exceeded",
                                        markdown=None, metadata={}))[0] == "error"


def test_403_not_retried_and_rest_of_site_skipped(cfg):
    pages = [{"brand_id": "walmart", "region": "CA", "url": f"https://www.walmart.ca/en/{p}"} for p in ("", "bf", "cm")]
    pages.append({"brand_id": "bestbuy", "region": "CA", "url": "https://www.bestbuy.ca/en-ca"})
    crawler = FakeCrawler({"https://www.walmart.ca/en/": blocked_403(),
                           "https://www.bestbuy.ca/en-ca": ok(page_md(BB_CA))})
    res = asyncio.run(off.crawl_pages(pages, crawler, {}, lambda u: True, FakeSleep()))
    assert crawler.calls.count("https://www.walmart.ca/en/") == 1                 # 不重试
    assert "https://www.walmart.ca/en/bf" not in crawler.calls                    # 同网站后面的页面不再请求
    assert [r.status for r in res] == ["blocked", "skipped", "skipped", "ok"]


def test_robots_refusal_recorded():
    pages = [{"brand_id": "sephora", "region": "CA", "url": "https://www.sephora.com/ca/en/"}]
    crawler = FakeCrawler({})
    res = asyncio.run(off.crawl_pages(pages, crawler, {}, lambda u: False, FakeSleep()))
    assert crawler.calls == [] and res[0].status == "blocked" and res[0].reason == "robots.txt 不允许"


# ---------- 状态、哈希、需人工查看 ----------
def _setup(tmp_path, pages):
    p = tmp_path / "official_pages.yaml"
    off.save_pages(p, pages, NOW.isoformat())
    return p, tmp_path / "official_state.json", tmp_path / "manual_review.json"


def test_unchanged_hash_means_no_ai_and_blocked_goes_to_review(cfg, tmp_path):
    pages = [{"brand_id": "bestbuy", "region": "CA", "url": "https://www.bestbuy.ca/en-ca", "origin": "homepage"},
             {"brand_id": "walmart", "region": "CA", "url": "https://www.walmart.ca/en", "origin": "homepage"}]
    pp, sp, rp = _setup(tmp_path, pages)
    crawler = FakeCrawler({"https://www.bestbuy.ca/en-ca": ok(page_md(BB_CA)),
                           "https://www.walmart.ca/en": blocked_403()})
    kw = dict(crawler_factory=lambda: crawler, robots_ok=lambda u: True, sleep=FakeSleep())
    run1, state = off.run_official(cfg.brands, {}, pp, sp, rp, NOW, **kw)
    assert [it["link"] for it in run1.items] == ["https://www.bestbuy.ca/en-ca"]
    it = run1.items[0]
    assert it["official_region"] == "CA" and it["brand_hints"] == ["bestbuy"] and BB_CA in it["excerpt"]
    review = json.loads(rp.read_text(encoding="utf-8"))
    assert review == [{"brand_id": "walmart", "region": "CA", "url": "https://www.walmart.ca/en",
                       "reason": "HTTP 403", "last_attempt": NOW.isoformat(timespec="seconds")}]
    saved = json.loads(sp.read_text(encoding="utf-8"))["pages"]
    assert saved["https://www.walmart.ca/en"]["status"] == "blocked"
    assert all(set(v) <= {"brand_id", "region", "status", "reason", "hash", "last_crawl", "last_attempt",
                          "last_changed", "last_ai"} for v in saved.values())      # 不存正文
    # AI 处理完才记哈希；同样的内容再抓一次 → 不交给 AI
    off.finish_official(state, run1, run1.items, NOW.isoformat(), sp, rp)
    run2, _ = off.run_official(cfg.brands, {}, pp, sp, rp, NOW, **kw)
    assert run2.items == [] and run2.changed == []
    crawler.results["https://www.bestbuy.ca/en-ca"] = ok(page_md(BB_CA, "Available starting November 6."))
    run3, _ = off.run_official(cfg.brands, {}, pp, sp, rp, NOW, **kw)
    assert len(run3.items) == 1                                                   # 内容变了才再交给 AI
    assert "被拒：walmart.ca" in run3.describe()


def test_ai_failure_keeps_old_hash(cfg, tmp_path):
    pages = [{"brand_id": "bestbuy", "region": "CA", "url": "https://www.bestbuy.ca/en-ca", "origin": "homepage"}]
    pp, sp, rp = _setup(tmp_path, pages)
    crawler = FakeCrawler({"https://www.bestbuy.ca/en-ca": ok(page_md(BB_CA))})
    kw = dict(crawler_factory=lambda: crawler, robots_ok=lambda u: True, sleep=FakeSleep())
    run1, state = off.run_official(cfg.brands, {}, pp, sp, rp, NOW, **kw)
    off.finish_official(state, run1, [], NOW.isoformat(), sp, rp)                # AI 这一批失败了
    run2, _ = off.run_official(cfg.brands, {}, pp, sp, rp, NOW, **kw)
    assert len(run2.items) == 1                                                   # 下次再试


# ---------- 走完整流程：官网事件并入 data/local/official_base.json ----------
@pytest.fixture
def official_env(tmp_path, monkeypatch):
    conf = tmp_path / "config"
    shutil.copytree(APP / "config", conf)
    (conf / "manual_events.yaml").write_text("events: []\n", encoding="utf-8")
    fix = tmp_path / "fix"
    shutil.copytree(FIXTURES / "run", fix)
    ex = json.loads((fix / "extractions.json").read_text(encoding="utf-8"))
    ex["https://www.bestbuy.ca/en-ca"] = [
        {"brand_id": "bestbuy", "region": "US", "event_type": "black_friday", "event_name": "My Best Buy early access",
         "year": 2026, "start": "2026-11-02", "end": "2026-11-05", "discount": None, "quote": BB_CA}]
    (fix / "extractions.json").write_text(json.dumps(ex), encoding="utf-8")
    pages = [{"brand_id": "bestbuy", "region": "CA", "url": "https://www.bestbuy.ca/en-ca", "origin": "homepage"}]
    off.save_pages(conf / "official_pages.yaml", pages, datetime.now(timezone.utc).isoformat())
    crawler = FakeCrawler({"https://www.bestbuy.ca/en-ca": ok(page_md(BB_CA))})
    monkeypatch.setattr(runmod, "OFFICIAL_CRAWLER_FACTORY", lambda: crawler)
    monkeypatch.setattr(runmod, "OFFICIAL_ROBOTS_OK", lambda u: True)
    monkeypatch.setattr(off.asyncio, "sleep", FakeSleep())
    data = tmp_path / "data"
    args = ["--fixture", str(fix), "--config-dir", str(conf), "--data-dir", str(data),
            "--web-out", str(tmp_path / "w.js"), "--no-push", "--official"]
    return args, data, crawler


def test_official_events_merge_as_official_with_page_region(official_env, caplog):
    args, data, crawler = official_env
    assert runmod.main(args) == 0
    events = {e["id"]: e for e in json.loads((data / "local" / "official_base.json").read_text(encoding="utf-8"))
              ["events"]}
    e = events["bestbuy-CA-black_friday-2026"]                 # AI 说 US，地区按页面改成 CA
    assert (e["start"], e["end"], e["status"]) == ("2026-11-02", "2026-11-05", "confirmed")
    src = e["sources"][0]
    assert src["level"] == "official" and src["quote_in"] == "excerpt" and src["domain"] == "bestbuy.ca"
    assert list(events) == ["bestbuy-CA-black_friday-2026"]    # --official：不抓新闻
    assert (data / "local" / "official_state.json").exists() and (data / "local" / "manual_review.json").exists()
    assert not (data / "news").exists()                         # --official 只写 data/local/
    assert sorted(p.name for p in (data / "local").iterdir()) == ["debug", "manual_review.json", "official_base.json",
                                                                  "official_state.json"]
    caplog.clear()
    assert runmod.main(args) == 0                              # 第二次：页面没变，不交给 AI
    assert "官网页面交给 AI" not in caplog.text and "交给 AI 的页面 0 个" in caplog.text


def test_skip_official_and_errors_do_not_break_news(official_env, monkeypatch, caplog):
    args, data, crawler = official_env
    assert runmod.main([a for a in args if a != "--official"] + ["--skip-official"]) == 0
    assert crawler.calls == []
    assert not (data / "local").exists()                        # --skip-official 只写 data/news/
    assert sorted(p.name for p in (data / "news").iterdir()) == ["changes.json", "sales.json", "seen.json",
                                                                 "source_health.json"]
    monkeypatch.setattr(runmod, "OFFICIAL_CRAWLER_FACTORY", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert runmod.main(args) == 0 and "官网监控出错" in caplog.text


def test_daily_workflow_skips_official():
    d = yaml.safe_load((APP / ".github" / "workflows" / "daily.yml").read_text(encoding="utf-8"))
    runs = [s.get("run", "") for s in d["jobs"]["daily"]["steps"]]
    assert any(r.startswith("python -m dealradar.run") and "--skip-official" in r for r in runs)


# ======================= S15 一审 ③ 的修改 =======================
class Clock:
    """假时钟：async / sync 的 sleep 都只推进假时间。"""

    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    async def asleep(self, s):
        self.t += s
        await asyncio.sleep(0)

    def ssleep(self, s):
        self.t += s


def transport_for(routes, log, clock, robots=None, refused=None, max_requests=10):
    async def handler(req):
        log.append((req.url.host, req.url.path, clock.t))
        r = routes.get((req.url.host, req.url.path))
        return r if r is not None else httpx.Response(404)
    return off.PoliteTransport(httpx.MockTransport(handler), off.SiteClock(5.0, clock), max_requests, robots,
                               refused, clock.asleep)


async def _get_all(t, urls, follow=False):
    async with httpx.AsyncClient(transport=t, headers={"User-Agent": off.UA}, follow_redirects=follow) as c:
        return [(await c.get(u)).status_code for u in urls]


def test_polite_transport_spacing_budget_and_product_sitemaps():
    clock, log = Clock(), []
    ok = httpx.Response(200, text="<urlset></urlset>")
    t = transport_for({("s.com", p): ok for p in ("/robots.txt", "/sitemap-pages.xml", "/sitemap-category.xml")},
                      log, clock, max_requests=3)
    codes = asyncio.run(_get_all(t, ["https://s.com/robots.txt", "https://s.com/sitemap-products-1.xml.gz",
                                     "https://s.com/sitemap-pages.xml", "https://s.com/sitemap-category.xml",
                                     "https://s.com/sitemap-events.xml"]))
    assert codes == [200, 404, 200, 200, 404]          # 商品 sitemap 本地跳过；第 4 次请求超出上限
    assert [p for _, p, _ in log] == ["/robots.txt", "/sitemap-pages.xml", "/sitemap-category.xml"]
    times = [tt for _, _, tt in log]
    assert all(b - a >= 5.0 for a, b in zip(times, times[1:]))       # 同站间隔 ≥ 5 秒
    assert off.PoliteTransport().max_requests == 10                    # 代码默认值和配置一致
    assert off.SiteClock().delay == 5.0


def test_polite_transport_checks_robots_for_every_request_and_redirect_hop():
    clock, log = Clock(), []
    robots = lambda u: (False, "robots.txt 不允许") if "/private/" in u else (
        (False, "robots.txt 读不到") if "nobots.com" in u else (True, ""))
    routes = {("s.com", "/sitemap.xml"): httpx.Response(301, headers={"location": "https://s.com/private/sitemap.xml"}),
              ("s.com", "/private/sitemap.xml"): httpx.Response(200, text="<urlset/>"),
              ("s.com", "/robots.txt"): httpx.Response(200, text="User-agent: *\nDisallow: /private/")}
    t = transport_for(routes, log, clock, robots=robots)
    codes = asyncio.run(_get_all(t, ["https://s.com/sitemap.xml", "https://nobots.com/sitemap.xml"], follow=True))
    assert codes == [404, 404]
    assert ("s.com", "/private/sitemap.xml") not in [(h, p) for h, p, _ in log]     # 跳转那一跳也拦住了
    assert ("nobots.com", "/sitemap.xml") not in [(h, p) for h, p, _ in log]
    assert t.errors == {"s.com": "robots.txt 不允许", "nobots.com": "robots.txt 读不到"}


def test_polite_transport_refused_host_never_requested_again():
    clock, log = Clock(), []
    routes = {("s.com", "/sitemap.xml"): httpx.Response(403), ("s.com", "/sitemap_index.xml"): httpx.Response(200)}
    t = transport_for(routes, log, clock)
    codes = asyncio.run(_get_all(t, ["https://s.com/sitemap.xml", "https://s.com/sitemap_index.xml",
                                     "http://s.com/sitemap.xml"]))
    assert codes == [403, 404, 404] and len(log) == 1                   # 换 http、换地址都不再请求
    assert "s.com" in t.refused and t.errors["s.com"] == "HTTP 403"
    t2 = transport_for({("x.com", "/a.xml"): httpx.Response(200)}, log, clock)
    t2.close_host("x.com", "读 sitemap 超时")                            # 超时后关掉这个网站
    assert asyncio.run(_get_all(t2, ["https://x.com/a.xml"])) == [404]


def test_discover_timeout_closes_host(cfg):
    async def seed(host):
        await asyncio.sleep(10)
    brands = {"costco": cfg.brands["costco"]}
    pages, st = asyncio.run(off.discover_async(brands, {"official_discover_timeout_seconds": 0.01}, seed))
    assert [f[2] for f in st.sitemap_failed] == ["读 sitemap 超时", "读 sitemap 超时"]
    assert [p["origin"] for p in pages] == ["homepage", "homepage"]


def test_robots_gate_reasons_and_429():
    def handler(req):
        return {"down.com": httpx.Response(503), "deny.com": httpx.Response(200, text="User-agent: *\nDisallow: /"),
                "busy.com": httpx.Response(429), "ok.com": httpx.Response(404)}[req.url.host]
    clock, refused = Clock(), set()
    gate = off.RobotsGate(off.SiteClock(5.0, clock), refused, client=httpx.Client(transport=httpx.MockTransport(handler)),
                          sleep=clock.ssleep)
    assert gate("https://down.com/a") == (False, "robots.txt 读不到")
    assert gate("https://deny.com/a") == (False, "robots.txt 不允许")
    assert gate("https://busy.com/a") == (False, "robots.txt 读不到") and "busy.com" in refused
    assert gate("https://ok.com/a") == (True, "")


def test_crawl_spacing_includes_robots_fetch_and_max_three_sites():
    clock, fetched = Clock(), []

    def robots_handler(req):
        fetched.append((req.url.host, "robots", clock.t))
        return httpx.Response(404)                                     # 没有 robots.txt：不限制
    refused = set()
    sclock = off.SiteClock(5.0, clock)
    gate = off.RobotsGate(sclock, refused, client=httpx.Client(transport=httpx.MockTransport(robots_handler)),
                          sleep=clock.ssleep)

    class TimedCrawler(FakeCrawler):
        async def arun(self, url, config=None):
            fetched.append((url.split("/")[2], "page", clock.t))
            return await super().arun(url, config)
    pages = [{"brand_id": "b", "region": "US", "url": f"https://site{h}.com/{i}"} for h in range(5) for i in range(3)]
    crawler = TimedCrawler({})
    res = asyncio.run(off.crawl_pages(pages, crawler, {}, gate, clock.asleep, None, sclock, refused))
    assert len(res) == 15 and all(r.status == "ok" for r in res)
    assert crawler.peak <= 3
    for h in range(5):
        ts = [t for host, _, t in fetched if host == f"site{h}.com"]
        assert len(ts) == 4 and all(b - a >= 5.0 for a, b in zip(ts, ts[1:]))   # robots.txt → 第一页也隔 5 秒


def test_redirect_target_checked_against_robots():
    redirected = SimpleNamespace(**vars(ok(page_md(BB_CA))), redirected_url="https://www.bestbuy.ca/private/x")
    crawler = FakeCrawler({"https://www.bestbuy.ca/en-ca": redirected})
    robots = lambda u: (False, "robots.txt 不允许") if "/private/" in u else (True, "")
    res = asyncio.run(off.crawl_pages([{"brand_id": "bestbuy", "region": "CA", "url": "https://www.bestbuy.ca/en-ca"}],
                                      crawler, {}, robots, FakeSleep()))
    r = res[0]
    assert r.status == "blocked" and "跳转后robots.txt 不允许" in r.reason and r.excerpt == "" and r.hash == ""
    assert off.update_state({}, res, NOW.isoformat()) == []           # 不交给 AI


def test_total_timeout_is_error_without_retry():
    class Hang(FakeCrawler):
        async def arun(self, url, config=None):
            self.calls.append(url)
            await asyncio.Event().wait()
    crawler = Hang({})
    res = asyncio.run(off.crawl_pages([{"brand_id": "b", "region": "US", "url": "https://slow.com/"}], crawler,
                                      {"official_page_timeout_seconds": 0.05}, lambda u: True, FakeSleep()))
    assert crawler.calls == ["https://slow.com/"] and res[0].status == "error" and res[0].reason.startswith("超时")


def test_empty_pages_keep_hash_not_success_and_listed_for_review(cfg, tmp_path):
    pages = [{"brand_id": "nordstrom", "region": "US", "url": "https://www.nordstrom.com", "origin": "homepage"}]
    pp, sp, rp = _setup(tmp_path, pages)
    crawler = FakeCrawler({"https://www.nordstrom.com": ok(page_md("Holiday Sale starts November 20."))})
    kw = dict(crawler_factory=lambda: crawler, robots_ok=lambda u: True, sleep=FakeSleep())
    run1, state = off.run_official(cfg.brands, {}, pp, sp, rp, NOW, **kw)
    off.finish_official(state, run1, run1.items, NOW.isoformat(), sp, rp)
    old_hash = json.loads(sp.read_text(encoding="utf-8"))["pages"]["https://www.nordstrom.com"]["hash"]
    crawler.results["https://www.nordstrom.com"] = ok("# Nordstrom\n\nSign in\n\nYour bag is empty")
    run2, _ = off.run_official(cfg.brands, {}, pp, sp, rp, NOW, **kw)
    r = run2.results[0]
    assert r.status == "empty" and r.reason.startswith("无活动文字（正文 ") and run2.items == []
    saved = json.loads(sp.read_text(encoding="utf-8"))["pages"]["https://www.nordstrom.com"]
    assert saved["status"] == "empty" and saved["hash"] == old_hash               # 不覆盖已有的哈希
    assert run2.site_status() == {"nordstrom.com": "empty"} and "没有活动文字 1" in run2.describe()
    review = json.loads(rp.read_text(encoding="utf-8"))
    assert [x["url"] for x in review] == ["https://www.nordstrom.com"] and review[0]["reason"].startswith("无活动文字")


# ---------- 第 0 条：不隐藏自动化标记 ----------
def test_evasion_flags_stripped():
    assert off.strip_evasion_flags(["--no-sandbox", "--disable-blink-features=AutomationControlled",
                                    "--disable-infobars", "--disable-blink-features=Foo,AutomationControlled"]) == \
        ["--no-sandbox", "--disable-blink-features=Foo"]
    pytest.importorskip("crawl4ai")
    from crawl4ai import browser_manager as bm
    off.patch_crawl4ai()
    bc = off.browser_config()
    flags = bm.ManagedBrowser.build_browser_flags(bc)
    args = bm.BrowserManager(browser_config=bc)._build_browser_args()["args"]
    for lst in (flags, args):
        assert not any("AutomationControlled" in a for a in lst) and "--disable-infobars" not in lst
    assert not bc.enable_stealth and bc.user_agent_mode != "random" and not bc.init_scripts and not bc.extra_args
    rc = off.run_config()
    assert not (rc.magic or rc.simulate_user or rc.override_navigator) and rc.max_retries == 0


def test_real_browser_exposes_navigator_webdriver():
    """真实启动一次 Chromium（只打开本地 raw: 页面，不联网），navigator.webdriver 必须是 true。"""
    pytest.importorskip("crawl4ai")
    import re
    from crawl4ai import AsyncWebCrawler, CacheMode, CrawlerRunConfig

    async def go():
        async with AsyncWebCrawler(config=off.browser_config()) as c:
            r = await c.arun("raw:<html><body><p id=x>x</p></body></html>", config=CrawlerRunConfig(
                cache_mode=CacheMode.BYPASS, verbose=False,
                js_code="document.getElementById('x').textContent = 'webdriver=' + navigator.webdriver;"))
            return r.html or ""
    try:
        html = asyncio.run(go())
    except Exception as e:                      # 只有找不到浏览器（没装 Playwright 浏览器，例如 CI）才跳过
        if "Executable doesn't exist" in str(e) or "playwright install" in str(e):
            pytest.skip("没装 Playwright 浏览器")
        raise
    m = re.search(r"webdriver=(\w+)", html)
    assert m and m.group(1) == "true"


# ======================= S15 一审 ③ 第二轮 =======================
def test_site_clock_same_key_with_and_without_www():
    clock = Clock()
    c = off.SiteClock(5.0, clock)
    assert c.reserve("www.abercrombie.com") == 0
    assert c.reserve("abercrombie.com") == 5.0            # robots.txt（去掉 www）和页面（带 www）共用间隔
    assert c.reserve("WWW.Abercrombie.COM.") == 10.0
    assert c.reserve("www.amazon.com") == 0
    assert off.SiteClock.key("www.Amazon.com.") == "amazon.com"


def test_robots_fetch_then_www_page_waits_five_seconds():
    clock, fetched = Clock(), []
    sclock = off.SiteClock(5.0, clock)
    gate = off.RobotsGate(sclock, set(), client=httpx.Client(transport=httpx.MockTransport(
        lambda req: fetched.append(("robots", clock.t)) or httpx.Response(404))), sleep=clock.ssleep)

    class TimedCrawler(FakeCrawler):
        async def arun(self, url, config=None):
            fetched.append(("page", clock.t))
            return await super().arun(url, config)
    pages = [{"brand_id": "abercrombie", "region": "US", "url": "https://www.abercrombie.com/shop/us"}]
    asyncio.run(off.crawl_pages(pages, TimedCrawler({}), {}, gate, clock.asleep, None, sclock, set()))
    assert [k for k, _ in fetched] == ["robots", "page"] and fetched[1][1] - fetched[0][1] >= 5.0


def test_hosts_refused_with_429_in_discovery_not_crawled(cfg, tmp_path, monkeypatch):
    pages = [{"brand_id": "bestbuy", "region": "CA", "url": "https://www.bestbuy.ca/en-ca", "origin": "homepage"},
             {"brand_id": "costco", "region": "US", "url": "https://www.costco.com", "origin": "homepage"}]
    pp, sp, rp = _setup(tmp_path, pages)

    async def fake_discover(brands, settings, *a, **k):
        st = off.DiscoverStats(sites=2, homepage_pages=2)
        st.refused = {"www.bestbuy.ca": "HTTP 429", "www.costco.com": "HTTP 403"}
        return [dict(p, discovered="2026-11-01") for p in pages], st
    monkeypatch.setattr(off, "discover_async", fake_discover)
    crawler = FakeCrawler({})
    run, _ = off.run_official(cfg.brands, {}, pp, sp, rp, NOW, discover=True, crawler_factory=lambda: crawler,
                              robots_ok=lambda u: True, sleep=FakeSleep())
    assert crawler.calls == ["https://www.costco.com"]                # 429 的不抓；403 两个阶段分开记，照常抓
    r = {x.url: x for x in run.results}["https://www.bestbuy.ca/en-ca"]
    assert r.status == "skipped" and "HTTP 429" in r.reason


def test_language_and_irrelevant_pages_excluded():
    assert not off.region_ok("https://www.nike.com/ca/de/sale", "CA")
    assert not off.region_ok("https://www.apple.com/us/zh/holiday", "US")
    assert not off.region_ok("https://www.apple.com/us/es-mx/holiday", "US")
    assert off.region_ok("https://www.nike.com/ca/fr/soldes", "CA")              # 加拿大可以用法语
    assert not off.region_ok("https://www.nike.com/fr/soldes", "US")
    assert off.region_ok("https://www.nike.com/ca/en/sale", "CA")
    urls = ["https://www.costco.com/black-friday.html", "https://www.costco.com/legal/sale-terms.html",
            "https://www.costco.com/privacy-holiday.html", "https://www.costco.com/careers-events.html",
            "https://www.costco.com/easter-sale.html", "https://www.costco.com/valentines-day-deals.html"]
    assert [u for u, _ in off.pick_pages(urls, "https://www.costco.com", "US")] == \
        ["https://www.costco.com/black-friday.html"]


def test_site_status_partial_blocked_is_not_success():
    run = off.OfficialRun(results=[
        off.PageResult("a", "US", "https://a.com/1", "ok"), off.PageResult("a", "US", "https://a.com/2", "blocked"),
        off.PageResult("a", "US", "https://a.com/3", "skipped"),
        off.PageResult("b", "US", "https://b.com/1", "ok"), off.PageResult("b", "US", "https://b.com/2", "ok"),
        off.PageResult("c", "US", "https://c.com/1", "blocked"),
        off.PageResult("d", "US", "https://d.com/1", "empty"), off.PageResult("e", "US", "https://e.com/1", "error")])
    assert run.site_status() == {"a.com": "partial", "b.com": "ok", "c.com": "blocked", "d.com": "empty",
                                 "e.com": "error"}
    d = run.describe()
    assert "全部成功 1、部分被拒 1、全部被拒 1、没有活动文字 1、出错 1" in d and "部分被拒：a.com" in d


def test_two_consecutive_errors_or_empties_stop_the_site(tmp_path, cfg):
    def nojs(n):
        return ok("# Shop\n\nSign in " * n)                           # 没有活动文字 → empty
    err = SimpleNamespace(success=False, status_code=500, error_message="net::ERR_FAILED", markdown=None, metadata={})
    pages = [{"brand_id": "b", "region": "US", "url": f"https://e.com/{i}"} for i in range(4)] + \
            [{"brand_id": "b", "region": "US", "url": f"https://m.com/{i}"} for i in range(4)] + \
            [{"brand_id": "b", "region": "US", "url": f"https://x.com/{i}"} for i in range(4)]
    crawler = FakeCrawler({"https://e.com/0": err, "https://e.com/1": err,
                           "https://m.com/0": nojs(1), "https://m.com/1": nojs(2),
                           "https://x.com/0": err, "https://x.com/1": ok(page_md(BB_CA)), "https://x.com/2": err,
                           "https://x.com/3": nojs(1)})
    res = asyncio.run(off.crawl_pages(pages, crawler, {}, lambda u: True, FakeSleep()))
    by = {r.url: r for r in res}
    assert [by[f"https://e.com/{i}"].status for i in range(4)] == ["error", "error", "stopped", "stopped"]
    assert [by[f"https://m.com/{i}"].status for i in range(4)] == ["empty", "empty", "stopped", "stopped"]
    assert [by[f"https://x.com/{i}"].status for i in range(4)] == ["error", "ok", "error", "empty"]   # 不连续：不停
    assert "https://e.com/2" not in crawler.calls and "连续 2 次出错" in by["https://e.com/2"].reason
    state = {}
    off.update_state(state, res, NOW.isoformat())
    assert state["pages"]["https://e.com/2"]["status"] == "stopped"
    assert "https://e.com/2" not in [x["url"] for x in off.manual_review(state)]     # 停掉的不算被拒
