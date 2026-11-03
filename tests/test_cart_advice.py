"""购物车：建议规则、提醒去重、汇总正文（纯函数）。"""
from __future__ import annotations

from datetime import date

from dealradar.cart import advice

TODAY = date(2026, 11, 20)


def it(**kw):
    x = {"id": "c1", "name": "Dyson V15", "brand_id": "dyson", "region": "CA", "target_price": 700,
         "deadline": "2026-12-01"}
    x.update(kw)
    return x


def ev(id_, start, status="confirmed", brand="dyson", region="CA", end=None, **kw):
    return dict({"id": id_, "brand_id": brand, "region": region, "event_type": "black_friday", "start": start,
                 "end": end, "status": status, "discount": None, "_label": f"{brand} 黑五", "sources": []}, **kw)


def price(p, ok=True, name="Best Buy"):
    return {"price": p, "currency": "CAD" if ok else "USD", "comparable": ok, "retailer_name": name, "url": "https://x"}


def test_buy_now_when_price_at_or_below_target():
    r = advice.recommend(it(), [price(749.99), price(700)], [ev("e1", "2026-11-27")], TODAY)
    assert r["action"] == "buy_now" and "C$ 700" in r["reason"]


def test_currency_mismatch_not_compared():
    r = advice.recommend(it(), [price(500, ok=False)], [], TODAY)
    assert r["action"] == "watch"


def test_wait_until_confirmed_event_before_deadline():
    evs = [ev("late", "2026-12-05"), ev("e2", "2026-11-28"), ev("e1", "2026-11-27", discount="up to $200 off")]
    r = advice.recommend(it(), [price(749.99)], evs, TODAY)
    assert r == {"action": "wait_until", "date": "2026-11-27",
                 "reason": "dyson 黑五（已官宣）11/27 开始，在最晚购买日 12/1 之前；up to $200 off"}


def test_event_after_deadline_or_rumor_only_is_watch():
    r = advice.recommend(it(), [], [ev("late", "2026-12-05"), ev("r", None, status="rumored")], TODAY)
    assert r["action"] == "watch" and r["reason"].startswith("只有传闻")
    assert advice.recommend(it(), [], [], TODAY)["reason"] == "暂时没有相关活动，也还没读到价格"


def test_deadline_rules():
    r = advice.recommend(it(deadline="2026-11-23"), [price(800)], [ev("r", None, status="rumored")], TODAY)
    assert r["action"] == "deadline" and "还有 3 天" in r["reason"] and "C$ 800" in r["reason"]
    assert advice.recommend(it(deadline="2026-11-20"), [], [], TODAY)["reason"].startswith("今天是最晚购买日")
    assert advice.recommend(it(deadline="2026-11-24"), [], [], TODAY)["action"] == "watch"
    assert advice.recommend(it(deadline="2026-11-19"), [], [], TODAY)["reason"].startswith("已经过了")
    # 截止前 3 天内、但有已官宣活动在截止前开始：等活动
    assert advice.recommend(it(deadline="2026-11-22"), [], [ev("e", "2026-11-21")], TODAY)["action"] == "wait_until"


def test_active_and_ended_events():
    r = advice.recommend(it(), [price(800)], [ev("a", "2026-11-18", end="2026-11-25")], TODAY)
    assert r["action"] == "watch" and "正在进行" in r["reason"]
    assert advice.recommend(it(), [], [ev("old", "2026-11-01", end="2026-11-05")], TODAY)["reason"].startswith("暂时")


def test_no_target_never_buy_now():
    assert advice.recommend(it(target_price=None), [price(1)], [], TODAY)["action"] == "watch"


def test_relevant_events_include_retailer_of_selected_pages():
    pages = [{"retailer": "bestbuy", "selected": True}, {"retailer": "costco", "selected": False}]
    ids = advice.item_brand_ids(it(), {"brand_id": "dyson"}, pages)
    assert ids == {"dyson", "bestbuy"}
    evs = [ev("a", "2026-11-27", brand="bestbuy"), ev("b", "2026-11-27", brand="costco"),
           ev("c", "2026-11-27", brand="bestbuy", region="US")]
    assert [e["id"] for e in advice.relevant_events(ids, "CA", evs)] == ["a"]


# ---------- 提醒 ----------
def test_reminders_three_kinds_and_keys():
    entry = {"prices": [price(689.5)]}
    evs = [ev("bb-ca-bf", "2026-11-21", brand="bestbuy"), ev("x", "2026-11-22")]
    rs = advice.reminders_for(it(deadline="2026-11-23"), entry, evs, TODAY, {})
    assert [r["key"] for r in rs] == ["event:c1:bb-ca-bf:2026-11-21", "price:c1:689.50", "deadline:c1:2026-11-23"]
    assert rs[1]["text"] == "Best Buy 现价 C$ 689.50，不高于心理价 C$ 700"


def test_reminders_dedupe_price_only_when_lower():
    entry = {"prices": [price(689.5)]}
    sent = {"keys": {"price:c1:689.50": "t"}}
    assert advice.reminders_for(it(), entry, [], TODAY, sent) == []
    sent = {"keys": {"price:c1:650.00": "t"}}
    assert advice.reminders_for(it(), entry, [], TODAY, sent) == []            # 比提醒过的贵：不再提醒
    lower = advice.reminders_for(it(), {"prices": [price(640)]}, [], TODAY, sent)
    assert [r["key"] for r in lower] == ["price:c1:640.00"]


def test_reminders_dedupe_event_and_deadline():
    sent = {"keys": {"event:c1:e:2026-11-21": "t", "deadline:c1:2026-11-22": "t"}}
    assert advice.reminders_for(it(deadline="2026-11-22"), {}, [ev("e", "2026-11-21")], TODAY, sent) == []
    # 截止日之后才开始的活动不提醒
    assert advice.reminders_for(it(deadline="2026-11-20"), {}, [ev("e", "2026-11-21")], TODAY, {})[0]["kind"] == \
        "deadline"


# ---------- 汇总 ----------
def test_digest_none_without_reminders():
    assert advice.build_digest([], [it()], {}, TODAY) is None


def test_digest_chinese_grouped_by_item():
    items = [it(), it(id="c2", name="AirPods Pro 2", region="US", target_price=199, deadline="2026-11-30")]
    entries = {"c1": {"identified": {"brand": "Dyson", "model": "V15 Detect", "specs": "Absolute", "confirmed": False},
                      "recommendation": {"action": "buy_now", "reason": "Best Buy 现价 C$ 689.50"},
                      "pages": [{"selected": True, "status": "blocked"}]}}
    rs = advice.reminders_for(items[0], {"prices": [price(689.5)]}, [], TODAY, {})
    d = advice.build_digest(rs, items, entries, TODAY)
    assert d["subject"] == "黑五购物提醒 · 11 月 20 日 · 1 件商品有动静"
    assert "1. Dyson V15（加拿大 · 心理价 C$ 700 · 最晚 12/1 买）" in d["body"]
    assert "型号：Dyson V15 Detect（Absolute）  ← AI 识别，未确认" in d["body"]
    assert "建议：现在买 —— Best Buy 现价 C$ 689.50" in d["body"]
    assert "另有 1 个勾选的商品页读不到价格" in d["body"] and "AirPods" not in d["body"]
