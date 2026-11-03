"""购物车：型号确认（DeepSeek，用假的 client，不联网）。"""
from __future__ import annotations

import pytest

from dealradar.cart import identify
from dealradar.cart.conf import load_brands


@pytest.fixture(scope="module")
def brands():
    return load_brands()


class FakeAI:
    def __init__(self, reply=None, exc=None):
        self.reply, self.exc, self.calls = reply, exc, []

    def chat_json(self, messages):
        self.calls.append(messages)
        if self.exc:
            raise self.exc
        return self.reply


ITEM = {"id": "c1", "name": "AirPods Pro", "brand_id": None, "brand_text": "苹果", "model": "", "region": "US",
        "note": "第二代"}


def test_ambiguous_gives_up_to_four_candidates(brands):
    ai = FakeAI({"brand": "Apple", "model": "AirPods Pro 2", "specs": "USB-C", "confidence": 0.7, "ambiguous": True,
                 "candidates": [{"brand": "Apple", "model": f"AirPods Pro {i}", "specs": "x" * 400} for i in range(6)]})
    r = identify.resolve(ITEM, None, brands, ai, "2026-11-02")
    assert r["model"] == "AirPods Pro 2" and r["brand_id"] == "apple" and r["source"] == "ai"
    assert len(r["candidates"]) == 4 and all(len(c["specs"]) == 300 for c in r["candidates"])
    assert r["confirmed"] is False and 0 <= r["confidence"] <= 1
    assert "AirPods Pro" in ai.calls[0][1]["content"] and "苹果" in ai.calls[0][1]["content"]


def test_cached_until_description_changes(brands):
    ai = FakeAI({"brand": "Apple", "model": "AirPods Pro 2", "specs": "", "confidence": 0.9, "ambiguous": False,
                 "candidates": [{"brand": "Apple", "model": "AirPods Pro 2"}]})
    r1 = identify.resolve(ITEM, None, brands, ai, "2026-11-02")
    r2 = identify.resolve(ITEM, r1, brands, ai, "2026-11-03")
    assert len(ai.calls) == 1 and r2["model"] == "AirPods Pro 2" and r2["candidates"] == []
    identify.resolve(dict(ITEM, note="第三代"), r2, brands, ai, "2026-11-03")
    assert len(ai.calls) == 2


def test_confirmed_model_wins(brands):
    ai = FakeAI({"brand": "Apple", "model": "AirPods Pro 2", "confidence": 0.5, "ambiguous": True,
                 "candidates": [{"brand": "Apple", "model": "AirPods Pro 2"}, {"brand": "Apple", "model": "AirPods Pro 3"}]})
    it = dict(ITEM, confirmed_model={"brand": "Apple", "model": "AirPods Pro 3", "specs": "MagSafe"})
    r = identify.resolve(it, None, brands, ai, "2026-11-02")
    assert r["confirmed"] is True and r["model"] == "AirPods Pro 3" and r["specs"] == "MagSafe"
    assert r["brand_id"] == "apple" and r["confidence"] == 1.0 and len(r["candidates"]) == 2
    bad = dict(ITEM, confirmed_model={"model": ""})
    assert identify.resolve(bad, None, brands, ai, "2026-11-02")["confirmed"] is False


def test_ai_error_falls_back_and_retries_next_day(brands):
    ai = FakeAI(exc=RuntimeError("boom"))
    it = dict(ITEM, model="MTJV3AM/A", brand_id="apple")
    r1 = identify.resolve(it, None, brands, ai, "2026-11-02")
    assert r1["source"] == "input" and r1["model"] == "MTJV3AM/A" and "AI 出错" in r1["error"]
    r2 = identify.resolve(it, r1, brands, ai, "2026-11-02")
    assert len(ai.calls) == 1 and r2["error"]                          # 当天不再重试
    ai.exc = None
    ai.reply = {"brand": "Apple", "model": "AirPods Pro 2", "confidence": 0.9}
    r3 = identify.resolve(it, r2, brands, ai, "2026-11-03")
    assert len(ai.calls) == 2 and r3["source"] == "ai"


def test_no_client_uses_user_text_and_url_is_not_a_model(brands):
    r = identify.resolve(dict(ITEM, model="https://www.apple.com/shop/product/x"), None, brands, None, "2026-11-02")
    assert r["model"] == "" and r["source"] == "input" and "DEEPSEEK" in r["error"]


def test_brand_lookup(brands):
    assert identify.brand_lookup("Best Buy", brands) == "bestbuy"
    assert identify.brand_lookup("arcteryx", brands) == "arcteryx"
    assert identify.brand_lookup("Nobody", brands) is None
