"""合并四份输入，得到最终事件 data/local/final.json（§0.1 修订：每次从头算，不在已有结果上累加）。

    combine(news_events, official_events, manual_candidates, overrides) -> final_events

- news_events：云端新闻部分（data/news/sales.json 的 events；Mac 上读取后缓存在 data/local/news_cache.json）
- official_events：官网监控部分（data/local/official_base.json 的 events，只由 Mac 写）
- manual_candidates：config/manual_events.yaml（load_manual_candidates + finalize_candidates 之后的候选），每次重新读
- overrides：config/overrides.yaml 的 events

优先级 overrides > manual > official > AI 新闻：
1. 新闻事件做底。
2. 官网事件按 id 并进来。同一个 id：来源合在一起（同一 URL 只留一条，合不下时按 merge.trim_sources 的优先级留），
   日期和状态按合起来的来源重算（merge.recompute_from_sources：按来源级别投票，官网级 > 权威媒体 > 其他；
   状态有一条已官宣证据就是已官宣）；折扣、活动名：官网事件的级别 ≥ 新闻事件的级别就用官网的；
   first_seen 取早的，updated 取晚的。
3. 把 other 事件扫一遍（merge.fold_other_events，规则和以前一次合并时相同，例如没名字的 other 并进同一天开始的黑五）。
4. 人工记录走 merge.merge_candidates（人工来源在日期投票、来源排序、折扣、活动名上排在同级的官网来源前面）。
5. 最后套 overrides（AI 和人工记录都盖不过人工纠错）。

纯函数：不读写文件，不改动传入的参数；同样的输入得到同样的输出（用到的时间戳只取自输入，不取当前时间）。
输出事件的字段和 sales.json 里的事件一致：id、brand_id、region、event_type、year、event_name、start、end、discount、
status、sources[...]、field_levels、first_seen、updated，可选 sale_key / sale_key_kind / date_conflict，
被人工纠错改过的另有 overridden（改了哪些字段）。

命令行（在 app 目录下）：
  .venv312/bin/python -m dealradar.combine                重新算一次 data/local/final.json
  .venv312/bin/python -m dealradar.combine --news <文件>  指定新闻部分（默认 news_cache.json，没有就用 data/news/sales.json）
"""
from __future__ import annotations

import argparse
import copy
import logging
import sys
from collections import Counter
from pathlib import Path
from typing import Optional

from .config import Paths, load_config, load_json, now_utc, save_json
from .manual import load_manual_candidates
from .merge import (TEXT_FIELDS, _field_level, _fill_in, _same_source, apply_overrides, fold_other_events,
                    merge_candidates, recompute_from_sources, trim_sources)
from .status import finalize_candidates

log = logging.getLogger(__name__)

EPOCH = "1970-01-01T00:00:00+00:00"


def events_of(doc) -> list:
    """sales.json / official_base.json / final.json 的文档 → 事件列表（None 或空文档 → []）。"""
    if isinstance(doc, dict):
        return list(doc.get("events") or [])
    return list(doc or [])


def _latest_timestamp(news: list, official: list, manual: list) -> str:
    vals = [e.get(k) for e in news + official for k in ("updated", "first_seen") if e.get(k)]
    vals += [(c.get("source") or {}).get("published") for c in manual if (c.get("source") or {}).get("published")]
    return max(vals) if vals else EPOCH


def _absorb(e: dict, o: dict, max_sources: int) -> None:
    """官网事件 o 并进同 id 的新闻事件 e（e 是副本，原地改）。"""
    for src in o.get("sources") or []:
        src = copy.deepcopy(src)
        dup = next((x for x in e["sources"] if _same_source(x, src)), None)
        if dup is not None:
            _fill_in(dup, src)
        else:
            e["sources"].append(src)
    e["sources"] = trim_sources(e["sources"], max_sources)
    levels = e.setdefault("field_levels", {})
    olv = o.get("field_levels") or {}
    for f in TEXT_FIELDS:
        v = o.get(f)
        if v is None:
            continue
        cur = levels.get(f, 0) if e.get(f) is not None else 0
        lvl = olv.get(f, 0)
        if e.get(f) == v:
            levels[f] = max(cur, lvl)
        elif lvl >= cur:               # 同级时官网优先
            e[f] = v
            levels[f] = lvl
    if not e.get("sale_key") and o.get("sale_key"):
        e["sale_key"], e["sale_key_kind"] = o["sale_key"], o.get("sale_key_kind")
    if o.get("status") == "confirmed":
        e["status"] = "confirmed"
    recompute_from_sources(e)
    e["first_seen"] = min(x for x in (e.get("first_seen"), o.get("first_seen")) if x) \
        if (e.get("first_seen") or o.get("first_seen")) else None
    e["updated"] = max(x for x in (e.get("updated"), o.get("updated")) if x) \
        if (e.get("updated") or o.get("updated")) else None


def combine(news_events: list, official_events: list, manual_candidates: list, overrides: dict,
            max_sources: int = 8, now_iso: Optional[str] = None) -> list:
    """见模块说明。now_iso 只用在“人工记录新建 / 改动的事件”的时间戳上；不给就取输入里最晚的时间，保证可重复。"""
    news = copy.deepcopy(list(news_events or []))
    official = copy.deepcopy(list(official_events or []))
    manual = copy.deepcopy(list(manual_candidates or []))
    now_iso = now_iso or _latest_timestamp(news, official, manual)

    by_id = {e["id"]: e for e in news}
    for e in by_id.values():
        e["sources"] = list(e.get("sources") or [])
    for o in sorted(official, key=lambda x: x["id"]):
        e = by_id.get(o["id"])
        if e is None:
            by_id[o["id"]] = o
        else:
            _absorb(e, o, max_sources)
    fold_other_events(by_id, now_iso, max_sources)

    before = set(by_id)
    events = merge_candidates(list(by_id.values()), manual, now_iso, max_sources) if manual \
        else sorted(by_id.values(), key=lambda e: e["id"])
    for e in events:
        if e["id"] in before:
            continue
        # 人工记录新建的事件：时间戳用人工记录的查看日期（不随每次运行变）
        seen = sorted((s.get("published") or "") for s in e["sources"] if s.get("manual") and s.get("published"))
        if seen:
            e["first_seen"] = seen[0]
            e["updated"] = seen[-1]
    return sorted(apply_overrides(events, overrides or {}), key=lambda e: e["id"])


# ---------- 迁移拆分用：事件只留一部分来源 ----------
def rebuild_part(e: dict, sources: list, keep_text: bool = False) -> dict:
    """事件只留下 sources 这部分来源（scripts/migrate_split.py 用）：日期、状态按留下的来源重算；活动名保留。
    折扣：keep_text=True（事件的非人工来源全在这一部分）时原样保留；否则有留下的来源支持就保留原值，
    不支持就换成留下的来源里级别最高的折扣，留下的来源都没有折扣就保留原值。
    字段级别不超过留下的来源的最高级别，合并时级别高的那部分优先。"""
    p = copy.deepcopy(e)
    p["sources"] = copy.deepcopy(list(sources))
    p["status"] = "rumored"
    p.pop("date_conflict", None)
    levels = dict(p.get("field_levels") or {})
    p["field_levels"] = levels
    recompute_from_sources(p)
    top = max((_field_level(s) for s in p["sources"]), default=0)
    with_disc = [s for s in p["sources"] if s.get("discount")]
    support = [s for s in with_disc if s["discount"] == p.get("discount")]
    if p.get("discount") and support:
        levels["discount"] = max(_field_level(s) for s in support)
    elif with_disc and not keep_text:
        best = max(with_disc, key=lambda s: (_field_level(s), s.get("published") or ""))
        p["discount"] = best["discount"]
        levels["discount"] = _field_level(best)
    elif p.get("discount"):
        levels["discount"] = min(levels.get("discount", top), top)
    if p.get("event_name"):
        levels["event_name"] = min(levels.get("event_name", top), top)
    return p


# ---------- 读输入、写 final.json（mac_run 和命令行用）----------
def load_manual(cfg) -> list:
    """config/manual_events.yaml → 候选（每次重新读；不合格的条目丢弃并打 warning）。"""
    dropped: dict = {}
    cands = load_manual_candidates(cfg.manual_events or [], cfg.brands, int(cfg.settings.get("event_year", 2026)),
                                   dropped)
    return finalize_candidates(cands, cfg.brands, dropped, [])


def news_input(paths: Paths) -> tuple:
    """Mac 上用的新闻部分：先用 news_cache.json（从云端取来的），没有就用本地 data/news/sales.json。
    返回 (事件列表, 用的哪个文件 或 None)。"""
    for p in (paths.news_cache, paths.sales):
        doc = load_json(p, None)
        if doc is not None:
            return events_of(doc), p
    return [], None


def build_final(paths: Paths, cfg=None, news_events: Optional[list] = None, now_iso: Optional[str] = None) -> dict:
    """读四份输入、算 final、写 data/local/final.json。返回写入的文档 {"generated_at", "events"}。"""
    cfg = cfg or load_config(paths)
    if news_events is None:
        news_events, _ = news_input(paths)
    official = events_of(load_json(paths.official_base, None))
    manual = load_manual(cfg)
    events = combine(news_events, official, manual, cfg.overrides,
                     int(cfg.settings.get("max_sources_per_event", 8)))
    doc = {"generated_at": now_iso or now_utc().isoformat(timespec="seconds"), "events": events}
    save_json(paths.final, doc)
    log.info("final.json：新闻 %d + 官网 %d + 人工 %d → %d 个事件 → %s",
             len(news_events), len(official), len(manual), len(events), paths.final)
    return doc


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="合并 新闻 + 官网 + 人工记录 + 人工纠错 → data/local/final.json")
    ap.add_argument("--news", type=Path, default=None, help="新闻部分（默认 data/local/news_cache.json，没有就用 data/news/sales.json）")
    ap.add_argument("--data-dir", type=Path, default=Paths.data_dir)
    ap.add_argument("--config-dir", type=Path, default=Paths.config_dir)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stdout)
    paths = Paths(config_dir=args.config_dir, data_dir=args.data_dir)
    news = events_of(load_json(args.news, None)) if args.news else None
    doc = build_final(paths, news_events=news)
    c = Counter(e.get("status") for e in doc["events"])
    print(f"final.json：{len(doc['events'])} 个事件（已官宣 {c.get('confirmed', 0)}、传闻 {c.get('rumored', 0)}）→ {paths.final}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
