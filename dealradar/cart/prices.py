"""读商品页价格（ADR-008 原则例外：只读用户在购物车里勾选的商品页，每天每页一次）。

规矩和官网监控（official.py）相同：
- UA 如实写 DealRadar/0.1；每个请求（含每一跳跳转）先核对 robots.txt，读不到就当不允许；
- 同一网站两次请求（robots.txt 也算）至少间隔 5 秒；单页总超时 90 秒；
- 被拒（401/403/429、拦截页、robots 不允许）就记下来，不重试、不换方式；同一网站本次后面的页面也跳过；
- 不规避反爬：不用浏览器伪装、不换 UA、不用代理。
复用 official.py 的 SiteClock（同站间隔）、RobotsGate（robots.txt，内部是 articles.RobotsCache）、
BLOCK_STATUS / BLOCK_MARKERS（拦截判断）和 UA，只 import，不修改。

价格只读结构化数据：先找 JSON-LD 里的 Offer / AggregateOffer（price、priceCurrency、availability），
没有就退到 <meta property="og:price:amount"> / product:price:amount。
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Optional
from urllib.parse import urljoin, urlparse

import httpx

from ..official import BLOCK_MARKERS, BLOCK_STATUS, UA, RobotsGate, SiteClock

log = logging.getLogger(__name__)

REDIRECT_STATUS = (301, 302, 303, 307, 308)
CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
REGION_CURRENCY = {"US": "USD", "CA": "CAD"}


# ---------- 解析 ----------
class _Scan(HTMLParser):
    """收集 <script type="application/ld+json"> 的内容和 <meta property/name/itemprop> 的值。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.ld: list = []
        self.meta: dict = {}
        self._buf: Optional[list] = None
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "script" and "ld+json" in a.get("type", "").lower():
            self._buf = []
        elif tag == "meta":
            key = (a.get("property") or a.get("name") or a.get("itemprop") or "").strip().lower()
            if key and key not in self.meta and "content" in a:
                self.meta[key] = a["content"].strip()
        elif tag == "title" and not self.title:
            self._in_title = True

    def handle_data(self, data):
        if self._buf is not None:
            self._buf.append(data)
        elif self._in_title:
            self.title += data

    def handle_endtag(self, tag):
        if tag == "script" and self._buf is not None:
            self.ld.append("".join(self._buf))
            self._buf = None
        elif tag == "title":
            self._in_title = False


def _loads(text: str):
    t = (text or "").strip()
    t = re.sub(r"^\s*(?:<!--|//\s*<!\[CDATA\[)", "", t)
    t = re.sub(r"(?:-->|//\s*\]\]>)\s*$", "", t).strip()
    if not t:
        return None
    try:
        return json.loads(t, strict=False)
    except ValueError:
        return None


def to_price(v) -> Optional[float]:
    """'1,299.99'、'$699'、699、'C$ 49.99' → 数字；读不出来或不合理就 None。"""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        x = float(v)
    else:
        s = str(v).strip().replace("\xa0", " ")
        m = re.search(r"\d[\d.,\s]*", s)
        if not m:
            return None
        num = m.group(0).replace(" ", "").rstrip(".,")
        if "," in num and "." in num:
            if num.rfind(",") > num.rfind("."):  # 1.299,00（欧式）
                num = num.replace(".", "").replace(",", ".")
            else:                                # 1,299.00
                num = num.replace(",", "")
        elif "," in num:                         # 49,99（逗号当小数点）或 1,299（千分位）
            num = num.replace(",", ".") if re.fullmatch(r"\d+,\d{1,2}", num) else num.replace(",", "")
        try:
            x = float(num)
        except ValueError:
            return None
    return round(x, 2) if 0 < x < 1e7 else None


def _types(node: dict) -> set:
    t = node.get("@type")
    ts = t if isinstance(t, list) else [t]
    return {str(x).split("/")[-1].lower() for x in ts if x}


def _avail(v) -> Optional[str]:
    if not v:
        return None
    if isinstance(v, list):
        v = v[0] if v else None
    s = str(v).strip().rsplit("/", 1)[-1]
    s = re.sub(r"[^A-Za-z]", "", s)
    return s[:40] or None


def _offers_of(node, currency_hint=None) -> list:
    """一个 offers 字段（Offer / AggregateOffer / 列表）→ [{price, currency, availability}]。"""
    out = []
    if isinstance(node, list):
        for x in node:
            out += _offers_of(x, currency_hint)
        return out
    if not isinstance(node, dict):
        return out
    types = _types(node)
    cur = node.get("priceCurrency") or currency_hint
    spec = node.get("priceSpecification")
    if isinstance(spec, list):
        spec = next((s for s in spec if isinstance(s, dict) and s.get("price") is not None), None)
    if "aggregateoffer" in types or ("lowPrice" in node and "price" not in node):
        if node.get("offers"):
            out += _offers_of(node["offers"], cur)
        p = to_price(node.get("lowPrice"))
        if p is None:
            p = to_price(node.get("price"))
        if p is not None:
            out.append({"price": p, "currency": cur, "availability": _avail(node.get("availability"))})
        return out
    p = to_price(node.get("price"))
    if p is None and isinstance(spec, dict):
        p = to_price(spec.get("price"))
        cur = cur or spec.get("priceCurrency")
    if p is not None:
        out.append({"price": p, "currency": cur, "availability": _avail(node.get("availability"))})
    return out


PRODUCT_TYPES = {"product", "productgroup", "individualproduct", "productmodel", "car", "vehicle", "book",
                 "softwareapplication"}


def _variant_offers(node: dict, depth: int = 0) -> list:
    """一个商品节点自己的报价 + 它的变体（hasVariant）的报价：都算同一件商品。"""
    if depth > 4 or not isinstance(node, dict):
        return []
    out = _offers_of(node["offers"]) if node.get("offers") is not None else []
    vs = node.get("hasVariant")
    for v in vs if isinstance(vs, list) else [vs] if isinstance(vs, dict) else []:
        out += _variant_offers(v, depth + 1)
    return out


def _walk(obj, found: list, depth: int = 0) -> None:
    """找页面的主商品：JSON-LD 顶层、@graph 里、WebPage.mainEntity 里的商品节点（含变体）。
    不进 ItemList（列表页上的一串商品不是“这件商品”的价格）。found 收集 (商品标识, 报价列表)。"""
    if depth > 8:
        return
    if isinstance(obj, list):
        for x in obj:
            _walk(x, found, depth + 1)
        return
    if not isinstance(obj, dict):
        return
    if "@graph" in obj:
        _walk(obj["@graph"], found, depth + 1)
    types = _types(obj)
    if types & PRODUCT_TYPES or (not types and obj.get("offers") is not None):
        offers = _variant_offers(obj)
        if offers:
            ident = next((" ".join(str(obj[k]).split()).lower() for k in ("name", "sku", "productGroupID", "mpn")
                          if obj.get(k)), "")
            found.append((ident, offers))
        return
    if "mainEntity" in obj:
        _walk(obj["mainEntity"], found, depth + 1)


def _pick(offers: list) -> dict:
    """有货的报价里取最低价；都没货就取所有报价的最低价。"""
    instock = [o for o in offers if (o.get("availability") or "").lower() in ("instock", "limitedavailability",
                                                                             "onlineonly", "instoreonly", "presale",
                                                                             "preorder")]
    pool = instock or offers
    return min(pool, key=lambda o: o["price"])


def _clean_currency(c) -> Optional[str]:
    s = str(c or "").strip().upper()
    return s if CURRENCY_RE.match(s) else None


def extract_price(html: str) -> tuple:
    """→ (结果 或 None, 读不到的原因)。结果 = {price, currency, availability, via: jsonld|meta, offers: 报价个数}。
    页面上有几件不同的商品（多半是列表页、推荐区）就不认价格。"""
    sc = _Scan()
    try:
        sc.feed(html or "")
        sc.close()
    except Exception:              # 残缺的 HTML：能收集多少算多少
        pass
    found: list = []
    for raw in sc.ld:
        _walk(_loads(raw), found)
    if found:
        idents = {i for i, _ in found if i}
        if len(idents) > 1:
            return None, f"页面上有 {len(idents)} 件不同的商品（可能是列表页），不认价格"
        offers = [o for _, offs in found for o in offs]
        best = _pick(offers)
        return ({"price": best["price"], "currency": _clean_currency(best.get("currency")),
                 "availability": best.get("availability"), "via": "jsonld", "offers": len(offers)}, "")
    m = sc.meta
    amount = m.get("og:price:amount") or m.get("product:price:amount")
    p = to_price(amount)
    if p is not None:
        cur = m.get("og:price:currency") or m.get("product:price:currency")
        av = m.get("product:availability") or m.get("og:availability")
        return {"price": p, "currency": _clean_currency(cur), "availability": _avail(av), "via": "meta",
                "offers": 1}, ""
    return None, "页面里没有结构化价格（JSON-LD / og:price）"


def parse_price(html: str) -> Optional[dict]:
    return extract_price(html)[0]


ID_RE = re.compile(r"\d{5,}")


def same_page(url: str, final: str) -> bool:
    """跳转后还是不是同一件商品：路径一样（不管大小写、末尾斜杠），或者原网址里的商品编号（≥5 位数字）
    或最后一段 slug 出现在新网址里。跳到列表页、首页、别的商品，就不认这个价格。"""
    a, b = urlparse(url), urlparse(final)
    pa, pb = a.path.rstrip("/").lower(), b.path.rstrip("/").lower()
    if pa == pb:
        return True
    ids = ID_RE.findall(a.path + "?" + a.query)
    if ids and any(i in (b.path + "?" + b.query) for i in ids):
        return True
    slug = pa.rsplit("/", 1)[-1]
    return len(slug) >= 6 and slug in pb


def currency_ok(currency: Optional[str], region: str) -> bool:
    """USD 对美国、CAD 对加拿大；币种未知或对不上都不参与比较。"""
    return bool(currency) and REGION_CURRENCY.get(region) == currency


# ---------- 读取 ----------
@dataclass
class FetchResult:
    url: str
    status: str                 # ok / blocked / error
    reason: str = ""
    html: str = ""
    final_url: str = ""
    http_status: Optional[int] = None


class PoliteFetcher:
    """同步读取器：每一跳先核对 robots → 等够同站间隔 → GET（不自动跟跳转、不重试）。"""

    def __init__(self, settings: Optional[dict] = None, client: Optional[httpx.Client] = None, robots=None,
                 clock: Optional[SiteClock] = None, sleep=time.sleep, monotonic=time.monotonic):
        s = settings or {}
        self.delay = float(s.get("site_delay_seconds", 5))
        self.timeout = float(s.get("page_timeout_seconds", 90))
        self.max_redirects = int(s.get("max_redirects", 3))
        self.max_bytes = int(s.get("max_bytes", 6_000_000))
        self.clock = clock or SiteClock(self.delay)
        self.refused: set = set()             # 本次被拒的网站（host），后面的页面直接跳过
        self.sleep, self.monotonic = sleep, monotonic
        self._own_gate = robots is None
        self.robots = robots or RobotsGate(self.clock, self.refused, s.get("robots_agent", "DealRadar"),
                                           max_redirects=self.max_redirects)
        self._own_client = client is None
        self.client = client or httpx.Client(
            headers={"User-Agent": UA, "Accept": "text/html,application/xhtml+xml"}, follow_redirects=False,
            timeout=httpx.Timeout(self.timeout, connect=min(self.timeout, 20.0)))

    def close(self) -> None:
        if self._own_client:
            self.client.close()
        if self._own_gate and hasattr(self.robots, "close"):
            self.robots.close()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    @staticmethod
    def _robots(r) -> tuple:
        if isinstance(r, tuple):
            return bool(r[0]), (r[1] if len(r) > 1 else "") or ("" if r[0] else "robots.txt 不允许")
        return bool(r), "" if r else "robots.txt 不允许"

    def fetch(self, url: str) -> FetchResult:
        start = self.monotonic()
        cur = url
        for hop in range(self.max_redirects + 1):
            host = (urlparse(cur).hostname or "").lower()
            if SiteClock.key(host) in {SiteClock.key(h) for h in self.refused}:
                return FetchResult(url, "blocked", "同一网站本次已被拒，跳过", final_url=cur)
            ok, why = self._robots(self.robots(cur))
            if not ok:
                return FetchResult(url, "blocked", why if hop == 0 else f"跳转后{why}", final_url=cur)
            wait = self.clock.reserve(host)
            if wait > 0:
                self.sleep(wait)
            try:
                with self.client.stream("GET", cur) as r:
                    code = r.status_code
                    if code in REDIRECT_STATUS:
                        nxt = urljoin(cur, r.headers.get("location", ""))
                        if not nxt.lower().startswith(("http://", "https://")):
                            return FetchResult(url, "error", "跳转地址不对", final_url=cur, http_status=code)
                        cur = nxt
                        continue
                    if code in BLOCK_STATUS:
                        self.refused.add(host)
                        return FetchResult(url, "blocked", f"HTTP {code}", final_url=cur, http_status=code)
                    if code >= 400:
                        return FetchResult(url, "error", f"HTTP {code}", final_url=cur, http_status=code)
                    chunks, size = [], 0
                    for chunk in r.iter_bytes():
                        chunks.append(chunk)
                        size += len(chunk)
                        if size >= self.max_bytes:
                            break
                        if self.monotonic() - start > self.timeout:
                            return FetchResult(url, "error", f"超时（{self.timeout:.0f} 秒）", final_url=cur,
                                               http_status=code)
                    raw = b"".join(chunks)[: self.max_bytes]
                    enc = r.encoding or "utf-8"
            except httpx.TimeoutException:
                return FetchResult(url, "error", f"超时（{self.timeout:.0f} 秒）", final_url=cur)
            except httpx.HTTPError as e:
                return FetchResult(url, "error", f"网络错误 {type(e).__name__}", final_url=cur)
            try:
                html = raw.decode(enc, errors="replace")
            except LookupError:
                html = raw.decode("utf-8", errors="replace")
            head = html[:6000]
            m = BLOCK_MARKERS.search(head)
            if m and len(html) < 60000:
                self.refused.add(host)
                return FetchResult(url, "blocked", f"拦截页（{m.group(0)}）", final_url=cur, http_status=code)
            return FetchResult(url, "ok", "", html=html, final_url=cur, http_status=code)
        return FetchResult(url, "error", f"跳转超过 {self.max_redirects} 次", final_url=cur)


def read_page(fetcher, url: str) -> dict:
    """读一个商品页 → {status: ok|none|blocked|error, reason, price?, currency?, availability?, via?, final_url}。
    none = 打开了，但页面里没有结构化价格（可能要靠 JS 才显示）。"""
    r = fetcher.fetch(url)
    out = {"status": r.status, "reason": r.reason, "final_url": r.final_url or url, "http_status": r.http_status}
    if r.status != "ok":
        return out
    if not same_page(url, r.final_url or url):
        out.update(status="none", reason=f"商品页跳到了别的页面（{(r.final_url or '')[:120]}），可能下架或改了网址")
        return out
    p, why = extract_price(r.html)
    if not p:
        out.update(status="none", reason=why)
        return out
    out.update(p)
    return out
