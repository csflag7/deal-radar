"""官网所见（人工记录，config/manual_events.yaml）→ 候选事件。

- 不走 AI、不走 judge；之后走 status.finalize_candidates 生成 other 的 sale_key（跳过类型校正）。
- 来源：level=official、quote_in="official_page"、published=查看日期、manual=true。
- 读入时校验：brand_id 在清单里、region / event_type 合规、日期是 YYYY-MM-DD 并且出现在原文 quote 里、
  url 是网页地址。不合格的丢弃并打 warning。
- 优先级：overrides > manual > AI（见 merge.py）。
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Optional

from .config import EVENT_TYPES, REGIONS
from .status import date_in_quote, host_of, status_of

log = logging.getLogger(__name__)


def _iso(v) -> Optional[str]:
    if v is None or v == "":
        return None
    if isinstance(v, (date, datetime)):
        return v.isoformat()[:10]
    s = str(v).strip()
    date.fromisoformat(s)          # 不是 YYYY-MM-DD 会抛 ValueError
    return s


def _check(ev: dict, brands: dict) -> tuple:
    """返回 (规范化后的条目, None) 或 (None, 不合格原因)。"""
    if not isinstance(ev, dict):
        return None, "不是一条记录"
    if ev.get("brand_id") not in brands:
        return None, f"brand_id {ev.get('brand_id')!r} 不在品牌清单里"
    if ev.get("region") not in REGIONS or ev.get("region") == "unknown":
        return None, f"region {ev.get('region')!r} 只能是 US / CA"
    if ev.get("event_type") not in EVENT_TYPES:
        return None, f"event_type {ev.get('event_type')!r} 不合规"
    quote = " ".join(str(ev.get("quote") or "").split())
    if not quote:
        return None, "没有原文 quote"
    url = str(ev.get("url") or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        return None, "url 不是网页地址"
    try:
        start, end, seen = _iso(ev.get("start")), _iso(ev.get("end")), _iso(ev.get("seen"))
    except ValueError:
        return None, "日期不是 YYYY-MM-DD"
    for name, v in (("start", start), ("end", end)):
        if v and not date_in_quote(v, quote):
            return None, f"{name} {v} 不在原文 quote 里"
    if start and end and end < start:
        return None, "end 早于 start"
    return dict(ev, quote=quote, url=url, start=start, end=end, seen=seen), None


def load_manual_candidates(entries: list, brands: dict, default_year: int, dropped: Optional[dict] = None) -> list:
    """把 manual_events.yaml 的条目变成候选（和 status.judge 的输出同样的结构）。"""
    out = []
    for i, raw in enumerate(entries or []):
        ev, why = _check(raw, brands)
        if why:
            log.warning("manual_events.yaml 第 %d 条不合格，丢弃：%s", i + 1, why)
            if dropped is not None:
                dropped["人工记录不合格"] = dropped.get("人工记录不合格", 0) + 1
            continue
        year = int(ev["start"][:4]) if ev["start"] else (int(ev["end"][:4]) if ev["end"] else default_year)
        name = " ".join(str(ev.get("event_name") or "").split()) or None
        discount = " ".join(str(ev.get("discount") or "").split()) or None
        fields = {"start": ev["start"], "end": ev["end"], "discount": discount}
        status = status_of("official", fields)
        seen = ev["seen"] or date.today().isoformat()
        out.append({
            "brand_id": ev["brand_id"], "region": ev["region"], "event_type": ev["event_type"],
            "event_name": name, "year": year, **fields, "quote": ev["quote"],
            "status": status, "manual": True, "ai_start": None, "ai_end": None,
            "source": {
                "title": name or ev["url"], "url": ev["url"], "domain": host_of(ev["url"]),
                "name": "官网（人工记录）", "level": "official", "status": status, **fields,
                "quote": ev["quote"], "quote_in": "official_page",
                "published": f"{seen}T00:00:00+00:00", "manual": True,
            },
        })
    return out
