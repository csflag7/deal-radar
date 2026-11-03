"""一次性迁移（§0.1 修订）：把旧的 data/sales.json（新闻 + 官网监控 + 人工记录合在一起的累积结果）按来源拆开。

- 来源级别是 official、并且来自官网监控（来源名是「<品牌> official website (<网站>)」，或者网址在官网监控的状态文件里）
  → data/local/official_base.json；
- 人工记录（manual=true）不单独存：以后每次从 config/manual_events.yaml 读；
- 其余（新闻）→ data/news/sales.json。
官网页面内容没变时程序不会重新提取，所以不能清空重跑，只能拆。

同时把其余文件挪到新位置（只是改位置，不改内容）：
  data/seen.json、source_health.json、changes.json → data/news/
  data/official_state.json、manual_review.json     → data/local/
原来的 data/sales.json 改名为 data/sales.pre-split.json 留档（不删）。

用法（在 app 目录下）：
  .venv312/bin/python scripts/migrate_split.py            dry-run：只打印统计和核对结果，不写任何文件
  .venv312/bin/python scripts/migrate_split.py --apply    确认无误后真的写
核对：用 combine（新闻 + 官网 + 人工记录，不套人工纠错）把拆开的两份合回去，和原来的事件逐个比较 id、日期、状态。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

from dealradar.combine import combine, load_manual, rebuild_part  # noqa: E402
from dealradar.config import Paths, load_config, load_json, save_json  # noqa: E402

OFFICIAL_NAME = re.compile(r" official website \(", re.I)
MOVES = [("seen.json", "news"), ("source_health.json", "news"), ("changes.json", "news"),
         ("official_state.json", "local"), ("manual_review.json", "local")]


def source_kind(s: dict, monitored_urls: set) -> str:
    """manual / official（官网监控）/ news。"""
    if s.get("manual"):
        return "manual"
    if s.get("level") == "official" and (OFFICIAL_NAME.search(s.get("name") or "") or s.get("url") in monitored_urls):
        return "official"
    return "news"


def split_events(events: list, monitored_urls: set) -> tuple:
    """返回 (news_events, official_events, stats)。"""
    news, official = [], []
    st = Counter()
    for e in events:
        parts = {"news": [], "official": [], "manual": []}
        for s in e.get("sources") or []:
            k = source_kind(s, monitored_urls)
            parts[k].append(s)
            st[f"sources_{k}"] += 1
            if k == "official":
                st["official_by_name"] += bool(OFFICIAL_NAME.search(s.get("name") or ""))
                st["official_by_state_url"] += s.get("url") in monitored_urls
        whole = not (parts["news"] and parts["official"])     # 非人工来源全在一边：折扣原样保留
        if parts["news"]:
            news.append(rebuild_part(e, parts["news"], keep_text=whole))
        if parts["official"]:
            official.append(rebuild_part(e, parts["official"], keep_text=whole))
        if parts["news"] and parts["official"]:
            st["events_in_both"] += 1
        if not parts["news"] and not parts["official"]:
            st["events_manual_only"] += 1
    return news, official, st


def verify(original: list, news: list, official: list, manual: list) -> list:
    """合回去和原来比较；返回差异说明（空 = 完全对得上）。"""
    back = {e["id"]: e for e in combine(news, official, manual, {})}
    orig = {e["id"]: e for e in original}
    diffs = []
    for i in sorted(set(orig) - set(back)):
        diffs.append(f"合回去后少了：{i}")
    for i in sorted(set(back) - set(orig)):
        diffs.append(f"合回去后多了：{i}")
    for i in sorted(set(orig) & set(back)):
        a, b = orig[i], back[i]
        for f in ("start", "end", "status", "discount", "event_name"):
            if a.get(f) != b.get(f):
                diffs.append(f"{i} 的 {f}：原来 {a.get(f)!r} → 合回去 {b.get(f)!r}")
        if len(a.get("sources") or []) != len(b.get("sources") or []):
            diffs.append(f"{i} 的来源条数：原来 {len(a['sources'])} → 合回去 {len(b['sources'])}")
    return diffs


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="把旧的 data/sales.json 按来源拆成 news / official 两份")
    ap.add_argument("--data-dir", type=Path, default=Paths.data_dir)
    ap.add_argument("--config-dir", type=Path, default=Paths.config_dir)
    ap.add_argument("--apply", action="store_true", help="真的写（不加就是 dry-run）")
    args = ap.parse_args(argv)
    paths = Paths(config_dir=args.config_dir, data_dir=args.data_dir)
    old = args.data_dir / "sales.json"
    doc = load_json(old, None)
    if doc is None:
        print(f"没有 {old}：不需要迁移（或者已经迁移过）")
        return 0
    for target in (paths.sales, paths.official_base):
        if target.exists():
            print(f"{target} 已经存在：看起来迁移过了，为了不覆盖，停下")
            return 2
    events = doc.get("events") or []
    state = load_json(args.data_dir / "official_state.json", None) or load_json(paths.official_state, {}) or {}
    monitored = set((state.get("pages") or {}).keys())
    news, official, st = split_events(events, monitored)
    cfg = load_config(paths)
    manual = load_manual(cfg)
    diffs = verify(events, news, official, manual)

    total_src = sum(len(e.get("sources") or []) for e in events)
    print(f"原 {old.name}：{len(events)} 个事件，{total_src} 条来源（更新于 {doc.get('updated')}）")
    print(f"  来源：新闻 {st['sources_news']}，官网监控 {st['sources_official']}"
          f"（按来源名认出 {st['official_by_name']}，网址在官网状态文件里 {st['official_by_state_url']}），"
          f"人工记录 {st['sources_manual']}（不单独存，从 manual_events.yaml 读，现有 {len(manual)} 条合格）")
    print(f"拆分后：news/sales.json {len(news)} 个事件（来源 {sum(len(e['sources']) for e in news)}）；"
          f"local/official_base.json {len(official)} 个事件（来源 {sum(len(e['sources']) for e in official)}）；"
          f"两边都有的事件 {st['events_in_both']}，只有人工来源的事件 {st['events_manual_only']}")
    ok_src = st["sources_news"] + st["sources_official"] + st["sources_manual"] == total_src
    print(f"核对来源条数：新闻 + 官网 + 人工 = {st['sources_news'] + st['sources_official'] + st['sources_manual']}"
          f" {'=' if ok_src else '≠'} {total_src}")
    print(f"核对合回去（不套人工纠错）：{'和原来完全一致' if not diffs else f'有 {len(diffs)} 处不同'}")
    for d in diffs[:40]:
        print(f"  {d}")
    moves = [(args.data_dir / n, (paths.news_dir if where == "news" else paths.local_dir) / n) for n, where in MOVES]
    for src, dst in moves:
        if src.exists():
            print(f"挪位置：{src.relative_to(args.data_dir)} → {dst.relative_to(args.data_dir)}")
    print(f"留档：{old.name} → sales.pre-split.json")
    if not ok_src:
        print("来源条数对不上，停下")
        return 2
    if not args.apply:
        print("dry-run：没有写任何文件。确认无误后加 --apply")
        return 0

    updated = doc.get("updated")
    save_json(paths.sales, {"updated": updated, "events": news})
    save_json(paths.official_base, {"updated": updated, "events": official})
    for src, dst in moves:
        if src.exists():
            if dst.exists():
                print(f"{dst} 已经存在，不覆盖，{src.name} 留在原处")
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            src.replace(dst)
    pre = args.data_dir / "sales.pre-split.json"
    if pre.exists():
        print(f"{pre} 已经存在，{old.name} 留在原处")
    else:
        old.replace(pre)
    check_news = load_json(paths.sales, {})["events"]
    check_off = load_json(paths.official_base, {})["events"]
    print(f"已写入：{paths.sales}（{len(check_news)} 个事件）、{paths.official_base}（{len(check_off)} 个事件）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
