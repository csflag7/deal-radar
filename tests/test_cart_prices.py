"""购物车：读价格（解析 JSON-LD / og:price、币种核对、按规矩读取）。不联网：全部用 httpx.MockTransport。"""
from __future__ import annotations

import httpx

from dealradar.cart.prices import (PoliteFetcher, currency_ok, extract_price, parse_price, read_page,
                                   same_page, to_price)
from dealradar.official import UA, RobotsGate, SiteClock


def ld(obj: str) -> str:
    return f'<html><head><script type="application/ld+json">{obj}</script></head><body>x</body></html>'


# ---------- 解析 ----------
def test_offer_in_graph_prefers_in_stock_lowest():
    html = ld('{"@context":"https://schema.org","@graph":[{"@type":"BreadcrumbList"},{"@type":"Product","name":"V15",'
              '"offers":[{"@type":"Offer","price":"749.99","priceCurrency":"CAD","availability":"https://schema.org/InStock"},'
              '{"@type":"Offer","price":"699.99","priceCurrency":"CAD","availability":"https://schema.org/OutOfStock"}]}]}')
    p = parse_price(html)
    assert p == {"price": 749.99, "currency": "CAD", "availability": "InStock", "via": "jsonld", "offers": 2}


def test_aggregate_offer_and_price_specification():
    p = parse_price(ld('{"@type":"Product","offers":{"@type":"AggregateOffer","lowPrice":"1,199.00",'
                       '"highPrice":"1,399","priceCurrency":"USD"}}'))
    assert p["price"] == 1199.0 and p["currency"] == "USD" and p["via"] == "jsonld"
    p = parse_price(ld('[{"@type":"WebPage"},{"@type":["Product"],"offers":{"@type":"Offer",'
                       '"priceSpecification":{"price":249,"priceCurrency":"usd"},"availability":"InStock"}}]'))
    assert p["price"] == 249.0 and p["currency"] == "USD" and p["availability"] == "InStock"


def test_product_group_variants_and_comment_wrapped_json():
    html = ld('<!-- {"@type":"ProductGroup","hasVariant":[{"@type":"Product","offers":{"@type":"Offer","price":"98",'
              '"priceCurrency":"CAD"}}]} -->')
    p = parse_price(html)
    assert p["price"] == 98.0 and p["currency"] == "CAD"


def test_meta_fallback_and_bad_jsonld():
    html = ('<script type="application/ld+json">{not json</script>'
            '<meta property="og:price:amount" content="49.00"><meta property="og:price:currency" content="usd">'
            '<meta property="product:availability" content="in stock">')
    p = parse_price(html)
    assert p == {"price": 49.0, "currency": "USD", "availability": "instock", "via": "meta", "offers": 1}
    p = parse_price('<meta property="product:price:amount" content="12.5"><meta property="product:price:currency" '
                    'content="CAD">')
    assert p["price"] == 12.5 and p["currency"] == "CAD"


def test_no_price_and_garbage_currency():
    assert parse_price("<html><body>Black Friday</body></html>") is None
    assert parse_price(ld('{"@type":"Product","name":"x"}')) is None
    p = parse_price(ld('{"@type":"Product","offers":{"price":"10","priceCurrency":"<script>"}}'))
    assert p["price"] == 10.0 and p["currency"] is None


def test_listing_page_and_several_products_not_trusted():
    """列表页（ItemList 里一串商品）、页面上几件不同的商品：不认价格（实测 Dyson 旧网址跳到 shop-all 列表页）。"""
    listing = ld('{"@type":"ItemList","itemListElement":[{"@type":"ListItem","item":{"@type":"Product","name":"A",'
                 '"offers":{"price":"299.99","priceCurrency":"USD"}}},{"@type":"ListItem","item":{"@type":"Product",'
                 '"name":"B","offers":{"price":"749.99","priceCurrency":"USD"}}}]}')
    assert parse_price(listing) is None
    two = ld('[{"@type":"Product","name":"A","offers":{"price":"1","priceCurrency":"USD"}},'
             '{"@type":"Product","name":"B","offers":{"price":"2","priceCurrency":"USD"}}]')
    p, why = extract_price(two)
    assert p is None and "2 件不同的商品" in why
    same = ld('{"@type":"Product","name":"A","offers":{"price":"5","priceCurrency":"USD"}}') + \
        ld('{"@type":"Product","name":"a","sku":"9","offers":{"price":"4","priceCurrency":"USD"}}')
    assert parse_price(same)["price"] == 4.0
    main = ld('{"@type":"WebPage","mainEntity":{"@type":"Product","name":"M","offers":{"price":"8","priceCurrency":"CAD"}}}')
    assert parse_price(main)["price"] == 8.0


def test_same_page_after_redirect():
    assert same_page("https://www.costco.ca/apple-airpods.product.4000403964.html",
                     "https://www.costco.ca/p/-/apple-airpods/4000403964")
    assert same_page("https://www.sephora.com/product/x-P97989778", "https://www.sephora.com/product/x-P97989778?c=ca")
    assert same_page("https://a.com/p/Align-Pant/", "https://a.com/p/align-pant")
    assert not same_page("https://www.dyson.com/vacuum-cleaners/cordless/v15/detect-yellow",
                         "https://www.dyson.com/vacuum-cleaners/shop-all")
    assert not same_page("https://shop.example.com/p/1", "https://shop.example.com/")


def test_redirect_to_other_page_is_none():
    def h(req):
        if req.url.path == "/v15/detect-yellow":
            return httpx.Response(301, headers={"location": "/shop-all"})
        return httpx.Response(200, html=PRODUCT)
    f, _ = fetcher(h)
    r = read_page(f, "https://www.dyson.com/v15/detect-yellow")
    assert r["status"] == "none" and "跳到了别的页面" in r["reason"] and "price" not in r


def test_to_price():
    assert [to_price(x) for x in ["$1,299.99", "49,99", "C$ 12", "1.299,00", 0, -3, "abc", True, None, 7]] == \
        [1299.99, 49.99, 12.0, 1299.0, None, None, None, None, None, 7.0]


def test_currency_ok():
    assert currency_ok("USD", "US") and currency_ok("CAD", "CA")
    assert not currency_ok("USD", "CA") and not currency_ok("CAD", "US") and not currency_ok(None, "US")


# ---------- 读取 ----------
class Clock:
    def __init__(self):
        self.t = 0.0
        self.sleeps = []

    def mono(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


def fetcher(handler, robots=None, **kw):
    ck = Clock()
    client = httpx.Client(transport=httpx.MockTransport(handler), headers={"User-Agent": UA}, follow_redirects=False)
    f = PoliteFetcher({"site_delay_seconds": 5}, client=client, robots=robots or (lambda u: (True, "")),
                      clock=SiteClock(5.0, clock=ck.mono), sleep=ck.sleep, monotonic=ck.mono, **kw)
    return f, ck


PRODUCT = ld('{"@type":"Product","offers":{"@type":"Offer","price":"649.99","priceCurrency":"CAD",'
             '"availability":"https://schema.org/InStock"}}')


def test_ok_with_true_ua_and_site_delay():
    seen = []

    def h(req):
        seen.append((str(req.url), req.headers["user-agent"]))
        return httpx.Response(200, html=PRODUCT)
    f, ck = fetcher(h)
    r1 = read_page(f, "https://www.bestbuy.ca/en-ca/product/a/1")
    r2 = read_page(f, "https://bestbuy.ca/en-ca/product/b/2")         # 同一网站（去掉 www. 后一样）
    assert r1["status"] == "ok" and r1["price"] == 649.99 and r1["currency"] == "CAD"
    assert r2["status"] == "ok"
    assert all(ua.startswith("DealRadar/0.1") for _, ua in seen)
    assert ck.sleeps == [5.0]                                          # 第二个请求等够 5 秒


def test_robots_disallow_never_requests_page():
    hits = []
    f, _ = fetcher(lambda req: hits.append(req) or httpx.Response(200, html=PRODUCT),
                   robots=lambda u: (False, "robots.txt 不允许"))
    r = read_page(f, "https://www.amazon.com/dp/B0")
    assert r["status"] == "blocked" and "robots" in r["reason"] and hits == []


def test_403_is_blocked_no_retry_and_same_site_skipped():
    hits = []

    def h(req):
        hits.append(str(req.url))
        return httpx.Response(403, text="Access Denied")
    f, _ = fetcher(h)
    r1 = read_page(f, "https://www.walmart.ca/en/ip/a/1")
    r2 = read_page(f, "https://www.walmart.ca/en/ip/b/2")
    assert r1["status"] == "blocked" and r1["reason"] == "HTTP 403"
    assert r2["status"] == "blocked" and "已被拒" in r2["reason"]
    assert len(hits) == 1                                              # 不重试、同站后面的页面不再请求


def test_captcha_page_is_blocked():
    f, _ = fetcher(lambda req: httpx.Response(200, html="<html><title>Robot Check</title>Enter the characters you "
                                                         "see below. Sorry, we just need to make sure you're not a "
                                                         "robot. captcha</html>"))
    r = read_page(f, "https://www.amazon.com/dp/B0")
    assert r["status"] == "blocked" and "拦截页" in r["reason"]


def test_redirect_checks_robots_each_hop():
    checked = []

    def robots(u):
        checked.append(u)
        return (False, "robots.txt 不允许") if "/blocked" in u else (True, "")

    def h(req):
        if req.url.path == "/p/1":
            return httpx.Response(301, headers={"location": "/blocked/1"})
        return httpx.Response(200, html=PRODUCT)
    f, _ = fetcher(h, robots=robots)
    r = read_page(f, "https://shop.example.com/p/1")
    assert r["status"] == "blocked" and r["reason"].startswith("跳转后") and checked[-1].endswith("/blocked/1")


def test_none_when_no_structured_price_and_error_status():
    f, _ = fetcher(lambda req: httpx.Response(200, html="<html><body>" + "x" * 70000 + "</body></html>"))
    assert read_page(f, "https://www.dyson.com/v15")["status"] == "none"
    f, _ = fetcher(lambda req: httpx.Response(404, text="nope"))
    r = read_page(f, "https://www.dyson.com/v15")
    assert r["status"] == "error" and r["reason"] == "HTTP 404"


def test_timeout_is_error_without_retry():
    hits = []

    def h(req):
        hits.append(1)
        raise httpx.ReadTimeout("slow", request=req)
    f, _ = fetcher(h)
    r = read_page(f, "https://www.costco.ca/x.product.1.html")
    assert r["status"] == "error" and "超时" in r["reason"] and len(hits) == 1


def test_robots_gate_integration_reads_robots_once():
    """复用 official.RobotsGate：robots.txt 也按同站间隔，读一次缓存；Disallow 的页面不请求。"""
    hits = []

    def h(req):
        hits.append(req.url.path)
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /cart\n")
        return httpx.Response(200, html=PRODUCT)
    ck = Clock()
    clock = SiteClock(5.0, clock=ck.mono)
    refused: set = set()
    rclient = httpx.Client(transport=httpx.MockTransport(h), headers={"User-Agent": UA})
    gate = RobotsGate(clock, refused, "DealRadar", client=rclient, sleep=ck.sleep)
    client = httpx.Client(transport=httpx.MockTransport(h), headers={"User-Agent": UA})
    f = PoliteFetcher({}, client=client, robots=gate, clock=clock, sleep=ck.sleep, monotonic=ck.mono)
    assert read_page(f, "https://www.sephora.com/product/a")["status"] == "ok"
    assert read_page(f, "https://www.sephora.com/cart")["status"] == "blocked"
    assert hits == ["/robots.txt", "/product/a"]
    assert ck.sleeps == [5.0]                                          # robots.txt 和商品页之间也隔 5 秒
