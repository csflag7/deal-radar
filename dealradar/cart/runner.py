"""购物车提醒的一次运行：run(final_events, now, settings) -> {"reminders": [...], "digest": {...} | None}。

① 读 cart.json（只读；坏了就整次跳过，什么都不写）
② 每件商品：型号确认（描述变了才问 AI）→ 商品页清单（用户贴的 + cart.json 的 pages）→ 站内搜索链接
③ 读价格：只读勾选的商品页，每个页面每天最多一次（按本地日期），规矩同官网监控 → prices.json
④ 币种核对（USD↔美国、CAD↔加拿大，对不上不参与比较）→ 建议 → cart_state.json（原子写）
⑤ 提醒（去掉 notified.json 里发过的）→ 汇总正文；待发清单写 cart_pending.json（发送成功后 mail.mark_sent 用）
不发邮件：发不发由调用方决定（mail.send_digest）。
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from ..config import APP_ROOT, load_env, load_yaml, local_date
from . import advice, identify, pages as pg
from .conf import Files, LLM_YAML, load_brands, load_cart_config, read_json, resolve_data_dir, write_json
from .mail import body_hash
from .prices import PoliteFetcher, currency_ok, read_page

log = logging.getLogger(__name__)

HISTORY_KEEP = 120


def load_cart(path: Path) -> tuple:
    """→ (items, error)。只读，不改、不备份（备份是前端的事）。"""
    if not path.exists():
        return [], None
    try:
        doc = read_cart_text(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return None, f"cart.json 读不出来：{type(e).__name__}"
    items = []
    for x in doc.get("items") or []:
        if isinstance(x, dict) and x.get("id") and x.get("region") in ("US", "CA"):
            items.append(x)
    return items, None


def read_cart_text(text: str) -> dict:
    doc = json.loads(text)
    if not isinstance(doc, dict) or not isinstance(doc.get("items"), list):
        raise ValueError("没有 items 列表")
    return doc


def make_llm():
    """DeepSeek 客户端（llm_client.ChatClient）；没有 Key 就返回 None（型号先用用户写的）。"""
    from ..llm_client import ChatClient
    load_env(APP_ROOT / ".env")
    cfg = load_yaml(LLM_YAML) if LLM_YAML.exists() else {}
    key = os.environ.get(cfg.get("api_key_env", "DEEPSEEK_API_KEY"))
    if not key or not cfg.get("base_url"):
        return None
    cfg = dict(cfg, max_tokens=min(int(cfg.get("max_tokens") or 1200), 1200))
    return ChatClient(cfg, key)


def needs_ai(item: dict, prev_ident: Optional[dict], day: str) -> bool:
    """描述变了要问 AI；上次没问成（失败 / 没配 Key）的，每天再试一次；问成了就一直用缓存。"""
    prev_ident = prev_ident or {}
    if prev_ident.get("input_hash") != identify.input_hash(item):
        return True
    return prev_ident.get("source") != "ai" and prev_ident.get("ai_date") != day


def _label_events(events: list, brands: dict) -> list:
    out = []
    for e in events or []:
        if isinstance(e, dict) and e.get("brand_id") and e.get("region"):
            out.append(dict(e, _label=advice.ev_label(e, brands)))
    return out


def _latest(rec: dict) -> tuple:
    h = rec.get("history") or []
    return (h[-1] if h else None), (h[-2] if len(h) > 1 else None)


def price_rows(item: dict, page_list: list, db: dict) -> list:
    """cart_state 里的 prices：勾选页面的最新价格 + 上一次的价格和变化；币种对不上的标出来。"""
    rows = []
    for p in page_list:
        if not p.get("selected"):
            continue
        rec = (db.get("pages") or {}).get(p["url"]) or {}
        last, prev = _latest(rec)
        if not last:
            continue
        ok = currency_ok(last.get("currency"), item.get("region"))
        row = {"url": p["url"], "retailer": p.get("retailer"), "retailer_name": p.get("retailer_name"),
               "date": last.get("date"), "price": last.get("price"), "currency": last.get("currency"),
               "availability": last.get("availability"), "comparable": ok,
               "previous": prev.get("price") if prev and prev.get("currency") == last.get("currency") else None}
        if row["previous"] is not None:
            row["change"] = round(row["price"] - row["previous"], 2)
        if not ok:
            row["currency_note"] = (f"币种 {last.get('currency') or '未知'} 和地区"
                                    f"（{advice.REGION_ZH.get(item.get('region'), '')}）对不上，不参与比较")
        rows.append(row)
    return rows


def record_price(db: dict, url: str, res: dict, now_iso: str, day: str) -> None:
    rec = db.setdefault("pages", {}).setdefault(url, {})
    rec.update(last_attempt=now_iso, last_date=day, status=res["status"], reason=res.get("reason") or None,
               final_url=res.get("final_url") or url)
    if res["status"] != "ok":
        return
    rec["last_ok"] = now_iso
    h = rec.setdefault("history", [])
    entry = {"date": day, "price": res["price"], "currency": res.get("currency"),
             "availability": res.get("availability"), "via": res.get("via")}
    if h and h[-1].get("date") == day:
        h[-1] = entry
    else:
        h.append(entry)
    del h[:-HISTORY_KEEP]


def run(final_events: list, now: datetime, settings: dict, *, data_dir: Optional[Path] = None, llm="auto",
        fetcher=None, brands: Optional[dict] = None, cart_cfg: Optional[dict] = None) -> dict:
    settings = settings or {}
    cart_cfg = cart_cfg if cart_cfg is not None else load_cart_config()
    brands = brands if brands is not None else load_brands()
    files = Files(resolve_data_dir(settings, cart_cfg, data_dir))
    tz = settings.get("timezone", "America/Toronto")
    day = local_date(now, tz)
    today = date.fromisoformat(day)
    now_iso = now.isoformat(timespec="seconds")
    adv = cart_cfg.get("advice") or {}
    deadline_days = int(adv.get("deadline_days", 3))

    items, err = load_cart(files.cart)
    if err:
        log.error("购物车：%s；本次跳过，不写任何文件", err)
        return {"reminders": [], "digest": None, "error": err}
    old_state = read_json(files.state, {})
    old_items = {e.get("item_id"): e for e in old_state.get("items") or [] if isinstance(e, dict)}
    db = read_json(files.prices, {"pages": {}})
    db.setdefault("pages", {})
    events = _label_events(final_events, brands)

    # ② 型号、商品页、搜索链接
    client = None
    if any(needs_ai(it, (old_items.get(it["id"]) or {}).get("identified"), day) for it in items):
        client = make_llm() if llm == "auto" else llm
    entries = {}
    max_c = int((cart_cfg.get("identify") or {}).get("max_candidates", 4))
    try:
        for it in items:
            prev = old_items.get(it["id"]) or {}
            ident = identify.resolve(it, prev.get("identified"), brands, client, day, max_c)
            plist = pg.build_pages(it, cart_cfg, brands)
            q = pg.search_query(it, ident, brands)
            entries[it["id"]] = {"item_id": it["id"], "identified": ident, "pages": plist,
                                 "search_query": q,
                                 "search_links": pg.search_links(it, q, cart_cfg, brands, ident.get("brand_id"))}
    finally:
        if client is not None and llm == "auto" and hasattr(client, "close"):
            client.close()

    # ③ 读价格：勾选的页面，每天一次
    fcfg = cart_cfg.get("fetch") or {}
    todo = []
    for e in entries.values():
        for p in e["pages"]:
            if p["selected"] and p["url"] not in todo and (db["pages"].get(p["url"]) or {}).get("last_date") != day:
                todo.append(p["url"])
    limit = int(fcfg.get("max_pages_per_run", 40))
    if len(todo) > limit:
        log.warning("购物车：要读的商品页 %d 个，超过上限 %d，其余明天再读", len(todo), limit)
        todo = todo[:limit]
    if todo:
        own = fetcher is None
        f = fetcher or PoliteFetcher(dict(fcfg, robots_agent=settings.get("robots_agent", "DealRadar")))
        try:
            for url in todo:
                res = read_page(f, url)
                record_price(db, url, res, now_iso, day)
                log.info("购物车 读价格 %s：%s %s", res["status"], url,
                         res.get("price") if res["status"] == "ok" else res.get("reason"))
        finally:
            if own:
                f.close()
        write_json(files.prices, db)

    # ④ 价格、建议
    for it in items:
        e = entries[it["id"]]
        for p in e["pages"]:
            rec = db["pages"].get(p["url"]) or {}
            if not p["selected"]:
                p["status"] = "unselected"
            elif not rec:
                p["status"] = "pending"
            else:
                p["status"] = rec.get("status")
                p["reason"] = rec.get("reason")
                p["checked"] = rec.get("last_attempt")
        e["prices"] = price_rows(it, e["pages"], db)
        for p in e["pages"]:
            row = next((r for r in e["prices"] if r["url"] == p["url"]), None)
            if row and not row["comparable"]:
                p["currency_mismatch"] = True
        rel = advice.relevant_events(advice.item_brand_ids(it, e["identified"], e["pages"]), it["region"], events)
        e["related_events"] = [ev["id"] for ev in rel if ev.get("id")]
        e["recommendation"] = advice.recommend(it, e["prices"], rel, today, deadline_days)
        e["updated"] = now_iso
    state = {"version": 1, "updated": now_iso, "date": day, "items": [entries[it["id"]] for it in items]}
    write_json(files.state, state)

    # ⑤ 提醒和汇总
    notified = read_json(files.notified, {})
    reminders = []
    for it in items:
        e = entries[it["id"]]
        rel = advice.relevant_events(advice.item_brand_ids(it, e["identified"], e["pages"]), it["region"], events)
        reminders += advice.reminders_for(it, e, rel, today, notified, deadline_days)
    digest = None
    if day in (notified.get("digests") or {}):
        log.info("购物车：今天（%s）已经发过汇总，不再生成", day)
    else:
        digest = advice.build_digest(reminders, items, entries, today)
    if digest:
        write_json(files.pending, {"created": now_iso, "date": day, "hash": body_hash(digest["subject"], digest["body"]),
                                   "keys": [r["key"] for r in reminders]})
    elif files.pending.exists():
        files.pending.unlink()
    return {"reminders": reminders, "digest": digest}
