"""命令行（手动试跑用；定时任务由 mac_run 调 dealradar.cart.run）。

    python -m dealradar.cart preview [--events data/local/final.json] [--data-dir 目录] [--now 2026-11-27T08:00]
        跑一次购物车（型号确认、读价格、建议、提醒），有提醒就写一封**预览**邮件（不发送）
    python -m dealradar.cart probe --region CA <商品页网址> [...]
        按规矩读一次这些商品页的价格，只打印结果，不写任何文件
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

from ..config import APP_ROOT, load_json, load_yaml
from .mail import send_digest
from .prices import PoliteFetcher, currency_ok, read_page
from .conf import load_cart_config
from .runner import run


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m dealradar.cart")
    sub = ap.add_subparsers(dest="cmd", required=True)
    pv = sub.add_parser("preview", help="跑一次，有提醒就写预览邮件（不发送）")
    pv.add_argument("--events", type=Path, default=None, help="活动数据（含 events 列表的 JSON），默认 data/local/final.json")
    pv.add_argument("--data-dir", type=Path, default=None, help="购物车数据目录（默认 Obsidian 的 _黑五/data）")
    pv.add_argument("--now", default=None, help="模拟的当前时间（本地时间，ISO 格式），试跑提醒用")
    pr = sub.add_parser("probe", help="读一次商品页价格，只打印")
    pr.add_argument("--region", choices=["US", "CA"], required=True)
    pr.add_argument("urls", nargs="+")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    settings = load_yaml(APP_ROOT / "config" / "settings.yaml")

    if args.cmd == "probe":
        fcfg = load_cart_config().get("fetch") or {}
        with PoliteFetcher(dict(fcfg, robots_agent=settings.get("robots_agent", "DealRadar"))) as f:
            for u in args.urls:
                r = read_page(f, u)
                r["currency_ok"] = currency_ok(r.get("currency"), args.region) if r["status"] == "ok" else None
                print(json.dumps(dict(url=u, **r), ensure_ascii=False), flush=True)
        return 0

    # 默认用 Mac 本地合并好的 final.json（§0.1 修订）；没有就退到新闻部分、旧的 sales.json
    candidates = [APP_ROOT / "data" / "local" / "final.json", APP_ROOT / "data" / "news" / "sales.json",
                  APP_ROOT / "data" / "sales.json"]
    events_path = args.events or next((c for c in candidates if c.exists()), candidates[0])
    events = (load_json(events_path, {}) or {}).get("events") or []
    now = datetime.fromisoformat(args.now).astimezone() if args.now else datetime.now().astimezone()
    print(f"活动数据：{events_path}（{len(events)} 条）；时间：{now.isoformat(timespec='minutes')}")
    res = run(events, now, settings, data_dir=args.data_dir)
    print(f"提醒 {len(res['reminders'])} 条")
    for r in res["reminders"]:
        print(f"  [{r['kind']}] {r['item']}：{r['text']}")
    if res.get("digest"):
        out = send_digest(res["digest"]["subject"], res["digest"]["body"], preview=True, data_dir=args.data_dir, now=now)
        print(f"预览邮件：{out['html_path']}\n          {out['text_path']}")
    else:
        print("没有要发的汇总")
    return 0


if __name__ == "__main__":
    sys.exit(main())
