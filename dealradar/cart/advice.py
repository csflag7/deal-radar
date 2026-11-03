"""建议、提醒、汇总邮件正文——全是纯函数（不读写文件、不联网），好测试。

建议（优先级从上到下）：
1. 现价 ≤ 心理价 → buy_now（现在买）
2. 截止日前有同品牌或该零售商的已官宣活动 → wait_until（等到活动开始日）
3. 已过最晚购买日，或离截止日 ≤ 3 天 → deadline（截止临近）
4. 只有传闻 → watch（观察）；什么活动都没有也是 watch
相关活动 = 同地区，且品牌是商品的品牌，或是已勾选商品页所属的零售商（bestbuy.ca → bestbuy）。

提醒（去重键，§0.1 修订）：
- event:<商品>:<事件id>:<开始日>   相关活动明天开始
- price:<商品>:<价格>              价格 ≤ 心理价；只有比提醒过的更低才再提醒
- deadline:<商品>:<截止日>          离截止日 ≤ 3 天（含当天）
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

REGION_ZH = {"US": "美国", "CA": "加拿大"}
UNIT = {"USD": "US$", "CAD": "C$"}
REGION_UNIT = {"US": "US$", "CA": "C$"}
ACTION_ZH = {"buy_now": "现在买", "wait_until": "等活动", "watch": "观察", "deadline": "截止临近"}
TYPE_ZH = {"early_bf": "黑五预热", "black_friday": "黑五", "cyber_monday": "网络星期一", "cyber_week": "网络周",
           "boxing_day": "节礼日", "other": "促销"}


def parse_day(s) -> Optional[date]:
    try:
        return date.fromisoformat(str(s or "")[:10])
    except ValueError:
        return None


def md(d: date) -> str:
    return f"{d.month}/{d.day}"


def money(x: float, unit: str) -> str:
    x = float(x)
    return f"{unit} {x:,.0f}" if x.is_integer() else f"{unit} {x:,.2f}"


def ev_label(ev: dict, brands: dict) -> str:
    b = (brands.get(ev.get("brand_id")) or {}).get("name") or ev.get("brand_id") or ""
    t = ev.get("event_name") if ev.get("event_type") == "other" and ev.get("event_name") else TYPE_ZH.get(
        ev.get("event_type"), "促销")
    return f"{b} {t}".strip()


def ev_range(ev: dict) -> tuple:
    a = parse_day(ev.get("start"))
    b = parse_day(ev.get("end")) or a
    if a and b and b < a:
        b = a
    return a, b


def relevant_events(brand_ids: set, region: str, events: list) -> list:
    return [e for e in events or [] if e.get("region") == region and e.get("brand_id") in brand_ids]


def item_brand_ids(item: dict, identified: dict, pages: list) -> set:
    """商品的品牌 + 已勾选商品页所属的零售商（品牌 id）。"""
    ids = {x for x in (item.get("brand_id"), (identified or {}).get("brand_id")) if x}
    ids |= {p["retailer"] for p in pages or [] if p.get("selected") and p.get("retailer")}
    return ids


def best_price(prices: list) -> Optional[dict]:
    """可比较（币种和地区对得上）的最新价格里最低的那个。"""
    ok = [p for p in prices or [] if p.get("comparable") and p.get("price") is not None]
    return min(ok, key=lambda p: p["price"]) if ok else None


def recommend(item: dict, prices: list, events: list, today: date, deadline_days: int = 3) -> dict:
    """→ {action, date?, reason}。prices：[{price, currency, comparable, retailer_name, url}]；
    events：已经筛过的相关活动（relevant_events）。"""
    region = item.get("region")
    unit = REGION_UNIT.get(region, "")
    target = item.get("target_price")
    deadline = parse_day(item.get("deadline"))
    best = best_price(prices)
    if target is not None and best and best["price"] <= float(target):
        return {"action": "buy_now", "reason": f"{best.get('retailer_name') or '商品页'} 现价 "
                                               f"{money(best['price'], unit)}，不高于心理价 {money(float(target), unit)}"}
    if deadline and deadline < today:
        return {"action": "deadline", "date": deadline.isoformat(), "reason": f"已经过了最晚购买日 {md(deadline)}"}
    upcoming = []
    rumors = []
    active = []
    for ev in events or []:
        a, b = ev_range(ev)
        if a and b and a <= today <= b and ev.get("status") == "confirmed":
            active.append((b, ev))
            continue
        if a and b and b < today:
            continue                                     # 已经结束
        if ev.get("status") == "confirmed" and a and a > today and (deadline is None or a <= deadline):
            upcoming.append((a, ev))
        elif ev.get("status") != "confirmed" and (a is None or deadline is None or a <= deadline):
            rumors.append(ev)
    if upcoming:
        a, ev = min(upcoming, key=lambda x: (x[0], x[1].get("id", "")))
        why = f"{ev.get('_label') or ev.get('event_name') or '相关活动'}（已官宣）{md(a)} 开始"
        if deadline:
            why += f"，在最晚购买日 {md(deadline)} 之前"
        if ev.get("discount"):
            why += f"；{ev['discount']}"
        return {"action": "wait_until", "date": a.isoformat(), "reason": why}
    if deadline and (deadline - today).days <= deadline_days:
        n = (deadline - today).days
        why = "今天是最晚购买日" if n == 0 else f"离最晚购买日 {md(deadline)} 还有 {n} 天"
        if best:
            why += f"；现在最低 {money(best['price'], unit)}（{best.get('retailer_name') or '商品页'}）"
        return {"action": "deadline", "date": deadline.isoformat(), "reason": why}
    if active:
        b, ev = min(active, key=lambda x: x[0])
        return {"action": "watch", "reason": f"{ev.get('_label') or '相关活动'} 正在进行（到 {md(b)}），价格还没到心理价"}
    if rumors:
        names = "、".join(sorted({r.get("_label") or r.get("event_name") or "活动" for r in rumors}))[:120]
        return {"action": "watch", "reason": f"只有传闻：{names}"}
    return {"action": "watch", "reason": "暂时没有相关活动" + ("" if best else "，也还没读到价格")}


def reminders_for(item: dict, entry: dict, events: list, today: date, notified: dict,
                  deadline_days: int = 3) -> list:
    """一件商品的提醒（已经去掉发过的）。entry 是 cart_state 里这件商品的记录；events 是相关活动。"""
    keys = (notified or {}).get("keys") or {}
    iid = item["id"]
    name = item.get("name") or iid
    region = item.get("region")
    unit = REGION_UNIT.get(region, "")
    deadline = parse_day(item.get("deadline"))
    out = []
    tomorrow = today + timedelta(days=1)
    for ev in events or []:
        a, b = ev_range(ev)
        if a != tomorrow or (deadline and a > deadline):
            continue
        key = f"event:{iid}:{ev.get('id')}:{a.isoformat()}"
        if key in keys:
            continue
        st = "已官宣" if ev.get("status") == "confirmed" else "传闻"
        text = f"{ev.get('_label') or '相关活动'}（{st}）明天 {md(a)} 开始" + (f"，{ev['discount']}" if ev.get(
            "discount") else "")
        out.append({"key": key, "kind": "event", "item_id": iid, "item": name, "text": text,
                    "url": next((s.get("url") for s in ev.get("sources") or [] if s.get("url")), None)})
    target = item.get("target_price")
    best = best_price((entry or {}).get("prices") or [])
    if target is not None and best and best["price"] <= float(target):
        p = round(float(best["price"]), 2)
        sent = [float(k.rsplit(":", 1)[1]) for k in keys if k.startswith(f"price:{iid}:")
                and _num(k.rsplit(":", 1)[1]) is not None]
        if not sent or p < min(sent):
            out.append({"key": f"price:{iid}:{p:.2f}", "kind": "price", "item_id": iid, "item": name,
                        "price": p, "url": best.get("url"),
                        "text": f"{best.get('retailer_name') or '商品页'} 现价 {money(p, unit)}，"
                                f"不高于心理价 {money(float(target), unit)}"})
    if deadline:
        n = (deadline - today).days
        key = f"deadline:{iid}:{deadline.isoformat()}"
        if 0 <= n <= deadline_days and key not in keys:
            text = "今天是最晚购买日" if n == 0 else f"离最晚购买日 {md(deadline)} 还有 {n} 天"
            out.append({"key": key, "kind": "deadline", "item_id": iid, "item": name, "text": text})
    return out


def _num(s) -> Optional[float]:
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


KIND_ZH = {"price": "价格到了", "event": "活动明天开始", "deadline": "截止临近"}


def build_digest(reminders: list, items: list, entries: dict, today: date, max_items: int = 20) -> Optional[dict]:
    """有提醒才写；中文；按商品分组。→ {subject, body} 或 None。"""
    if not reminders:
        return None
    by_item: dict = {}
    for r in reminders:
        by_item.setdefault(r["item_id"], []).append(r)
    order = [it for it in items if it["id"] in by_item][:max_items]
    subject = f"黑五购物提醒 · {today.month} 月 {today.day} 日 · {len(order)} 件商品有动静"
    lines = [f"今天（{today.month} 月 {today.day} 日）购物车里有 {len(order)} 件商品有提醒：", ""]
    for i, it in enumerate(order, 1):
        e = entries.get(it["id"]) or {}
        unit = REGION_UNIT.get(it.get("region"), "")
        bits = [REGION_ZH.get(it.get("region"), "")]
        if it.get("target_price") is not None:
            bits.append(f"心理价 {money(float(it['target_price']), unit)}")
        d = parse_day(it.get("deadline"))
        if d:
            bits.append(f"最晚 {md(d)} 买")
        lines.append(f"{i}. {it.get('name')}（{' · '.join(b for b in bits if b)}）")
        ident = e.get("identified") or {}
        if ident.get("model"):
            lines.append(f"   型号：{' '.join(x for x in (ident.get('brand'), ident.get('model')) if x)}"
                         + (f"（{ident['specs']}）" if ident.get("specs") else "")
                         + ("" if ident.get("confirmed") else "  ← AI 识别，未确认"))
        for r in by_item[it["id"]]:
            lines.append(f"   · {KIND_ZH.get(r['kind'], '提醒')}：{r['text']}")
            if r.get("url"):
                lines.append(f"     {r['url']}")
        rec = e.get("recommendation") or {}
        if rec.get("action"):
            lines.append(f"   建议：{ACTION_ZH.get(rec['action'], rec['action'])}"
                         + (f"（{md(parse_day(rec['date']))}）" if parse_day(rec.get("date")) else "")
                         + (f" —— {rec['reason']}" if rec.get("reason") else ""))
        lines.append("")
    blocked = sum(1 for it in items for p in (entries.get(it["id"]) or {}).get("pages") or []
                  if p.get("selected") and p.get("status") in ("blocked", "none", "error"))
    if blocked:
        lines.append(f"另有 {blocked} 个勾选的商品页读不到价格，请在 Obsidian 购物车里点链接用浏览器查看。")
    lines.append("价格以商品页为准。这封邮件由 Deal Radar 根据你的购物车自动生成，每天最多 1 封。")
    return {"subject": subject, "body": "\n".join(lines).rstrip() + "\n"}
