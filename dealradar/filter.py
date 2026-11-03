"""去重和预筛：丢掉旧闻、已处理过的条目、非北美的条目、和打折无关的条目，每次最多留 max_items 条交给 AI。

同一篇文章出现在 Google News 和 Bing 两边时，按规范化后的标题认出来，只留一条；
有 Bing 版本的用 Bing 版本（它有原文链接，可以抓正文）。

条数上限（apply_cap）可以晚一步再套：每日流程先在全部候选里抓正文，抓到正文的条目优先入选，
剩下的名额再按新的优先给只有标题的条目。
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional

from .sources import NEWS_SOURCES
from .status import source_host

EVENT_RE = re.compile(
    r"black\s*friday|cyber|boxing\s*day|\bsales?\b|\bdeals?\b|%\s*off|percent\s+off|"
    r"doorbuster|\bbfcm\b|\bdiscount|\bpromo|\bsavings?\b|clearance|price\s+drop",
    re.I,
)
_QUOTES = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"', "\xa0": " "})

# 非北美：英镑 / 欧元 / 澳元 / 纽元金额（"CA$" 前面有字母 C，不算）
FOREIGN_MONEY_RE = re.compile(r"[£€]\s?\d|\d\s?[£€]|(?<![A-Za-z])(?:AU|A|NZ)\$\s?\d")
FOREIGN_TLDS = (".uk", ".au", ".de", ".ie", ".nz", ".in")   # 含 .co.uk、.com.au
# 标题里的国家名。不收 British（British Columbia）、England（New England）
FOREIGN_TITLE_RE = re.compile(r"\bUK\b|\bU\.K\.|\b(?:Britain|Australia|Australian|Aussie|New Zealand|Ireland|India|Germany)\b")


def clean(s: str) -> str:
    return (s or "").translate(_QUOTES)


class BrandMatcher:
    """在标题和摘要里找品牌名或别名。

    单个单词、首字母大写的名字（Gap、Coach、Target……）要求原文首字母也大写，
    避免把普通单词 "gap"、"target" 当成品牌。
    """

    def __init__(self, brands: dict):
        self.rules = []
        for bid, b in brands.items():
            for name in [b["name"], *b.get("aliases", [])]:
                name = clean(str(name))
                pat = re.compile(r"(?<![\w])" + re.escape(name) + r"(?![\w])", re.I)
                need_cap = " " not in name and name[:1].isupper()
                self.rules.append((bid, pat, need_cap))

    def match(self, text: str) -> list:
        text = clean(text)
        found = []
        for bid, pat, need_cap in self.rules:
            if bid in found:
                continue
            for m in pat.finditer(text):
                if not need_cap or m.group(0)[:1].isupper():
                    found.append(bid)
                    break
        return found


def item_keys(item: dict) -> list:
    """guid、链接，加上去重时并进来的同一篇文章的其他版本（alt_keys）。"""
    keys = []
    for k in (item.get("guid"), item.get("link"), *item.get("alt_keys", [])):
        if k and k not in keys:
            keys.append(k)
    return keys


def title_key(item: dict) -> Optional[str]:
    """新闻条目的规范化标题；Google News 标题末尾的“ - 来源名”去掉。太短的不用来去重。"""
    if item.get("source") not in NEWS_SOURCES:
        return None
    t = clean(item.get("title") or "").strip()
    if item.get("source") == "gnews" and " - " in t:
        name = (item.get("source_name") or "").strip()
        t = t[: -len(name) - 3] if name and t.endswith(" - " + name) else t.rpartition(" - ")[0]
    t = unicodedata.normalize("NFKC", t).lower()
    t = re.sub(r"[\W_]+", " ", t).strip()
    return t if len(t) >= 15 else None


def non_na_reason(item: dict) -> Optional[str]:
    """看起来是英国、澳洲、欧洲、印度等地的内容就返回原因，否则 None。"""
    title, summary = clean(item.get("title") or ""), clean(item.get("summary") or "")
    if FOREIGN_MONEY_RE.search(title) or FOREIGN_MONEY_RE.search(summary):
        return "£/€/A$ 金额"
    host = source_host(item)
    if host and host.endswith(FOREIGN_TLDS):
        return "非北美域名"
    if FOREIGN_TITLE_RE.search(title):
        return "标题带外国国名"
    return None


_BING_FIELDS = ("source", "feed", "title", "link", "guid", "source_name", "source_url", "published")


def _absorb(hit: dict, it: dict, by_title: bool) -> None:
    """把同一篇文章的另一个版本 it 并进 hit：合并地区和品牌提示；Bing 版本优先。"""
    hints = hit["region_hint"].split("/")
    for r in it["region_hint"].split("/"):
        if r not in hints:
            hints.append(r)
    hit["region_hint"] = "/".join(hints)
    hit["brand_hints"] = list(dict.fromkeys(hit["brand_hints"] + list(it.get("brand_hints", []))))
    alt = list(hit.get("alt_keys", []))
    if by_title and it.get("source") == "bing" and hit.get("source") != "bing":
        alt += item_keys(hit)
        for k in _BING_FIELDS:
            hit[k] = it.get(k)
        if it.get("summary"):
            hit["summary"] = it["summary"]
    else:
        alt += item_keys(it)
    own = {hit.get("guid"), hit.get("link")}
    hit["alt_keys"] = [k for k in dict.fromkeys(alt) if k and k not in own]


def is_seen(item: dict, seen: dict) -> bool:
    return any(k in seen for k in item_keys(item))


def mark_seen(seen: dict, items: list, today: str) -> None:
    for it in items:
        for k in item_keys(it):
            seen.setdefault(k, today)


def prune_seen(seen: dict, today: date, keep_days: int) -> dict:
    limit = (today - timedelta(days=keep_days)).isoformat()
    return {k: v for k, v in seen.items() if v >= limit}


def published_date(item: dict) -> Optional[date]:
    p = item.get("published")
    if not p:
        return None
    try:
        return datetime.fromisoformat(p).date()
    except ValueError:
        return None


@dataclass
class FilterStats:
    total: int = 0
    old: int = 0
    no_date: int = 0
    dup_in_run: int = 0
    dup_title: int = 0       # dup_in_run 里按标题认出的（Google News 和 Bing 的同一篇）
    dup_seen: int = 0
    non_na: int = 0
    non_na_reasons: dict = field(default_factory=dict)
    no_keyword: int = 0
    no_brand: int = 0
    over_cap: int = 0
    kept: int = 0
    kept_with_excerpt: int = 0
    kept_by_source: dict = field(default_factory=dict)

    def describe(self) -> str:
        return (f"共 {self.total} 条 → 旧闻 {self.old}、本次重复 {self.dup_in_run}（其中标题相同 {self.dup_title}）、"
                f"以前处理过 {self.dup_seen}、非北美 {self.non_na}、"
                f"无打折关键词 {self.no_keyword}、deal 源未命中品牌 {self.no_brand}、超过上限 {self.over_cap} "
                f"→ 保留 {self.kept} 条（其中带正文 {self.kept_with_excerpt} 条；无发布日期 {self.no_date} 条，已保留）")


def apply_cap(items: list, max_items: Optional[int], st: FilterStats) -> list:
    """带正文（excerpt）的优先，其余新的优先；最多 max_items 条（None 表示不限）。顺带填好统计。"""
    out = sorted(items, key=lambda x: x.get("published") or "", reverse=True)
    out.sort(key=lambda x: not x.get("excerpt"))     # 稳定排序：带正文的在前，各自仍是新的在前
    if max_items is not None:
        st.over_cap = max(0, len(out) - max_items)
        out = out[:max_items]
    else:
        st.over_cap = 0
    st.kept = len(out)
    st.kept_with_excerpt = sum(1 for it in out if it.get("excerpt"))
    st.kept_by_source = {}
    for it in out:
        st.kept_by_source[it["source"]] = st.kept_by_source.get(it["source"], 0) + 1
    return out


def prefilter(items: list, seen: dict, matcher: BrandMatcher, cutoff: date,
              max_items: Optional[int] = 300) -> tuple:
    """max_items=None 时先不套上限（之后再调用 apply_cap）。"""
    st = FilterStats(total=len(items))

    # 1) 旧闻
    fresh = []
    for it in items:
        d = published_date(it)
        if d is None:
            st.no_date += 1
        elif d < cutoff:
            st.old += 1
            continue
        fresh.append(it)

    # 2) 本次运行内重复（同一篇新闻出现在多个查询里，或 Google News 和 Bing 都有）：合并地区提示和品牌提示
    #    按标题只认“两边”的重复（Google News 一条 + Bing 一条）；同一个搜索引擎里标题相同、
    #    链接不同的（不同网站转载）仍算不同来源
    unique, index, by_title = [], {}, {}
    for it in fresh:
        hit = next((index[k] for k in item_keys(it) if k in index), None)
        tk = title_key(it)
        same_title = (hit is None and tk is not None and tk in by_title
                      and it["source"] not in by_title[tk][1])
        if same_title:
            hit, engines = by_title[tk]
            engines.add(it["source"])
        if hit is not None:
            st.dup_in_run += 1
            st.dup_title += int(same_title)
            _absorb(hit, it, same_title)
            for k in item_keys(hit):
                index[k] = hit
            continue
        it = dict(it, brand_hints=list(it.get("brand_hints", [])))
        for k in item_keys(it):
            index[k] = it
        if tk and tk not in by_title:
            by_title[tk] = (it, {it["source"]})
        unique.append(it)

    # 3) 以前处理过的  4) 非北美  5) 关键词预筛
    kept = []
    for it in unique:
        if is_seen(it, seen):
            st.dup_seen += 1
            continue
        why = non_na_reason(it)
        if why:
            st.non_na += 1
            st.non_na_reasons[why] = st.non_na_reasons.get(why, 0) + 1
            continue
        text = f"{it['title']}\n{it['summary']}"
        if not EVENT_RE.search(clean(text)):
            st.no_keyword += 1
            continue
        found = matcher.match(text)
        if it["source"] not in NEWS_SOURCES and not found:
            st.no_brand += 1
            continue
        it["brand_hints"] = list(dict.fromkeys(it["brand_hints"] + found))
        kept.append(it)

    # 6) 带正文的优先、其余新的优先，最多 max_items 条
    return apply_cap(kept, max_items, st), st
