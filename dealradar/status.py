"""状态判定（方案 §2.3，由代码做，不交给 AI）。

1. 原文引句必须真的出现在那条输入的标题、摘要或正文截取段落（excerpt）里，否则整条丢掉。
2. 来源级别：品牌官方域名 → official；权威媒体白名单 → media；其余 → other。
3. 字段逐个核对引句（S3 v3）：start / end 必须在引句里以“月名 + 日”、“M/D”、ISO 日期，
   或 key_dates 里的节日名（且日期就是那个节日）出现；discount 里的数字必须都出现在引句里。
   对不上的字段置为空，然后再定状态。
4. 一条证据算“已官宣”（confirmed）要同时满足：来源是 official 或 media，并且这条证据本身
   给出了开始日期或折扣。只有标题、没有日期也没有折扣的，一律算传闻（rumored）。
5. 判成 CA 要有加拿大证据：.ca 域名、加拿大媒体、文中出现 Canada / Canadian / C$ / CAD，
   或者品牌只在加拿大有店；没有就丢弃。
6. judge 之后（finalize_candidates，S3 v4）：
   - 类型校正：类型不是 other，但引句和活动名里都没有这个类型的关键词，就改成 other；
   - other 的促销身份 sale_key：活动名规范化（去掉品牌名、年份和 sale/event/deals 这类词），
     没有名字就用开始日期；名字和开始日期都没有的 other 丢弃。
"""
from __future__ import annotations

import re
import unicodedata
from datetime import date
from typing import Optional
from urllib.parse import urlparse

from .config import key_dates

LEVEL = {"other": 1, "media": 2, "official": 3}

_PUNCT = str.maketrans({
    "’": "'", "‘": "'", "‛": "'", "“": '"', "”": '"', "„": '"',
    "–": "-", "—": "-", "‐": "-", "‑": "-", "…": "...", "\xa0": " ",
})
_EDGE = " \t\n\"'.,;:!?()[]"


def normalize(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "").translate(_PUNCT).lower()
    return re.sub(r"\s+", " ", s).strip()


def quote_location(quote: str, item: dict) -> Optional[str]:
    """引句出现在哪个字段：title / summary / excerpt（正文截取段落）；都没有返回 None。"""
    q = normalize(quote).strip(_EDGE)
    if len(q) < 4:
        return None
    return next((k for k in ("title", "summary", "excerpt") if q in normalize(item.get(k) or "")), None)


def quote_found(quote: str, item: dict) -> bool:
    return quote_location(quote, item) is not None


def host_of(url: Optional[str]) -> str:
    if not url:
        return ""
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def domain_matches(host: str, domain: str) -> bool:
    domain = domain.lower().strip()
    if domain.startswith("www."):
        domain = domain[4:]
    return bool(host) and (host == domain or host.endswith("." + domain))


def source_host(item: dict) -> str:
    """Google News 的链接是 news.google.com 跳转，真正来源取 RSS 条目的 source href。"""
    if item.get("source") == "gnews":
        return host_of(item.get("source_url"))
    return host_of(item.get("source_url")) or host_of(item.get("link"))


def source_level(item: dict, brand: dict, trusted: list) -> str:
    host = source_host(item)
    if any(domain_matches(host, d) for d in brand.get("domains", [])):
        return "official"
    if any(domain_matches(host, d) for d in trusted):
        return "media"
    return "other"


def status_of(level: str, ev: Optional[dict] = None) -> str:
    """来源级别够、并且这条证据本身有开始日期或折扣，才算 confirmed。"""
    ev = ev or {}
    if level in ("official", "media") and (ev.get("start") or ev.get("discount")):
        return "confirmed"
    return "rumored"


# ---------- 字段核对 ----------
_MONTHS = ["jan(?:uary)?", "feb(?:ruary)?", "mar(?:ch)?", "apr(?:il)?", "may", "june?", "july?", "aug(?:ust)?",
           "sep(?:t(?:ember)?)?", "oct(?:ober)?", "nov(?:ember)?", "dec(?:ember)?"]
_HOLIDAYS = {"US Thanksgiving": ("thanksgiving",), "Black Friday": ("black friday",),
             "Cyber Monday": ("cyber monday",), "Boxing Day": ("boxing day",)}
_NUM_RE = re.compile(r"\d+(?:[.,]\d+)*")


def date_in_quote(value: str, quote: str) -> bool:
    """日期 value（YYYY-MM-DD）有没有在引句里写出来。"""
    try:
        d = date.fromisoformat(value)
    except (TypeError, ValueError):
        return False
    q = normalize(quote)
    if value in q:
        return True
    mon, day = _MONTHS[d.month - 1], rf"0?{d.day}(?:st|nd|rd|th)?"
    pats = [
        rf"\b{mon}\b\.?,?\s*{day}(?!\d)",                         # Nov. 20 / November 20th
        rf"(?<![\d/]){day}\s+(?:of\s+)?{mon}\b",                   # 20 November / 20th of Nov
        rf"(?<![\d/])0?{d.month}/{day}(?![\d])",                    # 11/20、11/20/2026
        # 范围的后一天只写了日子：Nov. 20-27 / Nov 20 to 27
        rf"\b{mon}\b\.?\s*\d{{1,2}}(?:st|nd|rd|th)?\s*(?:-|to|through|thru|until)\s*{day}(?!\d)",
    ]
    if any(re.search(p, q) for p in pats):
        return True
    for name, hd in key_dates(d.year).items():
        if hd == d and any(alias in q for alias in _HOLIDAYS.get(name, ())):
            return True
    return False


def discount_in_quote(discount: str, quote: str) -> bool:
    """折扣里的数字是不是都出现在引句里（没有数字的折扣不核对）。"""
    nums = [n.replace(",", "") for n in _NUM_RE.findall(discount or "")]
    if not nums:
        return True
    have = {n.replace(",", "") for n in _NUM_RE.findall(normalize(quote))}
    return all(n in have for n in nums)


CA_TEXT_RE = re.compile(r"\bCanada\b|\bCanadian\b|\bC\$|\bCA\$|\bCAD\b")


def ca_evidence(item: dict, brand: dict, canadian_media=(), brand_names_with_canada=()) -> bool:
    """判成 CA 的依据：.ca 域名、加拿大媒体、文中提到 Canada / Canadian / C$ / CAD，或品牌只在加拿大有店；
    官网监控的页面本身属于加拿大站（official_region == "CA"）也算。"""
    if set((brand.get("shop") or {}).keys()) == {"CA"} or item.get("official_region") == "CA":
        return True
    hosts = [h for h in (source_host(item), host_of(item.get("link"))) if h]
    if any(h.endswith(".ca") for h in hosts):
        return True
    if any(domain_matches(h, d) for h in hosts for d in canadian_media):
        return True
    text = " ".join(item.get(k) or "" for k in ("title", "summary", "excerpt"))
    for name in brand_names_with_canada:     # “Canada Goose”“Canadian Tire” 这类品牌名不算
        text = re.sub(re.escape(name), " ", text, flags=re.I)
    return bool(CA_TEXT_RE.search(text))


def _count(dropped: dict, why: str) -> None:
    dropped[why] = dropped.get(why, 0) + 1


def judge(pairs: list, brands: dict, trusted: list, dropped: dict, canadian_media=()) -> list:
    """输入 [(item, event)]：引句核对不上的丢掉；日期、折扣逐个核对引句，对不上的置空；
    判成 CA 却没有加拿大证据的丢掉；其余补上 status 和来源信息，返回候选事件列表。"""
    canada_names = [n for b in brands.values() for n in [b["name"], *b.get("aliases", [])]
                    if re.search(r"canad", str(n), re.I)]
    out = []
    for item, ev in pairs:
        where = quote_location(ev["quote"], item)
        if where is None:
            _count(dropped, "引句在原文里找不到")
            continue
        ev = dict(ev, ai_start=ev.get("start"), ai_end=ev.get("end"))   # AI 原始给的日期（调试存档用）
        for f in ("start", "end"):
            if ev.get(f) and not date_in_quote(ev[f], ev["quote"]):
                ev[f] = None
                _count(dropped, f"置空：{f} 不在引句里")
        if ev.get("discount") and not discount_in_quote(ev["discount"], ev["quote"]):
            ev["discount"] = None
            _count(dropped, "置空：折扣数字不在引句里")
        brand = brands[ev["brand_id"]]
        if ev.get("region") == "CA" and not ca_evidence(item, brand, canadian_media, canada_names):
            _count(dropped, "判成 CA 但没有加拿大证据")
            continue
        level = source_level(item, brand, trusted)
        status = status_of(level, ev)
        out.append(dict(ev, status=status, source={
            "title": item.get("title") or item.get("link"),
            "url": item.get("link"),
            "domain": source_host(item),
            "name": item.get("source_name"),
            "level": level,
            "status": status,            # 这条证据自己的级别（事件取所有证据里最高的）
            "start": ev.get("start"),
            "end": ev.get("end"),
            "discount": ev.get("discount"),
            "quote": ev["quote"],
            "quote_in": where,               # 引句出自标题 / 摘要 / 正文（excerpt）
            "published": item.get("published"),
        }))
    return out


# ---------- judge 之后：类型校正、other 的促销身份 ----------
TYPE_KEYWORDS = {
    "early_bf": re.compile(r"\bearly\b.{0,30}\bblack\s*friday\b|\bpre-?\s*black\s*friday\b|"
                           r"\b(?:before|ahead\s+of)\s+black\s*friday\b|\bearly\s+(?:access\s+)?bf\b|"
                           r"\bblack\s*friday\b.{0,20}\bearly\b|"                       # Black Friday came early
                           r"\bdon['’]?t\s+wait\s+(?:for|until)\s+black\s*friday\b", re.I),
    "black_friday": re.compile(r"\bblack\s*friday\b", re.I),
    "cyber_monday": re.compile(r"\bcyber\s*monday\b", re.I),
    "cyber_week": re.compile(r"\bcyber\s*(?:week|weekend|5|five)\b|\bbfcm\b|\bblack\s*friday\b.{0,40}\bcyber\s*monday\b",
                             re.I),
    "boxing_day": re.compile(r"\bboxing\s*(?:day|week)\b", re.I),
}
_SALE_STOPWORDS = {"sale", "sales", "event", "events", "deal", "deals"}


def type_supported(event_type: str, quote: str, event_name: Optional[str], extra: Optional[str] = None) -> bool:
    """引句、活动名（引句出自标题时再加上标题）里有没有这个类型的关键词。"""
    pat = TYPE_KEYWORDS.get(event_type)
    if pat is None:
        return True
    return any(pat.search(t or "") for t in (quote, event_name, extra))


def _ascii_lower(t: str) -> str:
    """Lancôme → lancome；弯引号先换成直的，免得被当成非 ASCII 去掉。"""
    t = t.replace("’", "'").replace("‘", "'")
    return unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode().lower()


def sale_key(event_name: Optional[str], brand_words=(), year: Optional[int] = None) -> str:
    """活动名 → 促销身份：小写，按非字母数字切词，去掉品牌名里的词、年份和 sale/event/deals，
    再去复数（长度 >3、以 s 结尾但不以 ss/us/is 结尾的词去掉末尾的 s），最后连起来。
    例如 "Best Buy Door BOOsters Sale 2026" → "doorbooster"。没有剩下的词返回空串。"""
    t = _ascii_lower(event_name or "")
    t = re.sub(r"['’]s\b", "", t)                       # 所有格 Costco's → costco
    drop = {w for w in brand_words} | _SALE_STOPWORDS | ({str(year)} if year else set())
    words = [_singular(w) for w in re.split(r"[^0-9a-z]+", t) if w and w not in drop]
    return "".join(w for w in words if w not in drop)


def _singular(w: str) -> str:
    if len(w) > 3 and w.endswith("s") and not w.endswith(("ss", "us", "is")):
        return w[:-1]
    return w


def brand_words(brand: dict) -> set:
    out = set()
    for n in [brand.get("name", ""), *brand.get("aliases", [])]:
        t = re.sub(r"['’]s\b", "", _ascii_lower(str(n)))
        out |= {w for w in re.split(r"[^0-9a-z]+", t) if w}
    return out


def finalize_candidates(cands: list, brands: dict, dropped: dict, rejected: Optional[list] = None) -> list:
    """类型校正 + 给 other 定促销身份；没有名字也没有开始日期的 other 丢弃（放进 rejected，调试存档用）。
    类型校正看引句和活动名；引句出自标题（quote_in == "title"）时也看整条标题。人工记录不做类型校正。
    返回留下的候选。"""
    out = []
    for c in cands:
        src = c.get("source") or {}
        title = src.get("title") if src.get("quote_in") == "title" else None
        if (not c.get("manual") and c["event_type"] != "other"      # 人工记录（manual）跳过类型校正
                and not type_supported(c["event_type"], c.get("quote"), c.get("event_name"), title)):
            _count(dropped, f"类型改成 other（{c['event_type']} 的关键词不在引句和活动名里）")
            c = dict(c, event_type="other", ai_event_type=c["event_type"])
        if c["event_type"] == "other":
            key = sale_key(c.get("event_name"), brand_words(brands.get(c["brand_id"], {})), c.get("year"))
            if key:
                c = dict(c, sale_key=key, sale_key_kind="name")
            elif c.get("start"):
                c = dict(c, sale_key=c["start"], sale_key_kind="start")
            else:
                _count(dropped, "other 没有名字也没有开始日期")
                if rejected is not None:
                    rejected.append(dict(c, event_id=None, kept=False,
                                         dropped_reason="other 没有名字也没有开始日期"))
                continue
        out.append(c)
    return out
