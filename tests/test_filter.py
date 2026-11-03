from __future__ import annotations

from datetime import date

import pytest

from dealradar.filter import BrandMatcher, mark_seen, prefilter, prune_seen

CUTOFF = date(2026, 10, 1)


@pytest.fixture(scope="module")
def matcher(cfg):
    return BrandMatcher(cfg.brands)


def deal(make_item, **kw):
    base = dict(source="slickdeals", feed="slickdeals", brand_hints=[], source_url=None, source_name="Slickdeals")
    base.update(kw)
    return make_item(**base)


def test_old_news_dropped(make_item, matcher):
    items = [
        make_item(guid="a", link="la", published="2025-11-20T12:00:00+00:00"),
        make_item(guid="b", link="lb", published="2026-09-30T23:59:00+00:00"),
        make_item(guid="c", link="lc", published="2026-10-01T00:00:00+00:00"),
        make_item(guid="d", link="ld", published=None),
    ]
    kept, st = prefilter(items, {}, matcher, CUTOFF)
    assert [i["guid"] for i in kept] == ["c", "d"]
    assert st.old == 2 and st.no_date == 1


def test_seen_by_guid_or_link(make_item, matcher):
    items = [make_item(guid="g1", link="l1"), make_item(guid="g2", link="l2"), make_item(guid="g3", link="l3")]
    kept, st = prefilter(items, {"g1": "2026-10-30", "l2": "2026-10-30"}, matcher, CUTOFF)
    assert [i["guid"] for i in kept] == ["g3"]
    assert st.dup_seen == 2


def test_duplicate_in_same_run_merges_hints(make_item, matcher):
    items = [make_item(guid="g", link="l", region_hint="US", brand_hints=["nike"]),
             make_item(guid="g", link="l", region_hint="CA", brand_hints=["adidas"])]
    kept, st = prefilter(items, {}, matcher, CUTOFF)
    assert len(kept) == 1 and st.dup_in_run == 1
    assert kept[0]["region_hint"] == "US/CA"
    assert set(kept[0]["brand_hints"]) == {"nike", "adidas"}


def test_deal_feed_needs_brand(make_item, matcher):
    items = [
        deal(make_item, guid="1", link="1", title="Kids toys 50% off at Toy Planet"),
        deal(make_item, guid="2", link="2", title="Apple AirPods Pro $169 at Amazon", summary="on sale today"),
    ]
    kept, st = prefilter(items, {}, matcher, CUTOFF)
    assert [i["guid"] for i in kept] == ["2"]
    assert st.no_brand == 1
    assert set(kept[0]["brand_hints"]) == {"apple", "amazon"}


def test_gnews_keeps_query_brand_without_name_in_text(make_item, matcher):
    kept, _ = prefilter([make_item(title="Early Black Friday deals are here", summary="")], {}, matcher, CUTOFF)
    assert kept and kept[0]["brand_hints"] == ["nike"]


def test_needs_sale_keyword(make_item, matcher):
    kept, st = prefilter([make_item(title="Nike reports quarterly earnings", summary="")], {}, matcher, CUTOFF)
    assert kept == [] and st.no_keyword == 1
    for t in ["Nike Cyber Monday", "Nike Boxing Day", "40% off at Nike", "Nike deal", "Nike sale"]:
        assert prefilter([make_item(title=t)], {}, matcher, CUTOFF)[0], t


def test_cap_keeps_newest(make_item, matcher):
    items = [make_item(guid=f"g{i}", link=f"l{i}", published=f"2026-10-{1 + i % 30:02d}T{i % 24:02d}:00:00+00:00")
             for i in range(350)]
    kept, st = prefilter(items, {}, matcher, CUTOFF, max_items=300)
    assert len(kept) == 300 and st.over_cap == 50
    assert kept[0]["published"] >= kept[-1]["published"]


def test_brand_matcher_case_rules(matcher):
    assert matcher.match("Mind the gap between prices") == []
    assert "gap" in matcher.match("Gap Factory: 50% off everything")
    assert matcher.match("target audience sale") == []
    assert "target" in matcher.match("Target Black Friday ad")
    assert "adidas" in matcher.match("Adidas sale") and "adidas" in matcher.match("adidas sale")
    assert "levis" in matcher.match("Levi’s Black Friday")
    assert "homedepot" in matcher.match("home depot black friday")
    assert "nike" not in matcher.match("Nikestyle deals")


def test_seen_mark_and_prune(make_item):
    seen = {}
    mark_seen(seen, [make_item(guid="g", link="l")], "2026-11-01")
    assert seen == {"g": "2026-11-01", "l": "2026-11-01"}
    seen["old"] = "2026-07-01"
    assert "old" not in prune_seen(seen, date(2026, 11, 1), 90)


# ---------- S3 v2：Google News 和 Bing 去重、非北美过滤 ----------
from dealradar.filter import is_seen, non_na_reason, title_key  # noqa: E402


def bing(make_item, **kw):
    base = dict(source="bing", feed="bing-CA-nike", region_hint="CA", source_name="IGN",
                link="https://www.ign.com/articles/nike-bf", guid="https://www.ign.com/articles/nike-bf",
                source_url="https://www.ign.com")
    base.update(kw)
    return make_item(**base)


def test_title_key_strips_google_source_suffix(make_item):
    g = make_item(title="Nike’s Black Friday Sale Is Live for 2026 - IGN", source_name="IGN")
    b = bing(make_item, title="Nike's Black Friday sale is live for 2026")
    assert title_key(g) == title_key(b) == "nike s black friday sale is live for 2026"
    assert title_key(make_item(source="slickdeals", title="Nike Black Friday sale is live")) is None
    assert title_key(make_item(title="Nike sale - X")) is None   # 太短的不用来去重


def test_bing_and_google_same_article_dedup_prefers_bing(make_item, matcher):
    g = make_item(guid="CBMiG1", link="https://news.google.com/rss/articles/CBMiG1",
                  title="Nike Black Friday sale starts Nov. 20 - IGN", source_name="IGN",
                  summary="Nike Black Friday sale starts Nov. 20  IGN", region_hint="US")
    g_ca = dict(g, region_hint="CA", feed="gnews-CA-nike")            # 同一条 Google 新闻，加拿大查询
    b = bing(make_item, title="Nike Black Friday sale starts Nov. 20", summary="Members get early access.",
             brand_hints=["nike", "adidas"])
    kept, st = prefilter([g, b, g_ca], {}, matcher, CUTOFF)
    assert len(kept) == 1 and st.dup_in_run == 2 and st.dup_title == 1
    it = kept[0]
    assert it["source"] == "bing" and it["link"] == "https://www.ign.com/articles/nike-bf"   # 用 Bing 版本
    assert it["summary"] == "Members get early access."
    assert it["region_hint"] == "US/CA" and set(it["brand_hints"]) == {"nike", "adidas"}
    # Google 版本的 guid 也记进 seen，下次 Google 那条单独出现也认得
    seen = {}
    mark_seen(seen, kept, "2026-11-01")
    assert "CBMiG1" in seen
    assert is_seen(g, seen)
    kept2, st2 = prefilter([g], seen, matcher, CUTOFF)
    assert kept2 == [] and st2.dup_seen == 1


def test_same_title_within_one_engine_is_not_merged(make_item, matcher):
    # 同一个搜索引擎里标题相同、链接不同（不同网站转载）：仍算两条
    a = bing(make_item, guid="https://a.com/1", link="https://a.com/1", title="Nike Black Friday sale starts Nov. 20")
    b = bing(make_item, guid="https://b.com/2", link="https://b.com/2", title="Nike Black Friday sale starts Nov. 20")
    kept, st = prefilter([a, b], {}, matcher, CUTOFF)
    assert len(kept) == 2 and st.dup_title == 0


def test_non_north_america_filtered(make_item, matcher):
    cases = {
        "gbp": make_item(guid="1", link="1", title="Nike Black Friday deals: trainers from £45"),
        "eur": make_item(guid="2", link="2", title="Nike sale", summary="Jetzt 30% Rabatt, ab 49,99 €"),
        "aud": make_item(guid="3", link="3", title="Nike Black Friday sale", summary="Shoes from A$89"),
        "aud2": make_item(guid="4", link="4", title="Nike Black Friday sale: AU$120 off"),
        "couk": make_item(guid="5", link="5", source_url="https://www.independent.co.uk"),
        "comau": bing(make_item, guid="6", link="https://www.news.com.au/x", source_url="https://www.news.com.au",
                      title="Nike Black Friday sale: what to expect"),
        "de": make_item(guid="7", link="7", source_url="https://www.chip.de"),
        "in": make_item(guid="8", link="8", source_url="https://www.livemint.in"),
        "uk": make_item(guid="9", link="9", title="Nike Black Friday sale UK: best deals"),
        "aus": make_item(guid="10", link="10", title="Nike Black Friday Australia deals"),
        "ie": make_item(guid="11", link="11", title="Black Friday in Ireland: Nike sale"),
    }
    ok = {
        "cad": make_item(guid="20", link="20", title="Nike Black Friday sale: CA$40 off", summary="C$ prices"),
        "bc": make_item(guid="21", link="21", title="Nike Black Friday sale in British Columbia"),
        "indiana": make_item(guid="22", link="22", title="Nike Black Friday sale at Indiana malls"),
        "usd": make_item(guid="23", link="23", title="Nike Black Friday: $40 off", source_url="https://www.cbc.ca"),
    }
    for name, it in cases.items():
        assert non_na_reason(it), name
    for name, it in ok.items():
        assert non_na_reason(it) is None, name
    kept, st = prefilter(list(cases.values()) + list(ok.values()), {}, matcher, CUTOFF)
    assert sorted(i["guid"] for i in kept) == ["20", "21", "22", "23"]
    assert st.non_na == len(cases) and sum(st.non_na_reasons.values()) == len(cases)
    assert "非北美 11" in st.describe()


def test_bing_items_keep_query_brand_like_google(make_item, matcher):
    kept, st = prefilter([bing(make_item, title="Early Black Friday deals are here", summary="")], {}, matcher, CUTOFF)
    assert kept and kept[0]["brand_hints"] == ["nike"] and st.no_brand == 0


# ---------- 第二轮：抓到正文的条目优先进入上限 ----------
from dealradar.filter import FilterStats, apply_cap  # noqa: E402


def test_cap_prefers_items_with_excerpt(make_item, matcher):
    items = [make_item(guid=f"n{i}", link=f"n{i}", title=f"Nike sale story {i}",
                       published=f"2026-10-{20 + i}T00:00:00+00:00") for i in range(8)]
    old = [make_item(guid=f"o{i}", link=f"o{i}", title=f"Nike Black Friday dates {i}", excerpt="Nike starts Nov. 20.",
                     published=f"2026-10-0{2 + i}T00:00:00+00:00") for i in range(2)]
    allk, st = prefilter(items + old, {}, matcher, CUTOFF, max_items=None)   # 先不套上限
    assert len(allk) == 10 and st.over_cap == 0
    kept = apply_cap(allk, 5, st)
    assert [i["guid"] for i in kept] == ["o1", "o0", "n7", "n6", "n5"]   # 带正文的先入选，其余新的优先
    assert st.over_cap == 5 and st.kept == 5 and st.kept_with_excerpt == 2
    assert "其中带正文 2 条" in st.describe()
    assert apply_cap(allk, None, FilterStats()) and len(apply_cap(allk, None, FilterStats())) == 10


def test_canadian_media_feed_needs_brand_like_deal_feeds(make_item, matcher):
    items = [make_item(source="gnews-camedia1", feed="gnews-camedia1", region_hint="CA", brand_hints=[],
                       guid="c1", link="c1", title="Black Friday shoppers line up at Toronto malls - CBC",
                       source_url="https://www.cbc.ca"),
             make_item(source="gnews-camedia1", feed="gnews-camedia1", region_hint="CA", brand_hints=[],
                       guid="c2", link="c2", title="Canadian Tire Black Friday flyer: sale starts Nov. 20 - Retail Insider",
                       source_url="https://retail-insider.com")]
    kept, st = prefilter(items, {}, matcher, CUTOFF)
    assert [i["guid"] for i in kept] == ["c2"] and st.no_brand == 1
    assert kept[0]["brand_hints"] == ["canadiantire"]
