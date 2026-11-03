"""把候选事件并入 data/sales.json，套用人工纠错，算出变更清单（给 S4 推送用）。

- 合并键 = brand_id + region + event_type + year；region=unknown 自成一组，不和 US/CA 合并：
  unknown 的证据只并进 unknown 事件，不会升级 US / CA 的事件。
- event_type=other 是兜底类型，不同促销会落进来，所以 other 的合并键再加促销身份 sale_key
  （活动名规范化，例如 DoorBOOsters → doorboosters；没有名字就用开始日期，见 status.finalize_candidates）。
- 只增不删：某个来源今天失败或没数据，已有事件原样保留。
- 来源列表去重；每条来源记着自己的级别（level）、证据状态（status）和它给出的日期、折扣。
- 一个事件最多保留 max_sources 个来源：已官宣证据 > 带日期 > 官网 > 权威媒体 > 较新。
  排不进去的证据直接忽略：不记来源，也不改事件的任何字段。
- 状态只升不降：保留下来的来源里有 confirmed 证据就升为 confirmed；已经是 confirmed 的保持不变，
  只有 config/overrides.yaml 能把它降级。
- 日期只看最终保留下来的来源（每次合并后重新算），按日期值分组投票：依次比最高来源级别、
  支持这个值的不同 URL 数、最新发布时间。有另一个值和胜出值同级、同票时，写进 date_conflict。
  所以低级来源改不了高级来源定下的日期；原来是空的，任何来源都可以补上。
  重算前先核对每条来源：日期不在它自己的引句里的置空（并写日志）。
- 同一来源（同一 URL）又来一条证据：引句相同才补空字段；引句不同就把
  (start, end, discount, quote, quote_in, status) 当一整组比优先级，高的整组替换，不拼接。
- 没有名字的 other（sale_key 是开始日期）：同品牌、同地区、同年份的某个非 other 事件开始日期相同、
  结束日期相同或有一方为空，就并进那个事件（合并时先看一次，合并完再把已有的 other 事件扫一遍）。
  有名字的 other（例如 DoorBOOsters）一般不并；只有起止日期都有、并且和某个事件完全相同时才折叠
  （防重复，S3 v5）：other 并进非 other，无名 other 并进有名 other。
- 人工记录（config/manual_events.yaml，来源带 manual=true）：来源级别和官网同级，
  但在日期投票、来源排序、折扣和活动名上排在同级的 AI 来源前面。优先级：overrides > manual > AI。
- 折扣、活动名：只有同级或更高级的来源才能改已有的值（官网 > 权威媒体 > 其他）；原来是空的，任何来源都可以补上。
- sales.json 存的是 AI 合并结果（不含人工纠错）；对外输出（data.js、变更清单）
  一律先套 config/overrides.yaml，所以 AI 永远盖不过人工纠错。
"""
from __future__ import annotations

import copy
import logging
from datetime import date, datetime
from typing import Optional

from .status import LEVEL, date_in_quote, normalize, sale_key, status_of

log = logging.getLogger(__name__)

FIELDS = ("event_name", "start", "end", "discount")
DATE_FIELDS = ("start", "end")              # 由保留下来的来源重新算
TEXT_FIELDS = ("event_name", "discount")    # 按来源级别逐条更新
OVERRIDE_FIELDS = ("event_name", "start", "end", "discount", "status")


def event_id(brand_id: str, region: str, event_type: str, year: int, key: Optional[str] = None) -> str:
    """非 other：品牌-地区-类型-年份；other 再加促销身份 sale_key。"""
    base = f"{brand_id}-{region}-{event_type}-{year}"
    return f"{base}-{key}" if event_type == "other" and key else base


def candidate_key(c: dict) -> tuple:
    """候选的 (sale_key, 种类)。正常由 status.finalize_candidates 给出；没给时这里按同样规则补（不去品牌名）。"""
    if c["event_type"] != "other":
        return None, None
    if c.get("sale_key"):
        return c["sale_key"], c.get("sale_key_kind") or "name"
    k = sale_key(c.get("event_name"), {c["brand_id"]}, c.get("year"))
    if k:
        return k, "name"
    if c.get("start"):
        return c["start"], "start"
    return None, None


def _same_source(a: dict, b: dict) -> bool:
    if a.get("url") and a.get("url") == b.get("url"):
        return True
    return (a.get("domain") or "") == (b.get("domain") or "") and \
        normalize(a.get("title") or "") == normalize(b.get("title") or "") != ""


def _source_rank(s: dict) -> tuple:
    """（是否人工记录，是否 confirmed，有没有日期，来源级别，发布时间），越大越优先。"""
    return (bool(s.get("manual")), s.get("status") == "confirmed", bool(s.get("start") or s.get("end")),
            LEVEL.get(s.get("level"), 0), s.get("published") or "")


def _field_level(src: dict) -> int:
    """折扣、活动名比级别时用：人工记录比同级（官网）的 AI 来源高一档。"""
    return LEVEL.get(src.get("level"), 0) + (1 if src.get("manual") else 0)


def trim_sources(sources: list, max_sources: int) -> list:
    """超过上限时按优先级留下 max_sources 个，保持原来的先后顺序。"""
    if max_sources <= 0 or len(sources) <= max_sources:
        return sources
    keep = sorted(range(len(sources)), key=lambda i: _source_rank(sources[i]), reverse=True)[:max_sources]
    return [sources[i] for i in sorted(keep)]


def _vote(sources: list, f: str) -> tuple:
    """按日期值分组投票：依次比最高来源级别、有没有人工记录、支持的不同 URL 数、最新发布时间。
    返回 (胜出值, 胜出组的最高级别, 和胜出值同级、同样有无人工记录、同票的所有值[不止一个时])。"""
    groups: dict = {}
    for i, src in enumerate(sources):
        v = src.get(f)
        if not v:
            continue
        g = groups.setdefault(v, {"level": 0, "manual": False, "urls": set(), "published": "", "last": -1})
        g["level"] = max(g["level"], LEVEL.get(src.get("level"), 0))
        g["manual"] = g["manual"] or bool(src.get("manual"))
        g["urls"].add(src.get("url") or f"#{i}")
        g["published"] = max(g["published"], src.get("published") or "")
        g["last"] = i
    if not groups:
        return None, None, []

    def head(v):
        return (groups[v]["level"], groups[v]["manual"], len(groups[v]["urls"]))
    win = max(groups, key=lambda v: (*head(v), groups[v]["published"], groups[v]["last"]))
    tied = sorted(v for v in groups if head(v) == head(win))
    return win, groups[win]["level"], (tied if len(tied) > 1 else [])


def _verify_source_dates(e: dict) -> None:
    """每条来源的日期必须在它自己的引句里，不在就置空（这条证据的状态也跟着重算）。"""
    for src in e["sources"]:
        bad = [f for f in DATE_FIELDS if src.get(f) and not date_in_quote(src[f], src.get("quote") or "")]
        for f in bad:
            log.warning("来源日期不在它自己的引句里，置空：%s %s %s=%s", e["id"], src.get("url"), f, src[f])
            src[f] = None
        if bad and src.get("level"):
            src["status"] = status_of(src["level"], src)


def recompute_from_sources(e: dict) -> bool:
    """日期只采用事件当前保留的来源（投票）；状态只升不降（保留的来源里有 confirmed 证据就升级）。返回是否有变化。"""
    before = (e.get("status"), e.get("start"), e.get("end"), e.get("date_conflict"))
    _verify_source_dates(e)
    if e.get("status") != "confirmed":
        e["status"] = "confirmed" if any(src.get("status") == "confirmed" for src in e["sources"]) else "rumored"
    levels = e.setdefault("field_levels", {})
    conflicts = {}
    for f in DATE_FIELDS:
        v, lvl, tied = _vote(e["sources"], f)
        e[f] = v
        conflicts[f] = tied
        if v is None:
            levels.pop(f, None)
        else:
            levels[f] = lvl
    if e.get("start") and e.get("end") and e["end"] < e["start"]:
        e["end"] = None
        levels.pop("end", None)
    # 开始日期有分歧就列开始日期；只有结束日期有分歧时列结束日期（前面加 ~）
    conflict = conflicts["start"] or ["~" + v for v in conflicts["end"]]
    if conflict:
        e["date_conflict"] = conflict
    else:
        e.pop("date_conflict", None)
    return (e.get("status"), e.get("start"), e.get("end"), e.get("date_conflict")) != before


BUNDLE = ("start", "end", "discount", "quote", "quote_in", "status")


def _fill_in(old: dict, new: dict) -> tuple:
    """同一来源又来了一条证据。返回 (有没有改动, 新证据有没有被采用)。
    引句相同：只补上原来空着的日期、折扣，证据状态只升不降（算采用）。
    引句不同：(start, end, discount, quote, quote_in, status) 当一整组比优先级，高的整组替换，不拼接；
    排名不更高就不采用。"""
    changed = False
    if normalize(old.get("quote") or "") == normalize(new.get("quote") or ""):
        for k in ("start", "end", "discount"):
            if old.get(k) is None and new.get(k) is not None:
                old[k] = new[k]
                changed = True
        if new.get("status") == "confirmed" and old.get("status") != "confirmed":
            old["status"] = "confirmed"
            changed = True
        return changed, True
    if _source_rank(new) > _source_rank(old):
        for k in BUNDLE:
            if old.get(k) != new.get(k):
                old[k] = new.get(k)
                changed = True
        return changed, True
    return False, False


def merge_candidates(events: list, candidates: list, now_iso: str, max_sources: int = 8) -> list:
    """返回合并后的新列表，不改动传入的 events。
    每个候选上记下 event_id（最后归到的事件）和 kept（来源有没有排进前 max_sources），调试存档用。"""
    by_id = {e["id"]: copy.deepcopy(e) for e in events}
    # 旧消息先并、新消息后并
    for c in sorted(candidates, key=lambda c: c["source"].get("published") or ""):
        key, kind = candidate_key(c)
        if c["event_type"] == "other" and not key:
            log.info("跳过没有名字也没有开始日期的 other 候选：%s", c["source"].get("url"))
            c["event_id"], c["kept"] = None, False
            continue
        eid = event_id(c["brand_id"], c["region"], c["event_type"], c["year"], key)
        if kind == "start":       # 没有名字的 other：日期对得上就并进非 other 事件
            target = _fold_target(by_id, c["brand_id"], c["region"], c["year"], c.get("start"), c.get("end"))
            if target is not None:
                eid = target["id"]
        c["event_id"], c["kept"] = eid, True
        src = dict(c["source"], seen=now_iso[:10])
        for k in ("status", "start", "end", "discount"):   # 证据自己的状态和日期（旧格式的候选没有就补上）
            src.setdefault(k, c.get(k))
        lvl = _field_level(src)
        e = by_id.get(eid)
        if e is None:
            e = by_id[eid] = {
                "id": eid,
                "brand_id": c["brand_id"], "region": c["region"],
                "event_type": c["event_type"], "year": c["year"],
                **({"sale_key": key, "sale_key_kind": kind} if key else {}),
                **{f: c.get(f) for f in FIELDS},
                "status": c["status"],
                "sources": [src],
                "field_levels": {f: lvl for f in TEXT_FIELDS if c.get(f)},
                "first_seen": now_iso, "updated": now_iso,
            }
            recompute_from_sources(e)
            continue
        changed = False
        adopted = True
        dup = next((x for x in e["sources"] if _same_source(x, src)), None)
        if dup is not None:
            changed, adopted = _fill_in(dup, src)
        else:
            kept = trim_sources(e["sources"] + [src], max_sources)
            if not any(x is src for x in kept):
                c["kept"] = False
                continue   # 排不进前 max_sources 的证据：不记来源，也不改日期、状态和其他字段
            e["sources"] = kept
            changed = True
        levels = e.setdefault("field_levels", {})
        for f in TEXT_FIELDS if adopted else ():   # 同一 URL、整组没被采用的证据：不能改折扣和活动名
            v = c.get(f)
            if v is None:
                continue
            cur = levels.get(f, 0) if e.get(f) is not None else 0
            if e.get(f) == v:
                levels[f] = max(cur, lvl)
            elif lvl >= cur:
                e[f] = v
                levels[f] = lvl
                changed = True
        changed = recompute_from_sources(e) or changed
        if changed:
            e["updated"] = now_iso
    moved = fold_other_events(by_id, now_iso, max_sources)
    for c in candidates:
        while c.get("event_id") in moved:
            c["event_id"] = moved[c["event_id"]]
        e = by_id.get(c.get("event_id"))
        # 最终有没有留在事件的来源里（后来的证据可能把它挤出前 max_sources）
        c["kept"] = e is not None and any(_same_source(x, c["source"]) for x in e["sources"])
    return sorted(by_id.values(), key=lambda e: e["id"])


def _fold_target(by_id: dict, brand_id: str, region: str, year: int, start, end):
    """找同品牌、同地区、同年份、开始日期相同、结束日期相同或有一方为空的非 other 事件。"""
    if not start:
        return None
    for eid in sorted(by_id):
        e = by_id[eid]
        if (e["event_type"] != "other" and e["brand_id"] == brand_id and e["region"] == region
                and e["year"] == year and e.get("start") == start
                and (end is None or e.get("end") is None or e.get("end") == end)):
            return e
    return None


def _exact_target(by_id: dict, e: dict, want) -> Optional[dict]:
    """起止日期都有、并且完全相同的另一个事件（同品牌、同地区、同年份），再按 want(候选) 过滤。"""
    if not (e.get("start") and e.get("end")):
        return None
    for tid in sorted(by_id):
        t = by_id[tid]
        if (tid != e["id"] and t["brand_id"] == e["brand_id"] and t["region"] == e["region"]
                and t["year"] == e["year"] and t.get("start") == e["start"] and t.get("end") == e["end"]
                and want(t)):
            return t
    return None


def _named(e: dict) -> bool:
    return e["event_type"] == "other" and e.get("sale_key_kind") == "name"


def fold_other_events(by_id: dict, now_iso: str, max_sources: int = 8) -> dict:
    """合并结束后把 other 事件扫一遍，返回 {被并掉的 id: 并进的 id}：
    1. 起止日期都有、并且和某个非 other 事件完全相同 → 并进它（有名、无名都一样，防重复）；
    2. 没有名字的 other：开始日期相同、结束日期相同或有一方为空的非 other 事件 → 并进它；
    3. 没有名字的 other：起止日期和某个有名 other 完全相同 → 并进有名的。"""
    moved: dict = {}
    for eid in sorted(by_id):
        e = by_id.get(eid)
        if e is None or e["event_type"] != "other":
            continue
        target = _exact_target(by_id, e, lambda t: t["event_type"] != "other")
        if target is None and not _named(e):
            target = _fold_target(by_id, e["brand_id"], e["region"], e["year"], e.get("start"), e.get("end"))
            if target is None:
                target = _exact_target(by_id, e, _named)
        if target is None:
            continue
        for src in e["sources"]:
            dup = next((x for x in target["sources"] if _same_source(x, src)), None)
            if dup is not None:
                _fill_in(dup, src)
            else:
                target["sources"] = trim_sources(target["sources"] + [src], max_sources)
        levels = target.setdefault("field_levels", {})
        for f in TEXT_FIELDS:
            if target.get(f) is None and e.get(f) is not None:
                target[f] = e[f]
                levels[f] = (e.get("field_levels") or {}).get(f, 1)
        if e.get("status") == "confirmed":
            target["status"] = "confirmed"           # 只升不降
        recompute_from_sources(target)
        target["first_seen"] = min(target.get("first_seen") or now_iso, e.get("first_seen") or now_iso)
        target["updated"] = now_iso
        del by_id[eid]
        moved[eid] = target["id"]
        log.info("other 事件 %s 并进 %s", eid, target["id"])
    return moved


def _ov_value(v):
    if isinstance(v, (date, datetime)):
        return v.isoformat()[:10]
    return v


def apply_overrides(events: list, overrides: dict) -> list:
    out = []
    used = set()
    for e in events:
        ov = overrides.get(e["id"])
        if not ov:
            out.append(e)
            continue
        used.add(e["id"])
        if ov.get("delete"):
            continue
        e = copy.deepcopy(e)
        fields = []
        for k, v in ov.items():
            if k not in OVERRIDE_FIELDS:
                log.warning("overrides.yaml：%s 的字段 %s 不认识，忽略", e["id"], k)
                continue
            v = _ov_value(v)
            if k == "status" and v not in ("confirmed", "rumored"):
                log.warning("overrides.yaml：%s 的 status 只能是 confirmed / rumored", e["id"])
                continue
            e[k] = v
            fields.append(k)
            if k in ("start", "end"):
                e.pop("date_conflict", None)     # 人工定了日期，就不再显示“日期有分歧”
        e["overridden"] = fields
        out.append(e)
    for eid in overrides:
        if eid not in used:
            log.info("overrides.yaml：事件 %s 目前不存在，暂不生效", eid)
    return out


def diff_changes(before: list, after: list) -> list:
    """新事件 / 新官宣 / 日期变动。"""
    old = {e["id"]: e for e in before}
    changes = []
    for e in after:
        base = {k: e.get(k) for k in ("id", "brand_id", "region", "event_type", "event_name",
                                       "start", "end", "discount", "status")}
        o = old.get(e["id"])
        if o is None:
            changes.append(dict(base, kind="new_event"))
            continue
        if e["status"] == "confirmed" and o.get("status") != "confirmed":
            changes.append(dict(base, kind="confirmed"))
        moved = [f for f in ("start", "end") if o.get(f) != e.get(f)]
        if moved and any(e.get(f) for f in moved):      # 新日期为空（被撤掉）时不报
            changes.append(dict(base, kind="date_changed", old_start=o.get("start"), old_end=o.get("end")))
    return changes


def merge_run(base_events: list, candidates: list, overrides: dict, now_iso: str, max_sources: int = 8) -> tuple:
    """一次完整的合并：返回 (新的 sales.json 事件, 套过纠错的最终事件, 变更清单)。"""
    before = apply_overrides(base_events, overrides)
    new_base = merge_candidates(base_events, candidates, now_iso, max_sources)
    final = apply_overrides(new_base, overrides)
    return new_base, final, diff_changes(before, final)
