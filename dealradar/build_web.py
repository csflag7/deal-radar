"""生成网页数据 web/data/data.js（window.DEAL_DATA = {meta, categories, brands, sales}）。

整个对象用 json.dumps 生成，不拼字符串；字段和 web/index.html 现在读的一致。
"""
from __future__ import annotations

import json
from pathlib import Path

EVENT_NAMES_ZH = {
    "early_bf": "提前黑五",
    "black_friday": "黑五",
    "cyber_monday": "网一",
    "cyber_week": "网一周",
    "boxing_day": "节礼日",
    "other": "促销活动",
}


def display_name(e: dict) -> str:
    """网页和推送里的活动标题：非 other 一律用类型的中文名（AI 给的活动名常从别家文章串过来，只放详情）；
    other 用活动名。"""
    t = e.get("event_type")
    if t == "other" and e.get("event_name"):
        return e["event_name"]
    return EVENT_NAMES_ZH.get(t, t or "")
PREFIX = "// 由 dealradar 自动生成，请勿手改（人工纠错写在 config/overrides.yaml）。\nwindow.DEAL_DATA = "


def build_data(final_events: list, categories: list, brands: dict, generated_at: str) -> dict:
    sales = []
    for e in final_events:
        if e["brand_id"] not in brands:
            continue
        sales.append({
            "id": e["id"],
            "brand": e["brand_id"],
            "region": e["region"],
            "event_type": e.get("event_type"),     # 网页用：没有开始日期的 other 只放在列表的“其他消息”里
            "event": display_name(e),
            "event_name": e.get("event_name") or None,   # AI 给的活动名，只在详情里显示
            "start": e.get("start"),
            "end": e.get("end"),
            "discount": e.get("discount") or "",
            "status": "confirmed" if e.get("status") == "confirmed" else "rumored",
            # 每条来源带上自己的级别（官网 / 权威媒体 / 其他）和这条证据的状态，网页详情里显示
            "sources": [{"title": s.get("title") or s.get("url") or "", "url": s.get("url") or "",
                         "level": s.get("level") or "other",
                         "status": s.get("status") if s.get("status") in ("confirmed", "rumored") else None}
                        for s in e.get("sources", [])],
            "updated": (e.get("updated") or "")[:10],
            # 来源之间日期有分歧（同级同票）时列出各个值，网页详情里显示“日期有分歧：A / B”
            "date_conflict": e.get("date_conflict") or None,
        })
    return {
        "meta": {"generated_at": generated_at, "sample": False},
        "categories": [{"id": c["id"], "name": c["name"], "color": c["color"]} for c in categories],
        "brands": [{"id": bid, "name": b["name"], "category": b["category"], "shop": b.get("shop", {})}
                   for bid, b in brands.items()],
        "sales": sales,
    }


def render(data: dict) -> str:
    return PREFIX + json.dumps(data, ensure_ascii=False, indent=2) + ";\n"


def parse(text: str) -> dict:
    """反向解析 data.js（测试和自检用）。"""
    start = text.index("window.DEAL_DATA = ") + len("window.DEAL_DATA = ")
    return json.loads(text[start:].rstrip().rstrip(";"))


def write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".js.tmp")
    tmp.write_text(render(data), encoding="utf-8")
    tmp.replace(path)
