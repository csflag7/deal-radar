from __future__ import annotations

import httpx
import pytest

from dealradar.articles import (DATE_RE, ArticleStats, eligible, extract_paragraphs, fetch_articles, make_excerpt,
                                rank, relevance)
from dealradar.filter import BrandMatcher

FILLER = ("Shoppers have plenty of options this holiday season and stores keep adding new promotions every week "
          "so it pays to compare prices before you buy anything big. ")


def page(*paras, extra=""):
    body = "".join(f"<p>{p}</p>" for p in paras)
    return (f"<html><head><title>t</title><script>var nike = 'Nov. 1 script text';</script>"
            f"<style>.p{{color:red}}</style></head><body>"
            f"<nav><a>Nike deals Nov. 2 menu</a></nav>{extra}<article>{body}</article>"
            f"<footer>Nike footer Dec. 31 copyright</footer></body></html>")


LONG = [FILLER * 2] * 4      # 约 150 个词的无关正文


def news(make_item, **kw):
    base = dict(source="bing", feed="bing-US-nike", link="https://news.example.com/nike-bf",
                guid="https://news.example.com/nike-bf", source_url="https://news.example.com",
                title="Nike Black Friday 2026 sale dates announced")
    base.update(kw)
    return make_item(**base)


@pytest.fixture(scope="module")
def nike(cfg):
    return BrandMatcher({"nike": cfg.brands["nike"]})


@pytest.fixture
def settings(cfg):
    return dict(cfg.settings)


def transport(routes, log=None):
    """routes: {(host, path): Response 或 callable}；没写的路径 404。"""
    def handler(req):
        if log is not None:
            log.append((req.url.host, req.url.path, req.headers.get("User-Agent")))
        r = routes.get((req.url.host, req.url.path))
        if callable(r):
            return r(req)
        return r if r is not None else httpx.Response(404)
    return httpx.MockTransport(handler)


def html_response(text):
    return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"}, content=text.encode("utf-8"))


# ---------- 正文提取和截段 ----------
def test_extract_drops_script_style_nav_footer():
    paras, words = extract_paragraphs(page("Nike Black Friday starts Nov. 20.", *LONG))
    text = " ".join(paras)
    assert "Nike Black Friday starts Nov. 20." in paras
    for junk in ("script text", "color:red", "menu", "copyright"):
        assert junk not in text
    assert words >= 150


def test_short_page_counts_as_failure_words():
    _, words = extract_paragraphs("<html><body><div id=root>Loading…</div><p>Enable JavaScript to read.</p></body></html>")
    assert words < 150


def test_excerpt_keeps_brand_and_date_paragraphs_with_neighbors(nike):
    paras = ["Intro about the weather today.", "Before the news.", "Nike will start its Black Friday sale.",
             "After the news: details follow.", "Totally unrelated paragraph.", "Unrelated again.",
             "Doors open Friday at 6 a.m.", "Way later unrelated.", "Final unrelated words."]
    ex = make_excerpt(paras, nike)
    assert ex.split("\n") == ["Before the news.", "Nike will start its Black Friday sale.",
                              "After the news: details follow.", "...", "Unrelated again.",
                              "Doors open Friday at 6 a.m.", "Way later unrelated."]
    assert "Intro" not in ex and "Totally" not in ex and "Final" not in ex


def test_excerpt_length_capped_and_prioritized(nike):
    paras = []
    for i in range(60):
        paras.append(f"Filler paragraph number {i} with some ordinary words in it, nothing to see. " * 2)
        paras.append(f"Other store deal number {i} runs Nov. {i % 28 + 1}, details inside the article text.")
    paras[50] = "Nike says its Black Friday sale starts Nov. 20 with up to 50% off sitewide."
    ex = make_excerpt(paras, nike, max_chars=2500)
    assert len(ex) <= 2500
    assert "Nike says its Black Friday sale starts Nov. 20" in ex   # 品牌 + 日期的段落一定留下
    assert ex.count("\n...\n") >= 1


def test_long_paragraph_is_windowed(nike):
    para = ("word " * 600) + "Nike sale starts Nov. 20. " + ("tail " * 600)
    ex = make_excerpt([para], nike, max_chars=2500)
    assert "Nike sale starts Nov. 20." in ex and len(ex) <= 800


def test_date_regex_ignores_black_friday_and_cyber_monday():
    assert not DATE_RE.search("Black Friday deals and Cyber Monday deals")
    for s in ("starts Nov. 20", "through December", "on Thursday", "11/27", "the 27th of November", "Dec 1"):
        assert DATE_RE.search(s), s
    assert not DATE_RE.search("Marketing 20 deals")


# ---------- 挑哪些条目抓 ----------
def test_eligible_only_news_with_original_link(make_item):
    assert eligible(news(make_item), [])
    assert not eligible(make_item(), [])                                           # Google News 跳转链接
    assert not eligible(make_item(source="slickdeals", link="https://slickdeals.net/f/1"), [])   # deal 源有摘要
    assert not eligible(news(make_item, link="https://www.msn.com/en-us/x"), ["msn.com"])


def test_rank_prefers_brand_plus_event_and_schedule_titles(make_item, cfg):
    m = BrandMatcher(cfg.brands)
    a = news(make_item, guid="a", link="https://a.example.com/a", title="Holiday gift ideas for runners",
             published="2026-10-31T00:00:00+00:00")
    b = news(make_item, guid="b", link="https://b.example.com/b", title="Nike Black Friday deals roundup",
             published="2026-10-20T00:00:00+00:00")
    c = news(make_item, guid="c", link="https://c.example.com/c", title="Nike Black Friday 2026 sale dates announced",
             published="2026-10-10T00:00:00+00:00")
    d = news(make_item, guid="d", link="https://d.example.com/d", title="When does the holiday sale start?",
             published="2026-10-30T00:00:00+00:00")
    assert relevance(c, m) == 3 and relevance(b, m) == 2 and relevance(d, m) == 1 and relevance(a, m) == 0
    assert [x["guid"] for x in rank([a, b, c, d, make_item()], m, [])] == ["c", "b", "d", "a"]


# ---------- 联网（全部用 MockTransport，不联网；时间用假时钟） ----------
class FakeClock:
    """sleep 只推进假时间，不真的等。"""

    def __init__(self):
        self.t = 1000.0
        self.sleeps = []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


def run_fetch(items, cfg, settings, routes, calls=None, clock=None):
    clock = clock or FakeClock()
    log = [] if calls is None else calls

    def handler(req):
        log.append((req.url.host, req.url.path, req.headers.get("User-Agent"), clock.t))
        r = routes.get((req.url.host, req.url.path))
        if callable(r):
            return r(req)
        return r if r is not None else httpx.Response(404)
    st = fetch_articles(items, cfg.brands, settings, sleep=clock.sleep, clock=clock,
                        transport=httpx.MockTransport(handler))
    return st, log, clock


def test_robots_disallow_skips_article(make_item, cfg, settings):
    routes = {
        ("blocked.example.com", "/robots.txt"): httpx.Response(200, text="User-agent: *\nDisallow: /\n"),
        ("blocked.example.com", "/a"): html_response(page("Nike Black Friday starts Nov. 20.", *LONG)),
        ("blocked.example.com", "/b"): html_response(page("Nike Black Friday starts Nov. 20.", *LONG)),
        ("ok.example.com", "/robots.txt"): httpx.Response(200, text="User-agent: *\nDisallow: /private/\n"),
        ("ok.example.com", "/c"): html_response(page("Nike Black Friday sale starts Nov. 20.", *LONG)),
    }
    items = [news(make_item, guid="a", link="https://blocked.example.com/a"),
             news(make_item, guid="b", link="https://blocked.example.com/b"),
             news(make_item, guid="c", link="https://ok.example.com/c")]
    st, calls, _ = run_fetch(items, cfg, settings, routes)
    assert st.robots_blocked == 2 and st.attempted == 1 and st.ok == 1 and st.success_rate == 1.0
    paths = [(h, p) for h, p, _, _ in calls]
    assert ("blocked.example.com", "/a") not in paths and ("blocked.example.com", "/b") not in paths
    assert paths.count(("blocked.example.com", "/robots.txt")) == 1          # 同一网站只读一次
    assert "excerpt" not in items[0] and "Nike Black Friday sale starts Nov. 20." in items[2]["excerpt"]
    # 下载正文和读 robots.txt 用同一个名字：DealRadar
    assert all(ua == "DealRadar/0.1 (personal deal calendar)" for _, _, ua, _ in calls)


def test_robots_unreadable_or_forbidden_means_skip(make_item, cfg, settings):
    routes = {
        ("err.example.com", "/robots.txt"): httpx.Response(503),
        ("forbid.example.com", "/robots.txt"): httpx.Response(403),
        ("none.example.com", "/c"): html_response(page("Nike Black Friday starts Nov. 20.", *LONG)),
    }   # none.example.com 没有 robots.txt（404）：不限制
    items = [news(make_item, guid="a", link="https://err.example.com/a"),
             news(make_item, guid="b", link="https://forbid.example.com/b"),
             news(make_item, guid="c", link="https://none.example.com/c")]
    st, _, _ = run_fetch(items, cfg, settings, routes)
    assert st.robots_blocked == 2 and st.ok == 1


def test_failures_only_counted_and_limit(make_item, cfg, settings):
    routes = {
        ("a.example.com", "/short"): html_response("<html><body><p>Please enable JavaScript.</p></body></html>"),
        ("b.example.com", "/gone"): httpx.Response(404),
        ("c.example.com", "/pdf"): httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"%PDF"),
        ("d.example.com", "/boom"): lambda req: (_ for _ in ()).throw(httpx.ReadTimeout("slow", request=req)),
        ("e.example.com", "/norel"): html_response(page(*LONG)),
        ("f.example.com", "/good"): html_response(page("Nike Black Friday sale starts Nov. 20.", *LONG)),
        ("g.example.com", "/over"): html_response(page("Nike Black Friday sale starts Nov. 20.", *LONG)),
    }
    names = [("a", "short"), ("b", "gone"), ("c", "pdf"), ("d", "boom"), ("e", "norel"), ("f", "good"), ("g", "over")]
    items = [news(make_item, guid=n, link=f"https://{h}.example.com/{n}", published=f"2026-10-{30 - i}T00:00:00+00:00")
             for i, (h, n) in enumerate(names)]
    settings["max_articles_per_run"] = 6
    st, calls, _ = run_fetch(items, cfg, settings, routes)
    assert st.attempted == 6 and st.ok == 1                 # 第 7 篇超过上限，不抓
    assert st.failed == {"too_short": 1, "http": 2, "error": 1, "no_relevant": 1}
    assert [it.get("excerpt") is not None for it in items] == [False] * 5 + [True, False]
    assert ("g.example.com", "/over") not in [(h, p) for h, p, _, _ in calls]
    assert isinstance(st, ArticleStats) and "成功 1 篇" in st.describe() and "成功率 17%" in st.describe()


def test_pacing_per_site_and_overall(make_item, cfg, settings):
    good = html_response(page("Nike Black Friday sale starts Nov. 20.", *LONG))
    routes = {("x.example.com", f"/{i}"): good for i in range(5)}
    routes.update({("y.example.com", f"/{i}"): good for i in range(2)})
    items = [news(make_item, guid=f"x{i}", link=f"https://x.example.com/{i}") for i in range(5)] + \
            [news(make_item, guid=f"y{i}", link=f"https://y.example.com/{i}") for i in range(2)]
    st, calls, _ = run_fetch(items, cfg, settings, routes)
    # 同一网站最多 3 篇
    assert st.attempted == 5 and st.ok == 5 and st.site_cap == 2
    times = [t for _, _, _, t in calls]
    assert all(b - a >= 1.0 for a, b in zip(times, times[1:]))             # 任意两次请求间隔 ≥ 1 秒
    for host in ("x.example.com", "y.example.com"):
        ts = [t for h, _, _, t in calls if h == host]
        assert all(b - a >= 5.0 for a, b in zip(ts, ts[1:])), host        # 同一网站间隔 ≥ 5 秒（含 robots.txt）


def test_blocked_site_skipped_for_rest_of_run_no_retry(make_item, cfg, settings):
    hits = []
    routes = {("nj.example.com", f"/{i}"): (lambda req: hits.append(1) or httpx.Response(403)) for i in range(3)}
    routes[("ok.example.com", "/a")] = html_response(page("Nike Black Friday sale starts Nov. 20.", *LONG))
    routes[("slow.example.com", "/robots.txt")] = httpx.Response(429)
    items = [news(make_item, guid=f"n{i}", link=f"https://nj.example.com/{i}") for i in range(3)] + \
            [news(make_item, guid="s", link="https://slow.example.com/a"),
             news(make_item, guid="o", link="https://ok.example.com/a")]
    st, calls, _ = run_fetch(items, cfg, settings, routes)
    assert len(hits) == 1                                   # 403 之后不再请求这个网站，也不重试
    assert st.failed == {"blocked": 1} and st.site_blocked == 3 and st.ok == 1
    assert ("slow.example.com", "/a") not in [(h, p) for h, p, _, _ in calls]   # robots.txt 返回 429：整个网站跳过


def test_redirects_followed_manually_with_robots_each_hop(make_item, cfg, settings):
    good = html_response(page("Nike Black Friday sale starts Nov. 20.", *LONG))
    routes = {
        ("a.example.com", "/r"): httpx.Response(301, headers={"location": "https://b.example.com/final"}),
        ("b.example.com", "/final"): good,
        ("c.example.com", "/r"): httpx.Response(302, headers={"location": "https://d.example.com/private/x"}),
        ("d.example.com", "/robots.txt"): httpx.Response(200, text="User-agent: *\nDisallow: /private/\n"),
        ("d.example.com", "/private/x"): good,
        ("e.example.com", "/1"): httpx.Response(302, headers={"location": "/2"}),
        ("e.example.com", "/2"): httpx.Response(302, headers={"location": "/3"}),
        ("e.example.com", "/3"): httpx.Response(302, headers={"location": "/4"}),
        ("e.example.com", "/4"): httpx.Response(302, headers={"location": "/5"}),
        ("e.example.com", "/5"): good,
    }
    items = [news(make_item, guid="a", link="https://a.example.com/r"),
             news(make_item, guid="c", link="https://c.example.com/r"),
             news(make_item, guid="e", link="https://e.example.com/1")]
    st, calls, _ = run_fetch(items, cfg, settings, routes)
    paths = [(h, p) for h, p, _, _ in calls]
    assert items[0].get("excerpt")                                  # 跳到别的网站，核对过那边的 robots.txt
    assert ("b.example.com", "/robots.txt") in paths
    assert ("d.example.com", "/private/x") not in paths              # 跳转目标 robots 不允许：不请求
    assert ("e.example.com", "/5") not in paths                      # 最多跟 3 跳
    assert st.failed == {"robots": 1, "http": 1} and st.ok == 1


def test_skip_domains_do_not_use_slots(make_item, cfg, settings):
    items = [news(make_item, guid="m", link="https://www.msn.com/en-us/news/x"),
             make_item(guid="g")]   # Google News 条目
    st, _, _ = run_fetch(items, cfg, settings, {})
    assert st.candidates == 0 and st.skipped_domain == 1 and st.attempted == 0


# ---------- 第二轮：成功要有一段像正文、又提到品牌的段落 ----------
from dealradar.articles import has_brand_paragraph  # noqa: E402


def test_has_brand_paragraph_rules(nike):
    title = "Nike Black Friday 2026 sale dates announced"
    assert has_brand_paragraph("Intro.\nNike says its Black Friday sale starts Nov. 20 online.", nike, title)
    # 侧栏 / 相关文章标题：没有句末标点
    assert not has_brand_paragraph("Nike Black Friday deals worth grabbing for runners\n...\nDoors open Friday.",
                                   nike, title)
    # 文章标题本身不算
    assert not has_brand_paragraph("Nike Black Friday 2026 sale dates announced.\nShoppers line up early on Friday.",
                                   nike, title)
    # 太短的不算；提到日期但没提品牌的也不算
    assert not has_brand_paragraph("Nike: Nov. 20.\nThe sale starts Nov. 20 for all members online.", nike, title)


def test_sidebar_only_page_counts_as_failure(make_item, cfg, settings):
    sidebar = "".join(f"<div><a>Nike Black Friday deal pick number {i} you should not miss</a></div>"
                      for i in range(6))
    html = ("<html><body><h1>Nike Black Friday 2026 sale dates announced</h1>" + sidebar
            + "".join(f"<p>{FILLER * 2}</p>" for _ in range(4)) + "<p>Doors open Friday.</p></body></html>")
    routes = {("side.example.com", "/a"): html_response(html),
              ("ok.example.com", "/b"): html_response(page("Nike Black Friday sale starts Nov. 20.", *LONG))}
    items = [news(make_item, guid="a", link="https://side.example.com/a"),
             news(make_item, guid="b", link="https://ok.example.com/b")]
    st, _, _ = run_fetch(items, cfg, settings, routes)
    assert st.attempted == 2 and st.ok == 1 and st.failed == {"no_brand": 1}
    assert "excerpt" not in items[0] and items[1]["excerpt"]
    assert "没有提到品牌的正文段落 1" in st.describe()
