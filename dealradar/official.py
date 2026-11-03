"""S15 官网监控（ADR-006）：直接看品牌官网的促销页，页面有变化才交给 AI。只在 Mac 本地跑（家用 IP）。

硬性规定：不开 stealth / undetected / simulate_user / override_navigator / magic，不用随机 UA、不用代理、
不伪造指纹。UA 如实写 DealRadar/0.1；开 check_robots_txt，另外再用自己的 robots 检查（读不到就当不允许）。
被拒（401/403/429、Access Denied、captcha、Cloudflare 挑战页、robots 不允许）就记下来跳过，不重试、不换方式。

1) 找页面（--discover，或页面清单超过 7 天自动重找）：每个品牌 × 有店的地区，从 brands.yaml 的 shop 链接出发，
   用 Crawl4AI 的 AsyncUrlSeeder 读 sitemap（它先找 /sitemap.xml，找不到再读 robots.txt 里列出的）。
   发现阶段的请求经过 PoliteTransport：如实 UA、每个请求先核对 robots、同一网站间隔 ≥5 秒、每个网站最多 10 次请求、
   被拒（401/403/429）就不再请求这个网站；商品 / 图片 / 门店这类子 sitemap 直接跳过（本地跳过，不发请求）。
   按关键词给 URL 打分，只留本地区的路径、去掉单品页，每个站点最多 4 页 + 首页，写进 config/official_pages.yaml
   （origin: sitemap / homepage / manual；manual 是人工加的，重新发现时保留）。
2) 每天抓：同一网站串行、间隔 ≥5 秒，不同网站最多同时 3 个。取 raw markdown（没有就 fit markdown），
   只截取命中活动关键词或日期的段落（前后各 1 段，≤2500 字符），算 sha256，和 data/official_state.json 比较，
   变了才交给 AI。状态文件只存哈希、时间和状态，不存正文。
3) 提取：复用 extract / llm_client；来源是品牌官网（official），地区直接用页面所属的地区；
   之后照常走 judge（引句校验、逐字段核对）→ finalize → merge。
4) 被拒的页面在状态文件里记 blocked + 原因，同时生成 data/manual_review.json（需人工查看的清单）。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

import httpx
import yaml

from .articles import DATE_RE, RobotsCache, make_excerpt
from .config import load_json, save_json
from .status import domain_matches, host_of

log = logging.getLogger(__name__)

os.environ.setdefault("CRAWL4AI_NO_CLOUD_NOTICE", "1")

UA = "DealRadar/0.1 (personal deal calendar)"

# ---------- 1. 找页面：打分、地区、单品页 ----------
URL_KEYWORDS = [
    (re.compile(r"black[-_ ]?friday", re.I), 5),
    (re.compile(r"boxing[-_ ]?(?:day|week)", re.I), 5),
    (re.compile(r"cyber[-_ ]?(?:monday|week)", re.I), 5),
    (re.compile(r"(?<![a-z])deals?(?![a-z])", re.I), 2),
    (re.compile(r"(?<![a-z])sales?(?![a-z])", re.I), 2),
    (re.compile(r"(?<![a-z])holiday", re.I), 2),
    (re.compile(r"(?<![a-z])events?(?![a-z])", re.I), 1),
]
PRODUCT_SEGMENTS = {"p", "pd", "ip", "dp", "product", "products", "prod", "pdp", "sku", "skus", "item", "items"}
SKIP_EXT = (".pdf", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".xml", ".gz", ".json", ".txt", ".mp4")
# 国家 / 语言段：美国页不能带别国的，加拿大页要带加拿大的
COUNTRIES = {"gb", "uk", "au", "nz", "ie", "de", "fr", "es", "it", "nl", "be", "jp", "kr", "cn", "hk", "tw", "sg",
             "mx", "br", "ar", "ch", "at", "se", "no", "dk", "fi", "pl", "pt", "ae", "sa", "za", "ph", "th", "vn",
             "ca", "us", "eu", "uae", "latam", "intl"}
LOCALE_RE = re.compile(r"^[a-z]{2}[-_][a-z]{2}$", re.I)
CA_LOCALES = {"en-ca", "en_ca", "ca-en", "ca_en"}
US_LOCALES = {"en-us", "en_us", "us-en", "us_en"}
FRENCH = {"fr", "fr-ca", "fr_ca", "ca-fr", "ca_fr"}
# 非英文的语言段（加拿大另外允许法语）：/ca/de、/us/zh、/us/es-mx 之类不要
LANGS = {"de", "zh", "ja", "ko", "es", "it", "nl", "pt", "ru", "sv", "da", "nb", "no", "fi", "pl", "tr", "ar", "he",
         "th", "vi", "id", "ms", "cs", "hu", "el", "uk", "ro", "hi", "fr"}
IRRELEVANT_RE = re.compile(r"(?<![a-z])(?:legal|privacy|terms|careers?|jobs|easter|valentines?|mothers?-day|"
                           r"fathers?-day|back-to-school|accessibility|investors?|cookie)(?![a-z])", re.I)


def url_score(url: str) -> int:
    path = urlparse(url).path
    return sum(w for pat, w in URL_KEYWORDS if pat.search(path))


def is_product_url(url: str) -> bool:
    u = urlparse(url)
    path = u.path.lower()
    segs = [s for s in path.split("/") if s]
    if any(s in PRODUCT_SEGMENTS for s in segs) or path.endswith(SKIP_EXT):
        return True
    # 最后一段像商品编号：Best Buy 的 ….p、Target 的 A-12345678、纯数字编号（分类页 pcmcat….c 不算）
    if segs and (segs[-1].endswith(".p") or re.fullmatch(r"(?:a-)?\d{6,}", segs[-1])
                 or re.search(r"\.product\.|[-_.]\d{7,}(?:\.html?)?$", segs[-1])):   # Costco 的 ….product.123.html
        return True
    return bool(re.search(r"(?:^|&)(?:sku|skuid|productid|pid)=", u.query.lower()))


def _segments(url: str) -> list:
    return [s.lower() for s in urlparse(url).path.split("/") if s]


def _lang_of(seg: str) -> Optional[str]:
    """路径段是语言（de、zh、es-mx、zh_CN…）就返回语言代码，否则 None。"""
    s = seg.lower()
    if s in LANGS:
        return s
    if LOCALE_RE.match(s):
        a, b = re.split(r"[-_]", s)
        if a in LANGS or a == "en":
            return a
        if b in LANGS and a in COUNTRIES:      # ca-fr、us-es 这种“国家-语言”写法
            return b
    return None


def language_ok(url: str, region: str) -> bool:
    """只要英文页；加拿大还可以是法语页。前 3 个路径段里出现别的语言就不要。"""
    allowed = {"en", "fr"} if region == "CA" else {"en"}
    for seg in _segments(url)[:3]:
        lang = _lang_of(seg)
        if lang and lang not in allowed:
            return False
    return True


def is_irrelevant_url(url: str) -> bool:
    """法律条款、隐私、招聘、复活节、情人节之类和黑五无关的页面。"""
    return bool(IRRELEVANT_RE.search(urlparse(url).path))


def region_ok(url: str, region: str) -> bool:
    """加拿大：.ca 域名，或路径里有 /ca、/en-ca 之类；英文或法语页。
    美国：不能是 .ca 域名，也不能带别国的路径段；只要英文页。"""
    host = (urlparse(url).hostname or "").lower()
    segs = _segments(url)
    head = segs[:2]
    if not language_ok(url, region):
        return False
    if region == "CA":
        return host.endswith(".ca") or any(s == "ca" or s in CA_LOCALES or s in FRENCH for s in head)
    if host.endswith(".ca"):
        return False
    for s in head:
        if s in US_LOCALES or s == "us" or s == "en":
            continue
        if s in COUNTRIES or s in CA_LOCALES or (LOCALE_RE.match(s) and s not in US_LOCALES):
            return False
    return True


def _region_prefix(shop_url: str) -> str:
    """shop 链接路径里带地区标记时（/us、/ca/en、/en-ca…），候选页要以这一段开头。"""
    segs = [s for s in urlparse(shop_url).path.split("/") if s]
    out = []
    for s in segs:
        out.append(s)
        low = s.lower()
        if low in ("us", "ca") or low in CA_LOCALES or low in US_LOCALES:
            return "/" + "/".join(out)
    return ""


def pick_pages(urls: list, shop_url: str, region: str, max_pages: int = 4) -> list:
    """从 sitemap 的 URL 里挑本地区、非单品、有关键词的页面，按分数取前 max_pages 个。"""
    shop_host = (urlparse(shop_url).hostname or "").lower()
    prefix = _region_prefix(shop_url).lower()
    seen, scored = set(), []
    for u in urls:
        if not isinstance(u, str) or not u.lower().startswith(("http://", "https://")):
            continue
        u = u.split("#")[0]
        p = urlparse(u)
        if (p.hostname or "").lower() != shop_host or u in seen:
            continue
        seen.add(u)
        if prefix and not p.path.lower().startswith(prefix):
            continue
        if is_product_url(u) or is_irrelevant_url(u) or not region_ok(u, region):
            continue
        s = url_score(u)
        if s > 0:
            scored.append((-s, len(_segments(u)), len(u), u))
    scored.sort()
    return [(u, -neg) for neg, _, _, u in scored[:max_pages]]


# ---------- 发现阶段的请求：如实 UA、限速、限量 ----------
SKIP_SITEMAP_RE = re.compile(r"product|sku|pdp|(?<![a-z])items?(?![a-z])|image|video|review|store|location|inventory|"
                             r"question|brand|seller|author|tag", re.I)


class SiteClock:
    """同一网站两次请求（robots.txt、sitemap、页面都算）之间至少隔 delay 秒。线程安全：
    发现阶段（异步）、robots 检查（在线程里）、抓页面阶段共用同一个。reserve() 返回还要等几秒。"""

    def __init__(self, delay: float = 5.0, clock=time.monotonic):
        self.delay, self.clock = delay, clock
        self.next: dict = {}
        self.lock = threading.Lock()

    @staticmethod
    def key(host: str) -> str:
        """同一个网站只用一个键：小写、去掉末尾的点、去掉 www.（www.a.com 和 a.com 共用间隔）。"""
        h = (host or "").lower().rstrip(".")
        return h[4:] if h.startswith("www.") else h

    def reserve(self, host: str) -> float:
        host = self.key(host)
        with self.lock:
            now = self.clock()
            t = max(now, self.next.get(host, now))
            self.next[host] = t + self.delay
            return t - now


class _SyncPacer:
    """给 articles.RobotsCache 用的 pacer：读 robots.txt 前按 SiteClock 等够间隔。"""

    def __init__(self, clock: SiteClock, sleep=time.sleep):
        self.clock, self.sleep = clock, sleep

    def wait(self, host: str) -> None:
        w = self.clock.reserve(host)
        if w > 0:
            self.sleep(w)


class RobotsGate:
    """robots.txt 检查（发现和抓取两个阶段共用，在找页面之前建好）。check(url) -> (允许?, 原因)。
    读不到（5xx、超时、跳转太多）和不允许分开记原因；读 robots.txt 时遇到 429，这个网站记为已拒。"""

    def __init__(self, clock: SiteClock, refused: set, agent: str = "DealRadar", timeout: float = 15,
                 max_redirects: int = 3, client: Optional[httpx.Client] = None, sleep=time.sleep):
        self.client = client or httpx.Client(headers={"User-Agent": UA}, follow_redirects=False, timeout=timeout)
        self.cache = RobotsCache(self.client, agent, timeout, _SyncPacer(clock, sleep), refused, max_redirects)
        self.refused = refused
        self.locks: dict = {}
        self.guard = threading.Lock()

    def check(self, url: str) -> tuple:
        origin = RobotsCache._origin(url)
        with self.guard:
            lock = self.locks.setdefault(origin, threading.Lock())
        with lock:                                   # 同一网站的 robots.txt 只读一次
            if self.cache.allowed(url):
                return True, ""
            return False, "robots.txt 读不到" if self.cache.cache.get(origin) is None else "robots.txt 不允许"

    def __call__(self, url: str) -> tuple:
        return self.check(url)

    def close(self) -> None:
        self.client.close()


def _robots_result(r) -> tuple:
    """robots 检查函数可以返回 bool 或 (bool, 原因)。"""
    if isinstance(r, tuple):
        return bool(r[0]), (r[1] if len(r) > 1 else "") or ("" if r[0] else "robots.txt 不允许")
    return bool(r), "" if r else "robots.txt 不允许"


class PoliteTransport(httpx.AsyncBaseTransport):
    """给 AsyncUrlSeeder 用的 httpx 传输层（httpx 跟随重定向时每一跳也都经过这里）：
    - 除了 robots.txt 本身，每个请求发出前都核对 robots.txt；不允许或读不到 → 本地返回 404，记进 errors；
    - 同一网站的请求按 SiteClock 间隔（默认 5 秒），每个网站最多 max_requests 次；
    - 网站返回 401/403/429 → 记为已拒，之后对它的所有请求都在本地返回 404（不重试、不换方式）；
    - 名字像商品 / 图片 / 门店的子 sitemap 不去请求（本地 404）。"""

    def __init__(self, inner: Optional[httpx.AsyncBaseTransport] = None, clock: Optional[SiteClock] = None,
                 max_requests: int = 10, robots=None, refused: Optional[set] = None, sleep=asyncio.sleep):
        self.inner = inner or httpx.AsyncHTTPTransport()
        self.clock = clock or SiteClock(5.0)
        self.max_requests, self.robots, self.sleep = max_requests, robots, sleep
        self.refused = refused if refused is not None else set()
        self.count: Counter = Counter()
        self.skipped: Counter = Counter()
        self.status: dict = {}           # host -> 最近一次 robots.txt / sitemap 的状态码
        self.errors: dict = {}           # host -> 原因（robots、被拒）

    def close_host(self, host: str, reason: str = "") -> None:
        """不再请求这个网站（例如读 sitemap 超时后，防止残留任务继续请求）。"""
        self.count[host] = self.max_requests
        self.refused.add(host)
        if reason:
            self.errors.setdefault(host, reason)

    def _local(self, request, host) -> httpx.Response:
        self.skipped[host] += 1
        return httpx.Response(404, request=request)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        path = request.url.path
        if host in self.refused or self.count[host] >= self.max_requests:
            return self._local(request, host)
        if path != "/robots.txt" and SKIP_SITEMAP_RE.search(path.rsplit("/", 1)[-1]) and (
                "sitemap" in path.lower() or path.lower().endswith((".xml", ".gz"))):
            return self._local(request, host)
        if path != "/robots.txt" and self.robots is not None:
            ok, why = _robots_result(await asyncio.to_thread(self.robots, str(request.url)))
            if not ok:
                self.errors.setdefault(host, why)
                return self._local(request, host)
            if host in self.refused:                 # 读 robots.txt 时遇到 429
                return self._local(request, host)
        wait = self.clock.reserve(host)
        if wait > 0:
            await self.sleep(wait)
        if host in self.refused or self.count[host] >= self.max_requests:
            return self._local(request, host)
        self.count[host] += 1
        resp = await self.inner.handle_async_request(request)
        if resp.status_code in BLOCK_STATUS:
            self.refused.add(host)
            self.errors.setdefault(host, f"HTTP {resp.status_code}")
        if path == "/robots.txt" or "sitemap" in path.lower():
            self.status[host] = resp.status_code
        return resp

    async def aclose(self) -> None:
        await self.inner.aclose()


@dataclass
class DiscoverStats:
    sites: int = 0
    sitemap_pages: int = 0
    homepage_pages: int = 0
    manual_pages: int = 0
    sites_with_sitemap_pages: int = 0
    sitemap_failed: list = field(default_factory=list)    # [(brand, region, 原因)]
    refused: dict = field(default_factory=dict)            # host -> 找页面时被拒 / robots 不允许的原因

    def describe(self) -> str:
        return (f"{self.sites} 个品牌×地区：页面共 {self.sitemap_pages + self.homepage_pages + self.manual_pages} 个"
                f"（sitemap {self.sitemap_pages}、首页 {self.homepage_pages}、人工 {self.manual_pages}）；"
                f"{self.sites_with_sitemap_pages} 个站点从 sitemap 找到了页面，{len(self.sitemap_failed)} 个没找到；"
                f"读 sitemap 时被拒或 robots 不允许的网站 {len(self.refused)} 个")


def site_list(brands: dict) -> list:
    """[(brand_id, region, shop_url)]：每个品牌 × 有店的地区。"""
    return [(bid, region, url) for bid, b in brands.items() for region, url in (b.get("shop") or {}).items()
            if region in ("US", "CA")]


async def _seed_urls(seeder, host: str, timeout: float, max_urls: int) -> list:
    """AsyncUrlSeeder 只读 sitemap（不用 Common Crawl），不做 live check、不抓页面 head。"""
    from crawl4ai import SeedingConfig
    # force=False：24 小时内读过的 sitemap 用本地缓存（只再请求一次 sitemap 看有没有更新），少打扰网站
    cfg = SeedingConfig(source="sitemap", pattern="*", max_urls=max_urls, live_check=False, extract_head=False,
                        concurrency=4, hits_per_sec=2, force=False, cache_ttl_hours=24, verbose=False)
    rows = await seeder.urls(host, cfg)
    return [r.get("url") if isinstance(r, dict) else r for r in rows]


async def discover_async(brands: dict, settings: dict, seed=None, transport=None, robots=None,
                         clock: Optional[SiteClock] = None, refused: Optional[set] = None) -> tuple:
    """返回 (页面列表, DiscoverStats)。同一网站（host）只读一次 sitemap，不同网站最多同时 3 个。
    seed: async (host) -> [url]；不给就用 Crawl4AI 的 AsyncUrlSeeder（请求经过 PoliteTransport：如实 UA、
    每个请求都核对 robots、同站间隔 5 秒、被拒就停）。robots / clock / refused 和抓页面阶段共用。"""
    max_pages = int(settings.get("official_max_pages_per_site", 4))
    timeout = float(settings.get("official_discover_timeout_seconds", 180))
    max_urls = int(settings.get("official_discover_max_urls", -1))   # -1 = 不限（靠每个网站的请求上限控制）
    sites = site_list(brands)
    st = DiscoverStats(sites=len(sites))
    hosts = sorted({(urlparse(u).hostname or "").lower() for _, _, u in sites})
    polite = None
    client = None
    if seed is None:
        from crawl4ai import AsyncUrlSeeder
        polite = transport or PoliteTransport(
            clock=clock or SiteClock(float(settings.get("official_discover_delay_seconds", 5))),
            max_requests=int(settings.get("official_discover_max_requests", 10)), robots=robots, refused=refused)
        client = httpx.AsyncClient(headers={"User-Agent": UA}, timeout=20, transport=polite, follow_redirects=True)
        base = Path(settings.get("official_cache_dir") or Path.home() / ".cache" / "dealradar")
        seeder = AsyncUrlSeeder(client=client, base_directory=base, cache_root=base / "url_seeder")

        async def seed(host):
            return await _seed_urls(seeder, host, timeout, max_urls)
    by_host: dict = {}
    for bid, region, shop in sites:
        by_host.setdefault((urlparse(shop).hostname or "").lower(), []).append((bid, region, shop))
    picks: dict = {}         # (bid, region) -> [(url, score)]；URL 全表读完就丢掉，只留挑出的页面（省内存）
    counts: dict = {}
    errors: dict = {}
    sem = asyncio.Semaphore(int(settings.get("official_max_concurrent_sites", 3)))

    async def one(host):
        async with sem:
            urls: list = []
            try:
                urls = await asyncio.wait_for(seed(host), timeout)
            except asyncio.TimeoutError:
                errors[host] = "读 sitemap 超时"
                if polite is not None:
                    polite.close_host(host, "读 sitemap 超时")   # 残留的子 sitemap 任务也不能再请求
            except Exception as e:
                errors[host] = f"读 sitemap 出错 {type(e).__name__}"
            counts[host] = len(urls)
            for bid, region, shop in by_host[host]:
                picks[(bid, region)] = pick_pages(urls, shop, region, max_pages)
            log.info("sitemap %s：%d 个 URL%s", host, len(urls), f"（{errors[host]}）" if host in errors else "")
    try:
        await asyncio.gather(*(one(h) for h in hosts))
    finally:
        await _cleanup(client)
    if polite is not None:
        st.refused = dict(sorted(polite.errors.items()))
        for h, why in st.refused.items():
            log.info("找页面：%s %s", h, why)
    pages = []
    today = datetime.now(timezone.utc).date().isoformat()
    for bid, region, shop in sites:
        host = (urlparse(shop).hostname or "").lower()
        pages.append({"brand_id": bid, "region": region, "url": shop, "origin": "homepage", "discovered": today})
        st.homepage_pages += 1
        picked = picks.get((bid, region), [])
        for url, score in picked:
            pages.append({"brand_id": bid, "region": region, "url": url, "origin": "sitemap", "score": score,
                          "discovered": today})
        st.sitemap_pages += len(picked)
        if picked:
            st.sites_with_sitemap_pages += 1
        else:
            code = (polite.status if polite is not None else {}).get(host)
            why = errors.get(host) or (polite.errors.get(host) if polite is not None else None) or (
                f"sitemap 状态码 {code}" if code and code != 200 else
                                       ("sitemap 里没有关键词页面" if counts.get(host) else "没读到 sitemap"))
            st.sitemap_failed.append((bid, region, why))
    return pages, st


async def _cleanup(client) -> None:
    """超时的网站，seeder 里还有读到一半的子 sitemap 任务挂着：先全部取消（最多等 10 秒），再关连接。"""
    me = asyncio.current_task()
    rest = [t for t in asyncio.all_tasks() if t is not me and not t.done()]
    for t in rest:
        t.cancel()
    if rest:
        await asyncio.wait(rest, timeout=10)
    if client is not None:
        try:
            await asyncio.wait_for(client.aclose(), 10)
        except Exception:
            pass


# ---------- 页面清单文件 ----------
PAGES_HEADER = """# 官网监控的页面清单（S15，ADR-006）。
# origin: homepage = brands.yaml 里的 shop 首页；sitemap = 从 sitemap 按关键词找到的；manual = 人工加的。
# 运行 --discover（或清单超过 7 天）会重新生成 homepage / sitemap 条目，manual 条目原样保留。
# 可以手改：想长期监控某个页面，就加一条 origin: manual。
"""


def load_pages(path: Path) -> dict:
    if not path.exists():
        return {"discovered_at": None, "pages": []}
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    pages = [p for p in (doc.get("pages") or []) if isinstance(p, dict) and p.get("url")]
    for p in pages:
        if p.get("discovered") is not None:
            p["discovered"] = str(p["discovered"])
    return {"discovered_at": str(doc["discovered_at"]) if doc.get("discovered_at") else None, "pages": pages}


def merge_discovered(old_pages: list, new_pages: list) -> list:
    """新发现的 homepage / sitemap 条目替换旧的；manual 条目保留（URL 重复时以 manual 为准）。"""
    manual = [dict(p, origin="manual") for p in old_pages if p.get("origin") == "manual"]
    manual_urls = {p["url"] for p in manual}
    out = list(manual)
    seen = set(manual_urls)
    for p in new_pages:
        if p["url"] not in seen:
            out.append(p)
            seen.add(p["url"])
    order = {"homepage": 0, "sitemap": 1, "manual": 2}
    return sorted(out, key=lambda p: (p["brand_id"], p["region"], order.get(p.get("origin"), 3), p["url"]))


def save_pages(path: Path, pages: list, discovered_at: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = yaml.safe_dump({"discovered_at": discovered_at, "pages": pages}, allow_unicode=True, sort_keys=False,
                          width=200)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(PAGES_HEADER + body, encoding="utf-8")
    tmp.replace(path)


def needs_discovery(doc: dict, now: datetime, days: int = 7) -> bool:
    if not doc.get("pages") or not doc.get("discovered_at"):
        return True
    try:
        when = datetime.fromisoformat(doc["discovered_at"])
    except ValueError:
        return True
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return now - when > timedelta(days=days)


# ---------- 2. 每天抓 ----------
ACTIVITY_RE = re.compile(r"black\s*friday|cyber\s*(?:monday|week)|boxing\s*(?:day|week)|doorbuster|door\s*boo|"
                         r"early\s+access|holiday\s+(?:sale|savings|deals|event)|\bsale\b|\bdeals?\b|\bsavings?\b|"
                         r"%\s*off|\bpercent\s+off\b|members?\b.{0,20}\b(?:early|exclusive)", re.I)
BLOCK_MARKERS = re.compile(r"access denied|captcha|just a moment|attention required|cf-browser-verification|"
                           r"pardon our interruption|verify you are (?:a )?human|are you a robot|"
                           r"request unsuccessful|unusual traffic|bot detection|px-captcha|"
                           r"enable javascript and cookies to continue", re.I)
BLOCK_STATUS = (401, 403, 429)
ERR_BLOCK_RE = re.compile(r"anti-?bot|captcha|access denied|perimeterx|cloudflare|challenge", re.I)


class _KeywordMatcher:
    """给 articles.make_excerpt 用的“品牌匹配器”：这里匹配的是活动关键词。"""
    rules = [("activity", ACTIVITY_RE, False)]


_MD_LINK = re.compile(r"!\[[^\]]*\]\([^)]*\)|\[([^\]]*)\]\([^)]*\)")


def markdown_paragraphs(md: str) -> list:
    """markdown → 段落（去掉图片、把链接换成文字、去掉标题符号；空行和单独的行都切开）。"""
    text = _MD_LINK.sub(lambda m: m.group(1) or "", md or "")
    out = []
    for line in text.splitlines():
        line = re.sub(r"^\s*(?:#{1,6}|[*\-+]|\d+\.)\s+", "", line)
        line = re.sub(r"[*_`]{1,3}", "", line)
        line = " ".join(line.split())
        if len(line) >= 3 and re.search(r"[A-Za-z]", line):
            out.append(line)
    return out


def page_excerpt(md: str, max_chars: int = 2500) -> str:
    """命中活动关键词或日期的段落，加前后各 1 段，总长 ≤ max_chars。"""
    return make_excerpt(markdown_paragraphs(md), _KeywordMatcher(), max_chars)


def sha256(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _markdown_of(result) -> str:
    md = getattr(result, "markdown", None)
    if md is None:
        return ""
    if isinstance(md, str):
        return md
    return getattr(md, "raw_markdown", None) or getattr(md, "fit_markdown", None) or ""


def classify(result) -> tuple:
    """(状态, 原因)：ok / blocked / error。"""
    status = getattr(result, "status_code", None)
    err = getattr(result, "error_message", "") or ""
    if "robots" in err.lower():
        return "blocked", "robots.txt 不允许"
    if ERR_BLOCK_RE.search(err):          # Crawl4AI 自己认出的拦截（PerimeterX、Cloudflare 等）；不重试
        return "blocked", f"拦截（{err[:80]}）"
    if status in BLOCK_STATUS:
        return "blocked", f"HTTP {status}"
    md = _markdown_of(result)
    head = (md[:3000] + " " + str((getattr(result, "metadata", None) or {}).get("title") or "")) if md else ""
    if md and len(md) < 6000 and BLOCK_MARKERS.search(head):
        return "blocked", f"拦截页（{BLOCK_MARKERS.search(head).group(0)}）"
    if not getattr(result, "success", False):
        return "error", (err[:120] or f"HTTP {status}")
    if status and status >= 400:
        return "error", f"HTTP {status}"
    if not md.strip():
        return "error", "页面没有文字"
    return "ok", ""


# ---------- 浏览器：不隐藏自动化标记 ----------
# crawl4ai 默认启动参数里写死了 --disable-blink-features=AutomationControlled（隐藏 navigator.webdriver，
# 让自动化浏览器看起来像真人），违反“不伪造指纹”的规定，这里从所有启动路径里删掉；
# --disable-infobars 只影响本机界面上的“受自动化软件控制”提示，一并删掉，保持如实。
EVASION_FLAGS = ("--disable-infobars",)
EVASION_BLINK_FEATURES = ("AutomationControlled",)


def strip_evasion_flags(args: list) -> list:
    out = []
    for a in args or []:
        if a in EVASION_FLAGS:
            continue
        if a.startswith("--disable-blink-features="):
            feats = [f for f in a.split("=", 1)[1].split(",") if f and f not in EVASION_BLINK_FEATURES]
            if not feats:
                continue
            a = "--disable-blink-features=" + ",".join(feats)
        out.append(a)
    return out


def patch_crawl4ai() -> None:
    """把 crawl4ai 两个拼启动参数的方法包一层，返回前删掉规避参数（可重复调用）。
    - ManagedBrowser.build_browser_flags：受管浏览器（CDP）和 persistent context 路径
    - BrowserManager._build_browser_args：普通 playwright.chromium.launch 路径（我们默认走这条）
    legacy/crawler_strategy.py（Selenium 旧实现）我们不 import、也不会走到。"""
    from crawl4ai import browser_manager as bm
    if getattr(bm, "_dealradar_patched", False):
        return
    orig_flags = bm.ManagedBrowser.build_browser_flags
    orig_args = bm.BrowserManager._build_browser_args

    def build_browser_flags(config):
        return strip_evasion_flags(orig_flags(config))

    def _build_browser_args(self):
        d = orig_args(self)
        d["args"] = strip_evasion_flags(d.get("args") or [])
        return d
    bm.ManagedBrowser.build_browser_flags = staticmethod(build_browser_flags)
    bm.BrowserManager._build_browser_args = _build_browser_args
    bm._dealradar_patched = True


def run_config():
    """唯一的抓取设置（主力已验证）：如实 UA、查 robots、不用缓存、30 秒超时；所有规避功能保持关闭。"""
    from crawl4ai import CacheMode, CrawlerRunConfig
    return CrawlerRunConfig(check_robots_txt=True, cache_mode=CacheMode.BYPASS, page_timeout=30000,
                            max_retries=0, magic=False, simulate_user=False, override_navigator=False,
                            verbose=False)


def browser_config():
    """如实 UA；enable_stealth、user_agent_mode（随机 UA）、init_scripts、extra_args、代理全部保持默认关闭。"""
    patch_crawl4ai()
    from crawl4ai import BrowserConfig
    return BrowserConfig(headless=True, user_agent=UA, verbose=False)


@dataclass
class PageResult:
    brand_id: str
    region: str
    url: str
    status: str                  # ok / empty / blocked / error / skipped
    reason: str = ""
    excerpt: str = ""
    hash: str = ""
    title: str = ""
    final_url: str = ""


def _streak(streak: tuple, status: str) -> tuple:
    """同一个网站连续 2 次出错或连续 2 次 empty → 本次停掉这个网站。返回 (新的计数, 停止原因或 None)。"""
    if status not in ("error", "empty"):
        return (None, 0), None
    n = streak[1] + 1 if streak[0] == status else 1
    if n >= 2:
        return (status, n), f"同网站连续 {n} 次{'出错' if status == 'error' else '没有活动文字'}，本次停止"
    return (status, n), None


async def crawl_pages(pages: list, crawler, settings: dict, robots_ok, sleep=asyncio.sleep, config=None,
                      clock: Optional[SiteClock] = None, refused: Optional[set] = None) -> list:
    """同一网站串行，两次请求（含 robots.txt）间隔 ≥ site_delay 秒；不同网站最多同时 max_sites 个。
    每页：先核对 robots → 等够间隔 → 抓（总超时 page_timeout 秒，不重试）→ 跳转了就再核对最终地址的 robots。
    被拒的网站（含找页面阶段已拒的），后面的页面本次跳过。"""
    clock = clock or SiteClock(float(settings.get("official_site_delay_seconds", 5)))
    refused = refused if refused is not None else set()
    total_timeout = float(settings.get("official_page_timeout_seconds", 90))
    max_chars = int(settings.get("excerpt_max_chars", 2500))
    sem = asyncio.Semaphore(int(settings.get("official_max_concurrent_sites", 3)))
    by_host: dict = {}
    for p in pages:
        by_host.setdefault(host_of(p["url"]), []).append(p)
    results: list = []

    async def site(host, plist):
        async with sem:
            blocked_reason = None
            streak = (None, 0)          # 连续出错 / 连续 empty 的次数
            stop_reason = None
            for p in plist:
                url = p["url"]
                net_host = (urlparse(url).hostname or "").lower()
                if blocked_reason or net_host in refused:
                    results.append(PageResult(p["brand_id"], p["region"], url, "skipped",
                                              f"同网站已被拒：{blocked_reason or '找页面时已被拒（HTTP 429）'}"))
                    continue
                if stop_reason:
                    results.append(PageResult(p["brand_id"], p["region"], url, "stopped", stop_reason))
                    continue
                ok, why = _robots_result(await asyncio.to_thread(robots_ok, url))
                if not ok:
                    results.append(PageResult(p["brand_id"], p["region"], url, "blocked", why))
                    log.info("官网 跳过（%s）：%s", why, url)
                    continue
                wait = clock.reserve(net_host)
                if wait > 0:
                    await sleep(wait)
                try:
                    r = await asyncio.wait_for(crawler.arun(url, config=config), total_timeout)
                except asyncio.TimeoutError:      # 不重试
                    pr = PageResult(p["brand_id"], p["region"], url, "error", f"超时（{total_timeout:.0f} 秒）")
                    results.append(pr)
                    streak, stop_reason = _streak(streak, pr.status)
                    continue
                except Exception as e:            # 不重试
                    pr = PageResult(p["brand_id"], p["region"], url, "error", type(e).__name__)
                    results.append(pr)
                    streak, stop_reason = _streak(streak, pr.status)
                    continue
                status, reason = classify(r)
                final = getattr(r, "redirected_url", None) or url
                pr = PageResult(p["brand_id"], p["region"], url, status, reason,
                                title=str((getattr(r, "metadata", None) or {}).get("title") or "")[:200],
                                final_url=final)
                if status == "ok" and final.rstrip("/") != url.rstrip("/"):
                    ok2, why2 = _robots_result(await asyncio.to_thread(robots_ok, final))
                    if not ok2:                   # 跳转后的地址 robots 不允许：丢掉内容，不交给 AI
                        pr.status, pr.reason = "blocked", f"跳转后{why2}（{final[:80]}）"
                if pr.status == "ok":
                    md = _markdown_of(r)
                    pr.excerpt = page_excerpt(md, max_chars)
                    if pr.excerpt:
                        pr.hash = sha256(pr.excerpt)
                    else:
                        pr.status, pr.reason = "empty", f"无活动文字（正文 {len(md)} 字）"
                elif pr.status == "blocked":
                    blocked_reason = pr.reason
                    refused.add(net_host)
                log.info("官网 %s：%s %s", pr.status, url, pr.reason)
                results.append(pr)
                streak, stop_reason = _streak(streak, pr.status)

    await asyncio.gather(*(site(h, pl) for h, pl in by_host.items()))
    order = {p["url"]: i for i, p in enumerate(pages)}
    return sorted(results, key=lambda r: order.get(r.url, 0))


# ---------- 状态、需人工查看清单 ----------
def update_state(state: dict, results: list, now_iso: str) -> list:
    """把抓取结果记进状态（只存哈希、时间、状态），返回“内容有变化、要交给 AI”的结果。"""
    pages = state.setdefault("pages", {})
    changed = []
    for r in results:
        s = pages.setdefault(r.url, {})
        s.update(brand_id=r.brand_id, region=r.region, last_attempt=now_iso)
        if r.status == "skipped":
            s.update(status="blocked", reason=r.reason)
            continue
        if r.status == "stopped":          # 连续出错 / empty 后本次没抓：不算被拒，不进人工清单
            s.update(status="stopped", reason=r.reason)
            continue
        s.update(status=r.status, reason=r.reason or None)
        if r.status not in ("ok", "empty"):
            continue
        s["last_crawl"] = now_iso
        if r.status == "empty":       # 没有活动文字：不覆盖已有的哈希，也不交给 AI
            continue
        if s.get("hash") != r.hash:
            changed.append(r)
    return changed


def mark_processed(state: dict, results: list, now_iso: str) -> None:
    """AI 成功处理过的页面，才记下新哈希（失败的下次再试）。"""
    for r in results:
        s = state.setdefault("pages", {}).setdefault(r.url, {})
        if s.get("hash") != r.hash:
            s["last_changed"] = now_iso
        s["hash"] = r.hash
        s["last_ai"] = now_iso


def manual_review(state: dict) -> list:
    """需人工查看：被拒（blocked）和抓到了但没有活动文字（empty）的页面。"""
    out = []
    for url, s in sorted((state.get("pages") or {}).items()):
        if s.get("status") in ("blocked", "empty"):
            out.append({"brand_id": s.get("brand_id"), "region": s.get("region"), "url": url,
                        "reason": s.get("reason"), "last_attempt": s.get("last_attempt")})
    return out


def build_items(changed: list, brands: dict, now_iso: str) -> list:
    """变化了的页面 → 给 extract 的条目。来源是品牌官网，地区就是页面所属的地区。"""
    items = []
    for r in changed:
        b = brands.get(r.brand_id) or {}
        host = host_of(r.final_url or r.url)
        items.append({
            "source": "official", "feed": "official", "region_hint": r.region, "brand_hints": [r.brand_id],
            "title": r.title or f"{b.get('name', r.brand_id)} ({host})",
            "summary": "", "excerpt": r.excerpt, "link": r.url, "guid": r.url, "published": now_iso,
            "source_name": f"{b.get('name', r.brand_id)} official website ({host})",
            "source_url": f"https://{host}", "official_region": r.region, "page_hash": r.hash,
        })
    return items


@dataclass
class OfficialRun:
    pages: int = 0
    sites: int = 0
    results: list = field(default_factory=list)
    changed: list = field(default_factory=list)
    items: list = field(default_factory=list)
    discover: Optional[DiscoverStats] = None
    seconds: float = 0.0

    def site_status(self) -> dict:
        """host -> ok（全部成功）/ partial（部分被拒：有页面成功，但中途被拒）/ blocked（全部被拒）/
        empty（打开了但都没有活动文字）/ error。中途被拒不算成功。"""
        seen: dict = {}
        for r in self.results:
            seen.setdefault(host_of(r.url), set()).add(r.status)
        out = {}
        for h, st in seen.items():
            refused = bool(st & {"blocked", "skipped"})
            if "ok" in st:
                out[h] = "partial" if refused else "ok"
            elif refused:
                out[h] = "blocked"
            else:
                out[h] = "empty" if "empty" in st else "error"
        return out

    def describe(self) -> str:
        sites = self.site_status()
        c = Counter(sites.values())
        blocked = sorted(h for h, v in sites.items() if v == "blocked")
        partial = sorted(h for h, v in sites.items() if v == "partial")
        return (f"页面 {self.pages} 个（{len(sites)} 个网站）→ 全部成功 {c.get('ok', 0)}、部分被拒 {c.get('partial', 0)}、"
                f"全部被拒 {c.get('blocked', 0)}、没有活动文字 {c.get('empty', 0)}、出错 {c.get('error', 0)}；"
                f"内容有变化、交给 AI 的页面 {len(self.changed)} 个；用时 {self.seconds:.0f} 秒"
                + (f"\n  全部被拒：{', '.join(blocked)}" if blocked else "")
                + (f"\n  部分被拒：{', '.join(partial)}" if partial else ""))


def default_crawler_factory():
    patch_crawl4ai()
    from crawl4ai import AsyncWebCrawler
    return AsyncWebCrawler(config=browser_config())


async def _crawl_with(pages: list, settings: dict, crawler_factory, robots_ok, sleep, clock=None, refused=None):
    crawler = crawler_factory()
    enter = getattr(crawler, "__aenter__", None)
    if enter:
        await enter()
    try:
        cfg = run_config() if crawler_factory is default_crawler_factory else None
        return await crawl_pages(pages, crawler, settings, robots_ok, sleep, cfg, clock, refused)
    finally:
        exit_ = getattr(crawler, "__aexit__", None)
        if exit_:
            await exit_(None, None, None)


def run_official(brands: dict, settings: dict, pages_path: Path, state_path: Path, review_path: Path,
                 now: datetime, discover: bool = False, crawler_factory=None, seed=None,
                 robots_ok: Optional[Callable[[str], bool]] = None, sleep=asyncio.sleep,
                 write: bool = True) -> tuple:
    """抓官网 → 更新状态 → 返回 (OfficialRun, state)。交给 AI、合并由 run.py 做；
    AI 处理完再调用 finish_official 记下哈希、写状态和人工清单。"""
    t0 = time.monotonic()
    now_iso = now.isoformat(timespec="seconds")
    out = OfficialRun()
    # 发现和抓取两个阶段共用：同站间隔、robots 检查、已拒网站
    clock = SiteClock(max(float(settings.get("official_site_delay_seconds", 5)),
                          float(settings.get("official_discover_delay_seconds", 5))))
    refused: set = set()
    gate = None
    if robots_ok is None:
        gate = RobotsGate(clock, refused, settings.get("robots_agent", "DealRadar"),
                          max_redirects=int(settings.get("article_max_redirects", 3)))
        robots_ok = gate
    doc = load_pages(pages_path)
    if discover or needs_discovery(doc, now, int(settings.get("official_rediscover_days", 7))):
        log.info("官网监控：重新找页面（sitemap + 首页）")
        new_pages, out.discover = asyncio.run(discover_async(brands, settings, seed, None, robots_ok, clock, refused))
        doc = {"discovered_at": now_iso, "pages": merge_discovered(doc["pages"], new_pages)}
        out.discover.manual_pages = sum(1 for p in doc["pages"] if p.get("origin") == "manual")
        if write:
            save_pages(pages_path, doc["pages"], now_iso)
        log.info("  %s", out.discover.describe())
    pages = [p for p in doc["pages"] if p.get("brand_id") in brands and p.get("region") in ("US", "CA")]
    out.pages = len(pages)
    out.sites = len({host_of(p["url"]) for p in pages})
    # 抓页面阶段另记“已拒网站”：403 两个阶段分开记；找页面时返回 429（请求太多）的网站，本轮不再抓
    crawl_refused = {h for h, why in (out.discover.refused if out.discover else {}).items() if why == "HTTP 429"}
    try:
        out.results = asyncio.run(_crawl_with(pages, settings, crawler_factory or default_crawler_factory,
                                              robots_ok, sleep, clock, crawl_refused))
    finally:
        if gate is not None:
            gate.close()
    state = load_json(state_path, {"pages": {}})
    out.changed = update_state(state, out.results, now_iso)
    out.items = build_items(out.changed, brands, now_iso)
    out.seconds = time.monotonic() - t0
    if write:
        save_json(state_path, state)
        save_json(review_path, manual_review(state))
    return out, state


def finish_official(state: dict, run: OfficialRun, processed_items: list, now_iso: str, state_path: Path,
                    review_path: Path, write: bool = True) -> None:
    """AI 成功处理过的页面才记下新哈希；写状态文件和需人工查看清单。"""
    done = {it["link"] for it in processed_items}
    mark_processed(state, [r for r in run.changed if r.url in done], now_iso)
    if write:
        save_json(state_path, state)
        save_json(review_path, manual_review(state))
