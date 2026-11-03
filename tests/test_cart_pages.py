"""购物车：商品页清单、零售商对应、站内搜索链接。"""
from __future__ import annotations

from urllib.parse import urlparse

import pytest

from dealradar.cart import pages as pg
from dealradar.cart.conf import load_brands, load_cart_config


@pytest.fixture(scope="module")
def cc():
    return load_cart_config()


@pytest.fixture(scope="module")
def brands():
    return load_brands()


def item(**kw):
    it = {"id": "c1", "name": "吸尘器", "brand_id": "dyson", "brand_text": "Dyson", "model": "V15 Detect",
          "region": "CA", "target_price": 700, "deadline": "2026-12-01", "note": ""}
    it.update(kw)
    return it


def test_model_url_is_selected_user_page(cc, brands):
    it = item(model="https://www.bestbuy.ca/en-ca/product/dyson-v15/17183862#reviews")
    ps = pg.build_pages(it, cc, brands)
    assert ps == [{"url": "https://www.bestbuy.ca/en-ca/product/dyson-v15/17183862", "retailer": "bestbuy",
                   "retailer_name": "Best Buy", "region": "CA", "selected": True, "source": "user"}]


def test_pages_field_adds_and_unselects(cc, brands):
    u1 = "https://www.costco.ca/dyson.product.1.html"
    it = item(model="https://www.bestbuy.ca/en-ca/product/x/1",
              pages=[{"url": "https://www.bestbuy.ca/en-ca/product/x/1", "selected": False},
                     {"url": u1, "selected": True}, {"url": "javascript:alert(1)", "selected": True},
                     {"url": "https://www.dysoncanada.ca/en/v15", "selected": False}, "garbage"])
    ps = pg.build_pages(it, cc, brands)
    assert [(p["url"], p["selected"], p["retailer"]) for p in ps] == [
        ("https://www.bestbuy.ca/en-ca/product/x/1", False, "bestbuy"), (u1, True, "costco"),
        ("https://www.dysoncanada.ca/en/v15", False, "dyson")]


def test_suggested_pages_default_unselected(cc, brands):
    ps = pg.build_pages(item(), cc, brands, suggested=["https://www.dyson.com/v15", "ftp://x"])
    assert ps == [{"url": "https://www.dyson.com/v15", "retailer": "dyson", "retailer_name": "Dyson",
                   "region": "US", "selected": False, "source": "suggested"}]


@pytest.mark.parametrize("url,rid,region", [
    ("https://www.bestbuy.ca/en-ca/product/a/1", "bestbuy", "CA"),
    ("https://www.bestbuy.com/site/a/6505727.p?skuId=6505727", "bestbuy", "US"),
    ("https://www.amazon.ca/dp/B0", "amazon", "CA"),
    ("https://www.apple.com/ca/shop/product/x", "apple", "CA"),
    ("https://shop.lululemon.com/en-ca/p/align/1", "lululemon", "CA"),
    ("https://www.canadiantire.ca/en/pdp/x.html", "canadiantire", "CA"),
    ("https://example.org/p/1", None, "US"),
])
def test_retailer_and_region(cc, brands, url, rid, region):
    assert pg.retailer_of(url, cc, brands)[0] == rid
    assert pg.url_region(url) == region


def test_search_links_by_region(cc, brands):
    it = item()
    q = pg.search_query(it, {"brand": "Dyson", "model": "V15 Detect Absolute"}, brands)
    assert q == "Dyson V15 Detect Absolute"
    links = pg.search_links(it, q, cc, brands)
    hosts = [urlparse(x["url"]).hostname for x in links]
    assert links[0]["retailer"] == "dyson" and hosts[0] == "www.dysoncanada.ca"
    assert "https://www.bestbuy.ca/en-ca/search?search=Dyson+V15+Detect+Absolute" in [x["url"] for x in links]
    assert "www.canadiantire.ca" in hosts and "www.target.com" not in hosts
    assert all(h.endswith(".ca") for h in hosts)
    us = pg.search_links(item(region="US"), q, cc, brands)
    us_hosts = [urlparse(x["url"]).hostname for x in us]
    assert "www.target.com" in us_hosts and "www.canadiantire.ca" not in us_hosts
    assert all(x["url"].startswith("https://") for x in links + us)


def test_search_query_fallbacks_and_brand_without_template(cc, brands):
    it = item(brand_id="bose", brand_text="Bose", model="https://www.bose.ca/x")
    assert pg.search_query(it, {}, brands) == "Bose 吸尘器"
    links = pg.search_links(it, "Bose QC Ultra", cc, brands)
    assert links[0]["name"].endswith("官网（首页）") and links[0]["url"] == "https://www.bose.ca"
    assert pg.search_links(item(region="UK"), "x", cc, brands) == []


def test_retailer_brand_not_duplicated(cc, brands):
    links = pg.search_links(item(brand_id="bestbuy"), "TV", cc, brands)
    assert [x["retailer"] for x in links].count("bestbuy") == 1
