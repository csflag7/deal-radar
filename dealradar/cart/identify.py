"""型号确认：用 DeepSeek（llm_client.ChatClient，只 import）把购物车里的描述规范成 品牌 + 型号 + 关键规格。

- 描述（商品名、品牌、型号或链接、备注、地区）没变就不再问 AI（按 input_hash 缓存在 cart_state.json）；
  AI 失败的，当天不再重试，第二天再试
- 有歧义时给出 ≤4 个候选；用户在卡片上点选后，前端把选中的写进 cart.json 的 confirmed_model（用户字段），
  这里以用户确认的为准（confirmed = true）
- 不编造网址：AI 只给型号文字，商品页只来自用户
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Optional

from .pages import safe_url

log = logging.getLogger(__name__)

SYSTEM = (
    "你是商品型号规范助手。用户在购物车里随手写了想买的东西（可能是中文、英文、简称或商品网址）。"
    "请判断品牌、具体型号（用零售商网站上常用的英文写法）和关键规格（容量、尺寸、颜色、代数等，没有就空字符串）。"
    "只根据给出的文字和常识判断，不要编造不存在的型号；把握不大时 confidence 给低一些。"
    "如果描述能对应多个不同型号（例如不同代、不同配置），ambiguous 设为 true，并在 candidates 里给出最多 4 个最可能的候选，"
    "第一个是你认为最可能的；没有歧义时 candidates 只放一个（就是你的答案）。"
    "只输出 JSON 对象：{\"brand\": str, \"model\": str, \"specs\": str, \"confidence\": 0到1的数, "
    "\"ambiguous\": bool, \"candidates\": [{\"brand\": str, \"model\": str, \"specs\": str}]}"
)

FIELDS = ("name", "brand_id", "brand_text", "model", "note", "region")


def input_hash(item: dict) -> str:
    raw = json.dumps([str(item.get(k) or "") for k in FIELDS], ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _s(v, n: int) -> str:
    return " ".join(str(v or "").split())[:n]


def brand_lookup(name: str, brands: dict) -> Optional[str]:
    """品牌名 → brands.yaml 的 id（按 id、name、aliases，不分大小写、忽略符号）。"""
    k = re.sub(r"[^a-z0-9]+", "", (name or "").lower())
    if not k:
        return None
    for bid, b in brands.items():
        for x in [bid, b.get("name"), *(b.get("aliases") or [])]:
            if x and re.sub(r"[^a-z0-9]+", "", str(x).lower()) == k:
                return bid
    return None


def _cand(c: dict, brands: dict, fallback_brand_id: Optional[str]) -> Optional[dict]:
    if not isinstance(c, dict):
        return None
    model = _s(c.get("model"), 200)
    if not model:
        return None
    brand = _s(c.get("brand"), 100)
    return {"brand_id": brand_lookup(brand, brands) or fallback_brand_id, "brand": brand, "model": model,
            "specs": _s(c.get("specs"), 300)}


def describe(item: dict, brands: dict) -> str:
    b = brands.get(item.get("brand_id") or "") or {}
    lines = [f"商品名：{_s(item.get('name'), 200)}",
             f"品牌：{b.get('name') or _s(item.get('brand_text'), 100) or '（没写）'}",
             f"型号或链接：{_s(item.get('model'), 500) or '（没写）'}",
             f"备注：{_s(item.get('note'), 300) or '（没写）'}",
             f"地区：{'美国' if item.get('region') == 'US' else '加拿大'}"]
    return "\n".join(lines)


def ask_ai(item: dict, brands: dict, client, max_candidates: int = 4) -> dict:
    """调用 AI → 规范化后的结果（字段都截断过，候选 ≤ max_candidates）。出错抛异常。"""
    data = client.chat_json([{"role": "system", "content": SYSTEM},
                             {"role": "user", "content": describe(item, brands)}])
    fb = item.get("brand_id")
    main = _cand(data, brands, fb)
    cands = []
    for c in (data.get("candidates") or [])[: max_candidates + 1]:
        x = _cand(c, brands, fb)
        if x and all((x["model"].lower(), x["specs"].lower()) != (y["model"].lower(), y["specs"].lower())
                     for y in cands):
            cands.append(x)
    cands = cands[:max_candidates]
    if main is None and cands:
        main = cands[0]
    try:
        conf = max(0.0, min(1.0, float(data.get("confidence", 0))))
    except (TypeError, ValueError):
        conf = 0.0
    ambiguous = bool(data.get("ambiguous")) and len(cands) > 1
    return {"brand_id": (main or {}).get("brand_id") or fb, "brand": (main or {}).get("brand", ""),
            "model": (main or {}).get("model", ""), "specs": (main or {}).get("specs", ""),
            "confidence": round(conf, 2), "ambiguous": ambiguous, "candidates": cands if ambiguous else []}


def confirmed_of(item: dict, brands: dict) -> Optional[dict]:
    """cart.json 里用户确认的型号（前端写的 confirmed_model）。格式不对就当没有。"""
    c = item.get("confirmed_model")
    if not isinstance(c, dict) or not _s(c.get("model"), 200):
        return None
    brand = _s(c.get("brand"), 100)
    bid = c.get("brand_id") if c.get("brand_id") in brands else (brand_lookup(brand, brands) or item.get("brand_id"))
    return {"brand_id": bid, "brand": brand, "model": _s(c.get("model"), 200), "specs": _s(c.get("specs"), 300)}


def fallback(item: dict, brands: dict) -> dict:
    """没有 AI 结果时：直接用用户写的（型号字段不是网址才算型号）。"""
    model = str(item.get("model") or "").strip()
    b = brands.get(item.get("brand_id") or "") or {}
    return {"brand_id": item.get("brand_id"), "brand": b.get("name") or _s(item.get("brand_text"), 100),
            "model": "" if safe_url(model) else _s(model, 200), "specs": "", "confidence": 0.0,
            "ambiguous": False, "candidates": []}


def resolve(item: dict, prev: Optional[dict], brands: dict, client, today: str, max_candidates: int = 4) -> dict:
    """→ cart_state 里的 identified：{brand_id, brand, model, specs, confidence, candidates, confirmed, source,
    input_hash, ai_date, error?}。client 为 None（没配 Key）时不问 AI。"""
    h = input_hash(item)
    prev = prev or {}
    base = None
    if prev.get("input_hash") == h and prev.get("source") == "ai":
        base = {k: prev.get(k) for k in ("brand_id", "brand", "model", "specs", "confidence", "ambiguous", "candidates")}
        base.update(source="ai", ai_date=prev.get("ai_date"))
    elif prev.get("input_hash") == h and prev.get("error") and prev.get("ai_date") == today:
        base = dict(fallback(item, brands), source="input", ai_date=today, error=prev.get("error"))
    elif client is None:
        base = dict(fallback(item, brands), source="input", error="没有配置 AI（DEEPSEEK_API_KEY）")
    else:
        try:
            base = dict(ask_ai(item, brands, client, max_candidates), source="ai", ai_date=today)
        except Exception as e:                       # AI 出错：先用用户写的，明天再试
            log.warning("型号确认失败（%s）：%s", item.get("id"), type(e).__name__)
            base = dict(fallback(item, brands), source="input", ai_date=today, error=f"AI 出错：{type(e).__name__}")
    base["input_hash"] = h
    base["candidates"] = list(base.get("candidates") or [])
    conf = confirmed_of(item, brands)
    if conf:
        base.update(conf, confirmed=True, confidence=1.0)
    else:
        base["confirmed"] = False
    return base
