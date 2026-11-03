"""商品页清单和站内搜索链接（第一版：用户贴链接 + 程序给出各零售商的站内搜索链接；sitemap 匹配以后再说）。

- 用户在「型号或链接」里填的是 http(s) 网址 → 直接当成要读价格的商品页（默认勾选）
- 用户在卡片上「添加商品页链接」→ 写进 cart.json 的 pages（[{url, selected}]，用户字段）；勾选 / 取消勾选也记在那里
- 站内搜索链接：brands.yaml 里该地区有店的品牌官网 + cart.yaml 的主要零售商（按美 / 加区分）；只显示，程序不去抓
- 页面域名 → 零售商 / 品牌（bestbuy.ca → bestbuy），用来找相关活动
"""
from __future__ import annotations

import re
from typing import Optional
from urllib.parse import quote_plus, urlparse

from ..status import domain_matches

URL_RE = re.compile(r"^https?://[^\s\"'<>]+$", re.I)
MAX_URL = 500


def safe_url(u) -> Optional[str]:
    t = str(u or "").strip()
    if len(t) > MAX_URL or not URL_RE.match(t):
        return None
    return t.split("#")[0]


def host(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def url_region(url: str) -> str:
    """网址看起来是哪个地区的：.ca 域名或路径以 /ca、/en-ca 开头 → CA，否则 US。"""
    h = host(url)
    segs = [s.lower() for s in urlparse(url).path.split("/") if s][:2]
    if h.endswith(".ca") or any(s in ("ca", "en-ca", "en_ca", "fr-ca", "ca-en") for s in segs):
        return "CA"
    return "US"


def retailer_of(url: str, cart_cfg: dict, brands: dict) -> tuple:
    """(brand_id 或 None, 显示名)。先看 cart.yaml 的零售商，再看 brands.yaml 的官方域名。"""
    h = host(url)
    for r in cart_cfg.get("retailers") or []:
        if any(domain_matches(h, d) for d in r.get("domains") or []):
            return r["id"], r.get("name") or r["id"]
    for bid, b in brands.items():
        doms = list(b.get("domains") or [])
        doms += [host(u) for u in (b.get("shop") or {}).values() if u]
        if any(d and domain_matches(h, d.lower().removeprefix("www.")) for d in doms):
            return bid, b.get("name") or bid
    return None, (h.removeprefix("www.") or "未知网站")


def user_pages(item: dict) -> list:
    """cart.json 里用户自己的页面：「型号或链接」里的网址 + pages 字段。→ [(url, selected 或 None)]。"""
    out, seen = [], set()
    model_url = safe_url(item.get("model"))
    marks = {}
    for p in item.get("pages") or []:
        if isinstance(p, dict) and safe_url(p.get("url")):
            marks[safe_url(p["url"])] = bool(p.get("selected", True))
    if model_url:
        out.append((model_url, marks.get(model_url, True)))
        seen.add(model_url)
    for u, sel in marks.items():
        if u not in seen:
            out.append((u, sel))
            seen.add(u)
    return out


def build_pages(item: dict, cart_cfg: dict, brands: dict, suggested: Optional[list] = None) -> list:
    """→ [{url, retailer, retailer_name, region, selected, source: user|suggested}]。
    suggested：程序找到的页面（第一版没有，留给以后的 sitemap 匹配），默认不勾选，用户勾了才读。"""
    pages = []
    seen = set()
    marks = {u: s for u, s in user_pages(item)}
    for u, sel in user_pages(item):
        rid, rname = retailer_of(u, cart_cfg, brands)
        pages.append({"url": u, "retailer": rid, "retailer_name": rname, "region": url_region(u),
                      "selected": bool(sel), "source": "user"})
        seen.add(u)
    for u in suggested or []:
        u = safe_url(u)
        if not u or u in seen:
            continue
        rid, rname = retailer_of(u, cart_cfg, brands)
        pages.append({"url": u, "retailer": rid, "retailer_name": rname, "region": url_region(u),
                      "selected": bool(marks.get(u, False)), "source": "suggested"})
        seen.add(u)
    return pages


def search_query(item: dict, identified: dict, brands: dict) -> str:
    """搜索用的词：确认 / 识别出的型号（前面补上品牌名），没有就用「型号」字段（不是网址时）或商品名。"""
    model = (identified or {}).get("model") or ""
    if not model:
        m = str(item.get("model") or "").strip()
        model = "" if safe_url(m) else m
    if not model:
        model = str(item.get("name") or "").strip()
    brand = (identified or {}).get("brand") or ""
    if not brand and item.get("brand_id") in brands:
        brand = brands[item["brand_id"]].get("name") or ""
    if not brand:
        brand = str(item.get("brand_text") or "").strip()
    q = model
    if brand and brand.lower() not in model.lower():
        q = f"{brand} {model}"
    return " ".join(q.split())[:120]


def search_links(item: dict, query: str, cart_cfg: dict, brands: dict, brand_id: Optional[str] = None) -> list:
    """→ [{retailer, name, url}]：品牌官网（有站内搜索模板就用模板，没有就给官网首页）+ 本地区的主要零售商。"""
    region = item.get("region")
    if not query or region not in ("US", "CA"):
        return []
    q = quote_plus(query)
    out, seen = [], set()
    bid = brand_id or item.get("brand_id")
    b = brands.get(bid) if bid else None
    retailer_ids = {r["id"] for r in cart_cfg.get("retailers") or []}
    if b and (b.get("shop") or {}).get(region) and bid not in retailer_ids:
        tpl = ((cart_cfg.get("brand_search") or {}).get(bid) or {}).get(region)
        url = tpl.replace("{q}", q) if tpl else b["shop"][region]
        out.append({"retailer": bid, "name": f"{b.get('name', bid)} 官网" + ("" if tpl else "（首页）"), "url": url})
        seen.add(bid)
    for r in cart_cfg.get("retailers") or []:
        tpl = (r.get("search") or {}).get(region)
        if tpl and r["id"] not in seen:
            out.append({"retailer": r["id"], "name": r.get("name") or r["id"], "url": tpl.replace("{q}", q)})
            seen.add(r["id"])
    return [x for x in out if safe_url(x["url"])]
