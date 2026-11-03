"""抓正文（S3 v2）：预筛之后，给最相关的新闻抓原文，只截取提到品牌和日期的几段，交给 AI。

- 只抓有原文链接的新闻条目（Bing）；Google News 的链接是加密跳转，deal 网站 RSS 本身有摘要，都不抓
- 按相关度排序，每次最多 max_articles_per_run 篇（robots.txt 不允许的、article_skip_domains 里的不占名额）
- 合规（S3 v3）：UA 用 article_user_agent（DealRadar/0.1，和核对 robots.txt 用的是同一个名字）；
  不自动跟跳转，自己跟最多 article_max_redirects 跳，每一跳都核对 robots.txt（按网站缓存）；
  同一网站每次运行最多 article_max_per_site 篇，同一网站两次请求间隔 ≥ article_site_delay_seconds，
  任意两次请求间隔 ≥ article_delay_seconds；遇到 402/403/429，本次运行跳过这个网站，不重试
- 单篇超时 article_timeout_seconds
- 正文（够长的段落）少于 article_min_words 个词算失败（例如靠 JS 加载的页面）
- 截出的段落里至少要有一段像正文、并且提到品牌名或别名的（见 has_brand_paragraph），否则算失败，
  免得侧栏、相关文章列表凑够词数被当成功
- 截出来的文本放在条目的 excerpt 字段，AI 输入和引句核对都用它
- 原文只在内存里用，不写进任何文件（仓库是公开的）；抓失败只计数，不影响整体运行
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Callable, Optional
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx

from .filter import BrandMatcher, clean
from .sources import NEWS_SOURCES
from .status import domain_matches, host_of, normalize

log = logging.getLogger(__name__)

MAX_BYTES = 3_000_000          # 单个页面最多读这么多字节
REDIRECT_STATUS = (301, 302, 303, 307, 308)
BLOCK_STATUS = (402, 403, 429)  # 网站拒绝：本次运行不再请求这个网站
PARA_MAX = 800                 # 单个段落太长时，只截取命中位置附近这么多字符
COUNT_MIN_WORDS = 6            # 统计正文词数时，只算至少这么多词的段落（菜单、按钮之类不算）

# ---------- 相关度 ----------
ACTIVITY_RE = re.compile(r"black\s*friday|cyber\s*(?:monday|week)|boxing\s*(?:day|week)|\bbfcm\b", re.I)
_MONTH = (r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|"
          r"Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)")
SCHEDULE_RE = re.compile(
    r"\b(?:dates?|schedule|timeline|calendar|starts?|starting|begins?|kicks?\s+off|announce[sd]?|"
    r"reveals?|revealed|when\s+(?:is|does|do|will)|is\s+live|ends?)\b"
    rf"|\b{_MONTH}\b\.?\s+\d{{1,2}}\b", re.I)

# 段落里的日期：Nov / November / Dec / 具体日期 / 星期几（Black Friday、Cyber Monday 里的不算）
DATE_RE = re.compile(
    r"\b(?:Nov(?:ember)?|Dec(?:ember)?)\b"
    rf"|\b{_MONTH}\b\.?\s+\d{{1,2}}(?:st|nd|rd|th)?\b"
    rf"|\b\d{{1,2}}(?:st|nd|rd|th)?\s+(?:of\s+)?{_MONTH}\b"
    r"|\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b|\b\d{4}-\d{2}-\d{2}\b"
    r"|(?<!black )(?<!cyber )\b(?:mon|tues|wednes|thurs|fri|satur|sun)day\b",
    re.I)


def eligible(item: dict, skip_domains: list) -> bool:
    """有原文链接的新闻条目才抓：不是 news.google.com，也不是 deal 网站（它们的 RSS 有摘要）。"""
    if item.get("source") not in NEWS_SOURCES:
        return False
    link = item.get("link") or ""
    if not link.lower().startswith(("http://", "https://")):
        return False
    host = host_of(link)
    if not host or domain_matches(host, "news.google.com"):
        return False
    return not any(domain_matches(host, d) for d in skip_domains)


def relevance(item: dict, matcher: BrandMatcher) -> int:
    """标题同时含品牌名和活动词 +2；标题像日期、时间表、开始、宣布 +1。"""
    title = clean(item.get("title") or "")
    score = 0
    if matcher.match(title) and ACTIVITY_RE.search(title):
        score += 2
    if SCHEDULE_RE.search(title):
        score += 1
    return score


def rank(items: list, matcher: BrandMatcher, skip_domains: list) -> list:
    """可以抓正文的条目，按相关度从高到低；同分的新的在前。"""
    cands = [it for it in items if eligible(it, skip_domains)]
    cands.sort(key=lambda it: it.get("published") or "", reverse=True)
    cands.sort(key=lambda it: relevance(it, matcher), reverse=True)   # 稳定排序：同分保持新的在前
    return cands


# ---------- 请求节奏 ----------
class Pacer:
    """任意两次请求间隔 ≥ delay 秒；同一网站两次请求间隔 ≥ site_delay 秒。"""

    def __init__(self, delay: float, site_delay: float, sleep: Callable[[float], None],
                 clock: Callable[[], float]):
        self.delay, self.site_delay, self.sleep, self.clock = delay, site_delay, sleep, clock
        self.last_any: Optional[float] = None
        self.last_site: dict = {}

    def ready_at(self, host: str) -> float:
        t = self.last_site.get(host)
        return float("-inf") if t is None else t + self.site_delay

    def wait(self, host: str) -> None:
        now = self.clock()
        target = max(now, self.ready_at(host),
                     float("-inf") if self.last_any is None else self.last_any + self.delay)
        if target > now:
            self.sleep(target - now)
        t = self.clock()
        self.last_any = t
        self.last_site[host] = t


def _http_url(url: str) -> bool:
    return urlparse(url or "").scheme in ("http", "https")


# ---------- robots.txt ----------
class RobotsCache:
    """按网站（scheme + host）缓存 robots.txt。读不到（5xx、超时、跳转太多）就当不允许；429 时这个网站本次跳过。"""

    def __init__(self, client: httpx.Client, agent: str, timeout: float, pacer: Optional[Pacer] = None,
                 blocked: Optional[set] = None, max_redirects: int = 3):
        self.client, self.agent, self.timeout = client, agent, timeout
        self.pacer, self.max_redirects = pacer, max_redirects
        self.blocked = blocked if blocked is not None else set()
        self.cache: dict = {}

    @staticmethod
    def _origin(url: str) -> str:
        u = urlparse(url)
        return f"{u.scheme}://{u.netloc}"

    def known(self, url: str) -> bool:
        return self._origin(url) in self.cache

    def allowed(self, url: str) -> bool:
        origin = self._origin(url)
        if origin not in self.cache:
            self.cache[origin] = self._load(origin)
        rp = self.cache[origin]
        return rp is not None and rp.can_fetch(self.agent, url)

    def _load(self, origin: str) -> Optional[RobotFileParser]:
        url = origin + "/robots.txt"
        try:
            for hop in range(self.max_redirects + 1):
                if self.pacer:
                    self.pacer.wait(host_of(url))
                r = self.client.get(url, timeout=self.timeout)
                if r.status_code in REDIRECT_STATUS:
                    url = urljoin(url, r.headers.get("location", ""))
                    if not _http_url(url) or hop == self.max_redirects:
                        return None
                    continue
                break
        except Exception as e:
            log.info("robots.txt 读取失败 %s：%s", origin, type(e).__name__)
            return None
        rp = RobotFileParser()
        if r.status_code in (401, 403):
            rp.disallow_all = True
        elif r.status_code == 429:
            self.blocked.add(host_of(origin))
            return None
        elif 400 <= r.status_code < 500:
            rp.allow_all = True          # 没有 robots.txt：不限制
        elif r.status_code != 200:
            return None
        else:
            rp.parse(r.text.splitlines())
        return rp


# ---------- 正文提取 ----------
SKIP_TAGS = {"script", "style", "noscript", "template", "svg", "nav", "footer", "aside", "iframe",
             "button", "select"}
BLOCK_TAGS = {"p", "div", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "br", "tr", "table", "section",
              "article", "blockquote", "header", "main", "figure", "figcaption", "dd", "dt", "pre", "hr"}


class _ArticleText(HTMLParser):
    """去掉 script、style、nav、footer 等，按块级标签切成段落；记下每段是否在 <article> 里。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skip = 0
        self.article = 0
        self.buf: list = []
        self.paras: list = []    # [(text, in_article)]

    def _flush(self) -> None:
        text = " ".join("".join(self.buf).replace("\xa0", " ").split())
        self.buf = []
        if text:
            self.paras.append((text, self.article > 0))

    def handle_starttag(self, tag, attrs):
        if tag in SKIP_TAGS:
            self.skip += 1
            return
        if self.skip:
            return
        if tag in BLOCK_TAGS:
            self._flush()
        if tag == "article":
            self.article += 1
        elif tag in ("td", "th"):
            self.buf.append(" | ")

    def handle_endtag(self, tag):
        if tag in SKIP_TAGS:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        if tag in BLOCK_TAGS:
            self._flush()
        if tag == "article":
            self.article = max(0, self.article - 1)

    def handle_data(self, data):
        if not self.skip:
            self.buf.append(data)

    def close(self):
        super().close()
        self._flush()


def _count_words(paras: list) -> int:
    return sum(n for n in (len(p.split()) for p in paras) if n >= COUNT_MIN_WORDS)


def extract_paragraphs(html: str, min_words: int = 150) -> tuple:
    """返回 (段落列表, 正文词数)。<article> 里的内容够长就只用它，否则用整页。"""
    p = _ArticleText()
    try:
        p.feed(html)
        p.close()
    except Exception:            # 极少数坏页面：用已经解析出的部分
        p._flush()
    in_article = [t for t, a in p.paras if a]
    if _count_words(in_article) >= min_words:
        paras = in_article
    else:
        paras = [t for t, _ in p.paras]
    return paras, _count_words(paras)


def _window(text: str, pos: int, size: int = PARA_MAX) -> str:
    """长段落只截取 pos 附近 size 个字符，在空格处断开。"""
    if len(text) <= size:
        return text
    start = max(0, min(pos - size // 3, len(text) - size))
    end = start + size
    if start > 0:
        sp = text.find(" ", start)
        start = sp + 1 if 0 <= sp < start + 40 else start
    if end < len(text):
        sp = text.rfind(" ", start, end)
        end = sp if sp > start + size // 2 else end
    return text[start:end].strip()


def _first_brand(matcher: BrandMatcher, text: str) -> Optional[int]:
    """品牌名在段落里第一次出现的位置；没有就 None。（clean 只做等长替换，位置不变）"""
    text = clean(text)
    best = None
    for _bid, pat, need_cap in matcher.rules:
        for m in pat.finditer(text):
            if not need_cap or m.group(0)[:1].isupper():
                best = m.start() if best is None else min(best, m.start())
                break
    return best


def make_excerpt(paras: list, brand_matcher: BrandMatcher, max_chars: int = 2500) -> str:
    """含品牌名或别名的段落 + 含日期的段落 + 它们前后各 1 段；总长不超过 max_chars。

    超长时按优先级取：品牌和日期都有 > 只有品牌 > 品牌段落的前后段 > 只有日期 > 日期段落的前后段；
    选中的段落按原文顺序排列，不相邻的段落之间用 “...” 隔开。重复出现的段落只算第一次。
    """
    first_seen: dict = {}
    dup = set()
    for i, t in enumerate(paras):
        if t in first_seen:
            dup.add(i)
        else:
            first_seen[t] = i
    prio: dict = {}
    pos: dict = {}
    for i, t in enumerate(paras):
        if i in dup:
            continue
        b = _first_brand(brand_matcher, t)
        d = DATE_RE.search(t)
        if b is not None or d:
            prio[i] = 0 if (b is not None and d) else (1 if b is not None else 3)
            pos[i] = min(x for x in (b, d.start() if d else None) if x is not None)
    for i, p in list(prio.items()):
        for j in (i - 1, i + 1):
            if 0 <= j < len(paras) and j not in dup and j not in prio:
                prio[j] = 2 if p <= 1 else 4
            elif j in prio and prio[j] in (2, 4) and p <= 1:
                prio[j] = 2
    chosen, used = [], 0
    for i in sorted(prio, key=lambda k: (prio[k], k)):
        text = _window(paras[i], pos.get(i, 0))
        cost = len(text) + 5          # 加上段落之间的分隔符
        if used + cost > max_chars:
            continue
        chosen.append((i, text))
        used += cost
    chosen.sort()
    out, last = [], None
    for i, text in chosen:
        if last is not None:
            out.append("\n" if i == last + 1 else "\n...\n")
        out.append(text)
        last = i
    return "".join(out)[:max_chars]


SENTENCE_END_RE = re.compile(r"[.!?…](?:[\"'’”)\]]*)(?:\s|$)")


def has_brand_paragraph(excerpt: str, matcher: BrandMatcher, title: str = "") -> bool:
    """截出的段落里有没有一段像正文、又提到品牌名或别名的。

    像正文：至少 COUNT_MIN_WORDS 个词、带句末标点，并且不是这篇文章的标题本身
    （侧栏、相关文章列表里的标题一般没有句号）。
    """
    t = normalize(title)
    for line in (excerpt or "").split("\n"):
        if line == "..." or len(line.split()) < COUNT_MIN_WORDS or not SENTENCE_END_RE.search(line):
            continue
        if t and normalize(line).strip(" .!?") == t.strip(" .!?"):
            continue
        if _first_brand(matcher, line) is not None:
            return True
    return False


# ---------- 联网抓取 ----------
class ArticleError(Exception):
    def __init__(self, kind: str, detail: str = ""):
        super().__init__(f"{kind} {detail}".strip())
        self.kind = kind


def _charset(content_type: str) -> str:
    m = re.search(r"charset=([\w.-]+)", content_type or "", re.I)
    return m.group(1) if m else "utf-8"


def fetch_html(client: httpx.Client, url: str, timeout: float, robots: Optional[RobotsCache] = None,
               pacer: Optional[Pacer] = None, blocked: Optional[set] = None, max_redirects: int = 3,
               clock: Callable[[], float] = time.monotonic) -> str:
    """下载一个页面。不自动跟跳转：自己跟最多 max_redirects 跳，每一跳都核对 robots.txt、遵守请求节奏。
    402/403/429 → 把网站记进 blocked（本次运行不再请求）；非 200、不是 HTML、单篇超过 timeout 秒都算失败。"""
    blocked = blocked if blocked is not None else set()
    for hop in range(max_redirects + 1):
        host = host_of(url)
        if host in blocked:
            raise ArticleError("blocked", "网站本次已拒绝过")
        if hop and robots is not None and not robots.allowed(url):
            raise ArticleError("robots", "跳转目标 robots.txt 不允许")
        if host in blocked:      # 读 robots.txt 时遇到 429
            raise ArticleError("blocked", "网站本次已拒绝过")
        if pacer:
            pacer.wait(host)
        start = clock()
        try:
            with client.stream("GET", url, timeout=timeout) as r:
                if r.status_code in REDIRECT_STATUS:
                    nxt = urljoin(url, r.headers.get("location", ""))
                    if not _http_url(nxt) or nxt == url:
                        raise ArticleError("http", f"跳转地址不对（HTTP {r.status_code}）")
                    url = nxt
                    continue
                if r.status_code in BLOCK_STATUS:
                    blocked.add(host)
                    raise ArticleError("blocked", f"HTTP {r.status_code}")
                if r.status_code != 200:
                    raise ArticleError("http", f"HTTP {r.status_code}")
                ctype = r.headers.get("content-type", "")
                if ctype and "html" not in ctype.lower():
                    raise ArticleError("http", f"不是网页（{ctype.split(';')[0]}）")
                chunks, size = [], 0
                for chunk in r.iter_bytes():
                    chunks.append(chunk)
                    size += len(chunk)
                    if size >= MAX_BYTES:
                        break
                    if clock() - start > timeout:
                        raise ArticleError("error", "超时")
                return b"".join(chunks).decode(_charset(ctype), errors="replace")
        except ArticleError:
            raise
        except LookupError:
            raise ArticleError("error", "编码不认识")
        except httpx.HTTPError as e:
            raise ArticleError("error", type(e).__name__)
    raise ArticleError("http", f"跳转超过 {max_redirects} 次")


@dataclass
class ArticleStats:
    candidates: int = 0       # 有原文链接、可以抓的新闻条数
    skipped_domain: int = 0   # article_skip_domains 里的（不占名额）
    robots_blocked: int = 0   # robots.txt 不允许或读不到（不占名额）
    site_cap: int = 0         # 同一网站已经抓满 article_max_per_site 篇，跳过（不占名额）
    site_blocked: int = 0     # 网站本次返回过 402/403/429，同网站后面的条目跳过（不占名额）
    attempted: int = 0        # 实际下载了几篇
    ok: int = 0               # 拿到正文并截出相关段落
    failed: dict = field(default_factory=dict)   # 失败原因 → 篇数

    def fail(self, kind: str) -> None:
        self.failed[kind] = self.failed.get(kind, 0) + 1

    @property
    def success_rate(self) -> float:
        return self.ok / self.attempted if self.attempted else 0.0

    def describe(self) -> str:
        names = {"http": "HTTP 错误", "blocked": "网站拒绝（402/403/429）", "robots": "跳转目标 robots 不允许",
                 "error": "网络/超时", "too_short": "正文太短", "no_relevant": "没有相关段落",
                 "no_brand": "没有提到品牌的正文段落"}
        fails = "、".join(f"{names.get(k, k)} {v}" for k, v in sorted(self.failed.items())) or "无"
        return (f"可抓 {self.candidates} 篇（跳过 JS 页面域名 {self.skipped_domain}）→ robots.txt 不允许 "
                f"{self.robots_blocked}、同网站超过上限 {self.site_cap}、网站拒绝后跳过 {self.site_blocked}、"
                f"尝试 {self.attempted} 篇、成功 {self.ok} 篇（成功率 {self.success_rate:.0%}；失败：{fails}）")


def fetch_articles(items: list, brands: dict, settings: dict, client: Optional[httpx.Client] = None,
                   sleep: Callable[[float], None] = time.sleep, transport=None,
                   clock: Callable[[], float] = time.monotonic) -> ArticleStats:
    """给最相关的条目抓正文，把截取的段落写进 item["excerpt"]。只改内存里的条目，不写文件。

    按相关度从高到低处理；某个网站还在冷却（同一网站间隔）时，先处理后面其他网站的条目，不干等。
    """
    limit = int(settings.get("max_articles_per_run", 60))
    timeout = float(settings.get("article_timeout_seconds", 15))
    delay = float(settings.get("article_delay_seconds", 1))
    site_delay = float(settings.get("article_site_delay_seconds", 5))
    per_site = int(settings.get("article_max_per_site", 3))
    max_redirects = int(settings.get("article_max_redirects", 3))
    min_words = int(settings.get("article_min_words", 150))
    max_chars = int(settings.get("excerpt_max_chars", 2500))
    skip_domains = [d.lower() for d in settings.get("article_skip_domains") or []]
    agent = settings.get("robots_agent", "DealRadar")
    ua = settings.get("article_user_agent") or f"{agent}/0.1"

    all_matcher = BrandMatcher(brands)
    st = ArticleStats()
    news = [it for it in items if it.get("source") in NEWS_SOURCES and host_of(it.get("link"))
            and not domain_matches(host_of(it.get("link")), "news.google.com")]
    st.skipped_domain = sum(1 for it in news if not eligible(it, skip_domains))
    pending = rank(items, all_matcher, skip_domains)
    st.candidates = len(pending)
    if not pending or limit <= 0:
        return st

    own = client is None
    if own:
        client = httpx.Client(headers={"User-Agent": ua}, follow_redirects=False, timeout=timeout,
                              transport=transport)
    pacer = Pacer(delay, site_delay, sleep, clock)
    blocked: set = set()
    robots = RobotsCache(client, agent, timeout, pacer, blocked, max_redirects)
    picked: dict = {}
    matchers: dict = {}
    try:
        while pending and st.attempted < limit:
            # 挑排在最前、网站不在冷却中的一条；网站被拒或已抓满的直接去掉
            now, chosen, earliest, i = clock(), None, None, 0
            while i < len(pending):
                host = host_of(pending[i]["link"])
                if host in blocked:
                    pending.pop(i)
                    st.site_blocked += 1
                    continue
                if picked.get(host, 0) >= per_site:
                    pending.pop(i)
                    st.site_cap += 1
                    continue
                ready = pacer.ready_at(host)
                if ready <= now:
                    chosen = i
                    break
                earliest = ready if earliest is None else min(earliest, ready)
                i += 1
            if chosen is None:
                if earliest is None:
                    break
                sleep(max(0.0, earliest - now))
                continue
            it = pending[chosen]
            url, host = it["link"], host_of(it["link"])
            if not robots.known(url):
                robots.allowed(url)      # 先读 robots.txt；这个网站要隔 site_delay 秒才能再请求
                continue
            pending.pop(chosen)
            if not robots.allowed(url):
                st.robots_blocked += 1
                log.info("正文 跳过（robots.txt）：%s", host)
                continue
            picked[host] = picked.get(host, 0) + 1
            st.attempted += 1
            try:
                html = fetch_html(client, url, timeout, robots, pacer, blocked, max_redirects, clock)
                paras, words = extract_paragraphs(html, min_words)
                if words < min_words:
                    raise ArticleError("too_short", f"{words} 词")
                hints = tuple(b for b in it.get("brand_hints") or [] if b in brands)
                if hints not in matchers:
                    matchers[hints] = BrandMatcher({b: brands[b] for b in hints}) if hints else all_matcher
                excerpt = make_excerpt(paras, matchers[hints], max_chars)
                if not excerpt:
                    raise ArticleError("no_relevant")
                if not has_brand_paragraph(excerpt, matchers[hints], it.get("title") or ""):
                    raise ArticleError("no_brand")
            except ArticleError as e:
                st.fail(e.kind)
                log.info("正文 %d 失败（%s）：%s", st.attempted, e, host)
                continue
            except Exception as e:   # 单篇出任何问题都不能拖垮整体
                st.fail("error")
                log.info("正文 %d 失败（%s）：%s", st.attempted, type(e).__name__, host)
                continue
            it["excerpt"] = excerpt
            st.ok += 1
            log.info("正文 %d 成功：%s，%d 词 → 截取 %d 字", st.attempted, host, words, len(excerpt))
    finally:
        if own:
            client.close()
    return st
