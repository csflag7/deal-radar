"""购物车：一次完整运行（临时目录、假 AI、假读取器；不联网、不发邮件）。"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

import dealradar.cart.runner as runner
from dealradar.cart import mail, run
from dealradar.cart.conf import Files
from dealradar.cart.prices import FetchResult

NOW = datetime(2026, 11, 26, 13, 0, tzinfo=timezone.utc)          # 多伦多 11-26 早上 8 点


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("测试里不能真的去读网页 / 调 AI")
    monkeypatch.setattr(runner, "PoliteFetcher", boom)
    monkeypatch.setattr(runner, "make_llm", boom)


def ld(price, cur):
    return (f'<script type="application/ld+json">{{"@type":"Product","offers":{{"@type":"Offer","price":"{price}",'
            f'"priceCurrency":"{cur}","availability":"https://schema.org/InStock"}}}}</script>')


class FakeFetcher:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def fetch(self, url):
        self.calls.append(url)
        v = self.pages.get(url)
        if v is None:
            return FetchResult(url, "blocked", "HTTP 403")
        return FetchResult(url, "ok", html=v, final_url=url)


class FakeAI:
    def __init__(self):
        self.calls = 0

    def chat_json(self, messages):
        self.calls += 1
        if "AirPods" in messages[1]["content"]:
            return {"brand": "Apple", "model": "AirPods Pro 2", "specs": "USB-C", "confidence": 0.6, "ambiguous": True,
                    "candidates": [{"brand": "Apple", "model": "AirPods Pro 2", "specs": "USB-C"},
                                   {"brand": "Apple", "model": "AirPods Pro 3", "specs": ""}]}
        return {"brand": "Dyson", "model": "V15 Detect", "specs": "Absolute", "confidence": 0.9, "ambiguous": False,
                "candidates": [{"brand": "Dyson", "model": "V15 Detect", "specs": "Absolute"}]}


BB = "https://www.bestbuy.ca/en-ca/product/dyson-v15/17183862"
AMZ = "https://www.amazon.com/dp/B0CHWRXH8B"
BBUS = "https://www.bestbuy.com/site/airpods/6447382.p?skuId=6447382"
CART = {"items": [
    {"id": "dy1", "name": "Dyson V15 Detect", "brand_id": "dyson", "brand_text": "Dyson", "model": BB, "region": "CA",
     "target_price": 700, "deadline": "2026-12-01", "note": "", "created": "t", "updated": "t"},
    {"id": "ap1", "name": "AirPods Pro 2", "brand_id": "apple", "brand_text": "Apple", "model": "", "region": "US",
     "target_price": 199, "deadline": "2026-11-29", "note": "", "created": "t", "updated": "t",
     "pages": [{"url": AMZ, "selected": True}, {"url": BBUS, "selected": True},
               {"url": "https://www.apple.com/shop/x", "selected": False}],
     "future_field": {"keep": 1}},
]}
EVENTS = [
    {"id": "bestbuy-CA-black_friday-2026", "brand_id": "bestbuy", "region": "CA", "event_type": "black_friday",
     "event_name": "Best Buy Black Friday", "start": "2026-11-27", "end": "2026-12-01", "discount": None,
     "status": "confirmed", "sources": [{"url": "https://www.bestbuy.ca/en-ca/event/black-friday"}]},
    {"id": "apple-US-black_friday-2026", "brand_id": "apple", "region": "US", "event_type": "black_friday",
     "start": None, "end": None, "status": "rumored", "sources": []},
]


@pytest.fixture
def setup(tmp_path):
    (tmp_path / "cart.json").write_text(json.dumps(CART, ensure_ascii=False), encoding="utf-8")
    return tmp_path


def go(d, fetcher, ai, events=EVENTS, now=NOW):
    return run(events, now, {"timezone": "America/Toronto"}, data_dir=d, llm=ai, fetcher=fetcher)


def test_full_run_state_prices_reminders_digest(setup):
    before = (setup / "cart.json").read_bytes()
    f = FakeFetcher({BB: ld("749.99", "CAD"), AMZ: ld("189.00", "USD")})
    ai = FakeAI()
    res = go(setup, f, ai)
    assert (setup / "cart.json").read_bytes() == before                  # cart.json 只读
    assert sorted(f.calls) == sorted([BB, AMZ, BBUS]) and ai.calls == 2  # 只读勾选的页面
    st = json.loads((setup / "cart_state.json").read_text(encoding="utf-8"))
    dy, ap = st["items"]
    assert set(dy) >= {"item_id", "identified", "pages", "search_links", "prices", "recommendation", "updated"}
    assert dy["identified"]["model"] == "V15 Detect" and dy["identified"]["confirmed"] is False
    assert dy["pages"][0]["retailer"] == "bestbuy" and dy["pages"][0]["status"] == "ok"
    assert dy["prices"][0]["price"] == 749.99 and dy["prices"][0]["comparable"] is True
    # Dyson：Best Buy CA 黑五（已官宣）11-27 开始，在截止日前 → 等活动；明天开始 → 提醒
    assert dy["recommendation"]["action"] == "wait_until" and dy["recommendation"]["date"] == "2026-11-27"
    assert any(x["url"].startswith("https://www.bestbuy.ca/en-ca/search?search=") for x in dy["search_links"])
    # AirPods：Amazon 189 ≤ 199 → 现在买；Best Buy US 被拒 → blocked；Apple 页面没勾 → 不读
    assert ap["recommendation"]["action"] == "buy_now"
    assert {p["url"]: p["status"] for p in ap["pages"]} == {AMZ: "ok", BBUS: "blocked",
                                                            "https://www.apple.com/shop/x": "unselected"}
    assert len(ap["identified"]["candidates"]) == 2
    keys = [r["key"] for r in res["reminders"]]
    assert keys == ["event:dy1:bestbuy-CA-black_friday-2026:2026-11-27", "price:ap1:189.00", "deadline:ap1:2026-11-29"]
    d = res["digest"]
    assert d["subject"].startswith("黑五购物提醒 · 11 月 26 日") and "Dyson V15 Detect（加拿大" in d["body"]
    prices = json.loads((setup / "prices.json").read_text(encoding="utf-8"))
    assert prices["pages"][BBUS]["status"] == "blocked" and prices["pages"][AMZ]["history"][-1]["price"] == 189.0
    pending = json.loads((setup / "cart_pending.json").read_text(encoding="utf-8"))
    assert pending["keys"] == keys and pending["hash"] == mail.body_hash(d["subject"], d["body"])
    assert not (setup / "notified.json").exists()


def test_second_run_same_day_does_not_refetch_or_reask(setup):
    f, ai = FakeFetcher({BB: ld("749.99", "CAD"), AMZ: ld("189.00", "USD")}), FakeAI()
    go(setup, f, ai)
    f2, ai2 = FakeFetcher({}), FakeAI()
    res = go(setup, f2, ai2, now=NOW.replace(hour=16))
    assert f2.calls == [] and ai2.calls == 0                            # 每页每天一次；描述没变不问 AI
    assert len(res["reminders"]) == 3                                   # 预览模式没写去重，提醒还在
    nxt = go(setup, FakeFetcher({BB: ld("689.00", "CAD"), AMZ: ld("185.00", "USD")}), FakeAI(),
             now=NOW.replace(day=27))
    st = json.loads((setup / "cart_state.json").read_text(encoding="utf-8"))
    dy = st["items"][0]
    assert dy["prices"][0]["previous"] == 749.99 and dy["prices"][0]["change"] == -60.99
    assert dy["recommendation"]["action"] == "buy_now"
    assert "price:dy1:689.00" in [r["key"] for r in nxt["reminders"]]


def test_after_send_dedupe_and_one_digest_per_day(setup, tmp_path):
    res = go(setup, FakeFetcher({BB: ld("749.99", "CAD"), AMZ: ld("189.00", "USD")}), FakeAI())
    f = Files(setup)
    mail.mark_sent(f, res["digest"]["subject"], res["digest"]["body"], NOW, "2026-11-26")   # 相当于发送成功
    again = go(setup, FakeFetcher({}), FakeAI(), now=NOW.replace(hour=20))
    assert again["reminders"] == [] and again["digest"] is None
    assert not f.pending.exists()
    nxt = go(setup, FakeFetcher({AMZ: ld("179.00", "USD"), BB: ld("749.99", "CAD")}), FakeAI(),
             now=NOW.replace(day=27))
    assert [r["key"] for r in nxt["reminders"]] == ["price:ap1:179.00"]          # 截止提醒发过了，不再发
    assert nxt["digest"] is not None


def test_currency_mismatch_flagged(setup):
    f = FakeFetcher({BB: ld("520.00", "USD"), AMZ: ld("189.00", "USD")})
    go(setup, f, FakeAI())
    dy = json.loads((setup / "cart_state.json").read_text(encoding="utf-8"))["items"][0]
    assert dy["prices"][0]["comparable"] is False and "对不上" in dy["prices"][0]["currency_note"]
    assert dy["pages"][0]["currency_mismatch"] is True and dy["recommendation"]["action"] != "buy_now"


def test_confirmed_model_from_cart(setup):
    cart = json.loads((setup / "cart.json").read_text(encoding="utf-8"))
    cart["items"][1]["confirmed_model"] = {"brand": "Apple", "model": "AirPods Pro 3", "specs": ""}
    (setup / "cart.json").write_text(json.dumps(cart), encoding="utf-8")
    go(setup, FakeFetcher({}), FakeAI())
    ap = json.loads((setup / "cart_state.json").read_text(encoding="utf-8"))["items"][1]
    assert ap["identified"]["confirmed"] is True and ap["identified"]["model"] == "AirPods Pro 3"
    assert ap["search_query"] == "Apple AirPods Pro 3"


def test_broken_cart_writes_nothing(tmp_path):
    (tmp_path / "cart.json").write_text('{"items": [', encoding="utf-8")
    res = go(tmp_path, FakeFetcher({}), FakeAI())
    assert res["digest"] is None and res["reminders"] == [] and "cart.json" in res["error"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["cart.json"]


def test_missing_cart_and_no_ai_key(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "make_llm", lambda: None)
    res = run([], NOW, {}, data_dir=tmp_path, fetcher=FakeFetcher({}))
    assert res == {"reminders": [], "digest": None}
    (tmp_path / "cart.json").write_text(json.dumps({"items": [CART["items"][0]]}), encoding="utf-8")
    run([], NOW, {}, data_dir=tmp_path, fetcher=FakeFetcher({BB: ld("749.99", "CAD")}))
    st = json.loads((tmp_path / "cart_state.json").read_text(encoding="utf-8"))
    assert st["items"][0]["identified"]["source"] == "input" and "DEEPSEEK" in st["items"][0]["identified"]["error"]
