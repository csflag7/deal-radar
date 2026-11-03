from __future__ import annotations

from dealradar.status import domain_matches, judge, quote_found, source_level


def test_quote_exact_and_normalized(make_item):
    it = make_item(title="Nike Black Friday 2026 sale starts Nov. 20 - CNBC",
                   summary="Shoppers get  up to 50% off  sitewide. Members get early access.")
    assert quote_found("Nike Black Friday 2026 sale starts Nov. 20", it)
    assert quote_found("nike black   friday 2026 SALE starts nov. 20", it)
    assert quote_found("up to 50% off sitewide.", it)
    assert quote_found('"Members get early access."', it)


def test_quote_curly_punctuation(make_item):
    it = make_item(title="Levi’s Black Friday — extra 30% off", summary="")
    assert quote_found("Levi's Black Friday - extra 30% off", it)


def test_quote_not_in_text_is_rejected(make_item):
    it = make_item(title="Nike Black Friday sale starts Nov. 20", summary="Up to 50% off")
    assert not quote_found("Nike Black Friday sale starts Nov. 21", it)
    assert not quote_found("Cyber Monday deals", it)
    assert not quote_found("", it)
    assert not quote_found("...", it)
    # 不能把标题和摘要拼起来凑出引句
    assert not quote_found("starts Nov. 20 Up to 50% off", it)


def test_domain_matches():
    assert domain_matches("corporate.lululemon.com", "lululemon.com")
    assert domain_matches("nike.com", "www.nike.com")
    assert not domain_matches("notnike.com", "nike.com")
    assert not domain_matches("", "nike.com")


def test_status_official_domain(cfg, make_item):
    it = make_item(source_url="https://corporate.lululemon.com")
    assert source_level(it, cfg.brands["lululemon"], cfg.trusted_domains) == "official"
    deal = make_item(source="slickdeals", source_url=None, link="https://www.nike.com/promo/black-friday")
    assert source_level(deal, cfg.brands["nike"], cfg.trusted_domains) == "official"


def test_status_trusted_media(cfg, make_item):
    it = make_item(source_url="https://www.cnbc.com")
    assert source_level(it, cfg.brands["nike"], cfg.trusted_domains) == "media"
    it = make_item(source_url="https://www.theglobeandmail.com")
    assert source_level(it, cfg.brands["nike"], cfg.trusted_domains) == "media"


def test_status_other(cfg, make_item):
    it = make_item(source_url="https://sneakerblog.example.com")
    assert source_level(it, cfg.brands["nike"], cfg.trusted_domains) == "other"
    rfd = make_item(source="rfd", source_url=None, link="https://forums.redflagdeals.com/x")
    assert source_level(rfd, cfg.brands["nike"], cfg.trusted_domains) == "other"


def test_gnews_uses_source_href_not_redirect_link(cfg, make_item):
    # Google News 的 link 永远是 news.google.com；没有 source href 就不能算官宣
    it = make_item(link="https://news.google.com/rss/articles/X", source_url=None)
    assert source_level(it, cfg.brands["nike"], cfg.trusted_domains) == "other"
    it = make_item(link="https://news.google.com/rss/articles/X", source_url="https://news.nike.com")
    assert source_level(it, cfg.brands["nike"], cfg.trusted_domains) == "official"


def test_judge_drops_bad_quote_and_sets_status(cfg, make_item):
    good = make_item(title="Nike Black Friday sale starts Nov. 20", source_url="https://www.cnbc.com")
    rumor = make_item(title="Nike sale rumored for Nov. 22", source_url="https://blog.example.com", guid="r")
    ev = {"brand_id": "nike", "quote": "sale starts Nov. 20", "start": "2026-11-20"}
    dropped = {}
    out = judge([(good, ev), (good, dict(ev, quote="made up words")), (rumor, dict(ev, quote="rumored for Nov. 22", start="2026-11-22"))],
                cfg.brands, cfg.trusted_domains, dropped)
    assert [o["status"] for o in out] == ["confirmed", "rumored"]
    assert out[0]["source"]["level"] == "media" and out[0]["source"]["domain"] == "cnbc.com"
    assert sum(dropped.values()) == 1


# ---------- S3 v2：新的 confirmed 规则、正文引句 ----------
from dealradar.status import status_of  # noqa: E402


def test_confirmed_needs_good_source_and_date_or_discount():
    assert status_of("media", {"start": "2026-11-20"}) == "confirmed"
    assert status_of("official", {"discount": "up to 40% off"}) == "confirmed"
    assert status_of("media", {"start": None, "discount": None}) == "rumored"   # 只有标题：传闻
    assert status_of("official", {}) == "rumored"
    assert status_of("other", {"start": "2026-11-20", "discount": "50% off"}) == "rumored"
    assert status_of("media", {"end": "2026-11-30"}) == "rumored"               # 只有结束日期不够


def test_judge_per_evidence_status_and_fields(cfg, make_item):
    cnbc = make_item(title="Nike Black Friday sale is coming", summary="Nike Black Friday sale runs Nov. 20 to Nov. 30.",
                     source_url="https://www.cnbc.com")
    dropped = {}
    out = judge([(cnbc, {"brand_id": "nike", "quote": "Nike Black Friday sale is coming", "start": None,
                         "discount": None}),
                 (cnbc, {"brand_id": "nike", "quote": "Nike Black Friday sale runs Nov. 20 to Nov. 30", "start": "2026-11-20",
                         "end": "2026-11-30", "discount": None})],
                cfg.brands, cfg.trusted_domains, dropped)
    assert [o["status"] for o in out] == ["rumored", "confirmed"]
    src = out[1]["source"]
    assert (src["level"], src["status"], src["start"], src["end"]) == ("media", "confirmed", "2026-11-20", "2026-11-30")
    assert out[0]["source"]["status"] == "rumored"


def test_quote_found_in_excerpt(make_item):
    it = make_item(title="Nike Black Friday sale", summary="",
                   excerpt="Before.\nNike’s Black Friday sale runs Nov. 20 – Nov. 30 online.\n...\nAfter.")
    assert quote_found("Nike's Black Friday sale runs Nov. 20 - Nov. 30 online.", it)
    assert not quote_found("runs Nov. 20 - Nov. 30 in stores", it)
    assert not quote_found("Nike's Black Friday sale runs Nov. 20", make_item(title="x", summary="y"))
    from dealradar.status import quote_location
    assert quote_location("runs Nov. 20 - Nov. 30 online", it) == "excerpt"
    assert quote_location("Nike Black Friday sale", it) == "title"


# ---------- S3 v3：字段逐个核对引句、CA 要有证据 ----------
from dealradar.status import ca_evidence, date_in_quote, discount_in_quote  # noqa: E402


def test_date_must_appear_in_quote():
    yes = [("2026-11-20", "Sale starts Nov. 20 online"), ("2026-11-20", "from November 20th"),
           ("2026-11-20", "starting 20 November"), ("2026-11-20", "doors open 11/20 at 6am"),
           ("2026-11-20", "runs 2026-11-20 to 2026-11-30"), ("2026-11-27", "deals run Nov. 20-27"),
           ("2026-11-30", "Nov 27 through 30"), ("2026-11-27", "Nike Black Friday sale"),
           ("2026-11-30", "Cyber Monday deals"), ("2026-12-26", "Boxing Day blowout"),
           ("2026-11-26", "open on Thanksgiving"), ("2026-12-01", "Cyber Week runs Nov. 30 - Dec. 1")]
    for d, q in yes:
        assert date_in_quote(d, q), (d, q)
    no = [("2026-11-20", "Nike Black Friday sale"), ("2026-11-20", "starts Nov. 2"), ("2026-11-20", "Nov. 201"),
          ("2026-11-20", "11/2"), ("2026-11-28", "Black Friday weekend"), ("2026-11-20", "this Friday"),
          ("2026-11-20", "2026/11/20 ref 111/20"), ("bad", "Nov. 20")]
    for d, q in no:
        assert not date_in_quote(d, q), (d, q)


def test_discount_numbers_must_appear_in_quote():
    assert discount_in_quote("up to 50% off", "save up to 50 percent")
    assert discount_in_quote("$1,400 off", "taking $1400 off the TV")
    assert discount_in_quote("BOGO", "anything")                      # 没有数字：不核对
    assert not discount_in_quote("up to 60% off", "up to 50% off")
    assert not discount_in_quote("extra 25% off $100+", "extra 25% off")


def test_judge_nulls_unsupported_fields_then_sets_status(cfg, make_item):
    it = make_item(title="Nike Black Friday sale starts Nov. 20 with up to 50% off", source_url="https://www.cnbc.com")
    ev = {"brand_id": "nike", "region": "US", "quote": "Nike Black Friday sale starts Nov. 20",
          "start": "2026-11-20", "end": "2026-11-30", "discount": "up to 50% off"}
    dropped = {}
    out = judge([(it, ev)], cfg.brands, cfg.trusted_domains, dropped)
    o = out[0]
    assert (o["start"], o["end"], o["discount"]) == ("2026-11-20", None, None)
    assert o["status"] == "confirmed"          # media + 有开始日期
    assert dropped == {"置空：end 不在引句里": 1, "置空：折扣数字不在引句里": 1}
    ev2 = dict(ev, quote="Nike Black Friday sale", start="2026-11-20", discount="up to 50% off")
    o2 = judge([(it, ev2)], cfg.brands, cfg.trusted_domains, {})[0]
    assert o2["start"] is None and o2["discount"] is None and o2["status"] == "rumored"   # 全被置空 → 传闻


def test_ca_needs_canadian_evidence(cfg, make_item):
    media = ["retail-insider.com", "redflagdeals.com"]
    canada_names = ["Canada Goose", "Canadian Tire"]
    nike = cfg.brands["nike"]
    assert ca_evidence(make_item(source_url="https://www.bestbuy.ca"), nike, media)
    assert ca_evidence(make_item(source="bing", link="https://www.cbc.ca/news/x", source_url="https://www.cbc.ca"),
                       nike, media)
    assert ca_evidence(make_item(source_url="https://retail-insider.com"), nike, media)
    assert ca_evidence(make_item(source="rfd", source_url=None, link="https://forums.redflagdeals.com/x"), nike, media)
    assert ca_evidence(make_item(title="Nike Canada Black Friday"), nike, media)
    assert ca_evidence(make_item(summary="Prices from C$89"), nike, media)
    assert ca_evidence(make_item(excerpt="Canadian shoppers can save 30%."), nike, media)
    assert ca_evidence(make_item(), cfg.brands["canadiantire"], media)       # 只在加拿大有店的品牌
    assert not ca_evidence(make_item(), nike, media)                          # cnbc.com、文中没提加拿大
    assert not ca_evidence(make_item(title="Canada Goose Black Friday sale"), cfg.brands["canadagoose"], media,
                           canada_names)                                       # 品牌名里的 Canada 不算
    # judge 里没有证据的 CA 事件丢弃并计数
    dropped = {}
    out = judge([(make_item(title="Nike sale starts Nov. 20"),
                  {"brand_id": "nike", "region": "CA", "quote": "Nike sale starts Nov. 20", "start": "2026-11-20"})],
                cfg.brands, cfg.trusted_domains, dropped, media)
    assert out == [] and dropped == {"判成 CA 但没有加拿大证据": 1}


# ---------- S3 v5：early_bf 的两种写法、引句出自标题时连标题一起看、去复数 ----------
from dealradar.status import finalize_candidates, sale_key, type_supported  # noqa: E402


def test_early_bf_extra_phrasings():
    assert type_supported("early_bf", "Black Friday came early at Amazon", None)
    assert type_supported("early_bf", "Don't wait for Black Friday: Amazon deals now", None)
    assert type_supported("early_bf", "Don’t wait until Black Friday", None)
    assert not type_supported("early_bf", "Black Friday deals at Amazon", None)


def test_type_check_uses_title_when_quote_comes_from_title(cfg, make_candidate):
    title = "[Canadian Tire] Black Friday flyer leaked: up to 60% off, Nov 20 - Nov 27"
    c = make_candidate(brand_id="canadiantire", region="CA", quote="up to 60% off, Nov 20 - Nov 27",
                       start="2026-11-20", end="2026-11-27")
    c["source"].update(title=title, quote_in="title")
    out = finalize_candidates([c], cfg.brands, {})
    assert out[0]["event_type"] == "black_friday"
    c2 = make_candidate(brand_id="canadiantire", region="CA", quote="up to 60% off, Nov 20 - Nov 27",
                        start="2026-11-20")
    c2["source"].update(title=title, quote_in="excerpt")          # 引句出自正文：不看标题
    assert finalize_candidates([c2], cfg.brands, {})[0]["event_type"] == "other"


def test_sale_key_singularizes():
    assert sale_key("DoorBOOsters", set(), 2026) == sale_key("Door BOOster", set(), 2026) == "doorbooster"
    assert sale_key("Holiday Savings", set(), 2026) == "holidaysaving"
    assert sale_key("Bonus Days", set(), 2026) == "bonusday"            # bonus 以 us 结尾，不动
    assert sale_key("Glass Class", set(), 2026) == "glassclass"         # ss 结尾不动
    assert sale_key("Iris Week", set(), 2026) == "irisweek"             # is 结尾不动
    assert sale_key("Gas Days", set(), 2026) == "gasday"                # 长度不超过 3 的不动
