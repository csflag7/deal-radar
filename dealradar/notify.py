"""S4 推送：用 ntfy.sh 给手机发通知（方案 §2 ⑥）。

- 每次运行最多 1 条摘要：新官宣、日期变动、新事件里有日期的、明天开始的（按 America/Toronto 判断“明天”），
  最多列 10 条，超出的写“还有 N 条”，末尾附网页链接；正文不超过 3500 字节。
- 首次运行（data/news/sales.json 原来不存在）只建库，不发摘要。
- 告警单独 1 条（priority = high）：信息源连续失败达到 alert_source_failures 次，或 AI 整体失败 / 额度不足。
- 主题名从环境变量 NTFY_TOPIC 读（也可以写在 app/.env）；没配只打 warning，不报错。
- 服务器默认 https://ntfy.sh（settings 的 ntfy_server 可改）。用 JSON 方式发布（POST 到服务器根地址），
  中文标题不用塞进 HTTP 头。

预览（不发送）：
  .venv/bin/python -m dealradar.notify --preview --data-dir data [--as-if-not-first-run]
读 sales.json（套上 overrides.yaml）、changes.json、source_health.json，把将要发的摘要和告警原样打印出来。
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import httpx

from .build_web import display_name
from .config import Paths, load_config, load_json, local_date

log = logging.getLogger(__name__)

REGION_ZH = {"US": "美国", "CA": "加拿大", "unknown": "地区未知"}
KIND_ZH = {"tomorrow": "明天开始", "confirmed": "新官宣", "date_changed": "日期变动", "new_event": "新活动"}
PRIORITY_HIGH = 4    # ntfy：1 最低 … 5 最高；4 = high


# ---------- 拼消息 ----------
def _md(d: Optional[str]) -> str:
    return f"{int(d[5:7])}/{int(d[8:10])}" if d else ""


def _range(start: Optional[str], end: Optional[str]) -> str:
    if start and end:
        return f"{_md(start)}–{_md(end)}"
    if start:
        return f"{_md(start)} 起"
    if end:
        return f"到 {_md(end)}"
    return "日期未定"


def _line(kind: str, e: dict, brands: dict) -> str:
    brand = (brands.get(e.get("brand_id")) or {}).get("name") or e.get("brand_id")
    name = display_name(e)      # 和网页一样：非 other 用类型中文名，other 用活动名
    text = f"【{KIND_ZH[kind]}】{brand} · {name}（{REGION_ZH.get(e.get('region'), e.get('region'))}）" \
           f"{_range(e.get('start'), e.get('end'))}"
    if kind == "date_changed":
        text += f"（原 {_range(e.get('old_start'), e.get('old_end'))}）"
    if e.get("discount"):
        text += f" · {e['discount']}"
    return text[:200]


def digest_items(changes: list, final_events: list, today: date) -> list:
    """[(kind, 事件或变更)]：明天开始 > 新官宣 > 日期变动 > 有日期的新事件；同一个事件只列一次；
    已经结束的（结束日期，或没有结束日期时的开始日期，早于今天）不列。"""
    tomorrow = (today + timedelta(days=1)).isoformat()
    out, seen = [], set()

    def add(kind, e):
        last = e.get("end") or e.get("start")
        if last and last < today.isoformat():
            return
        if e["id"] not in seen:
            seen.add(e["id"])
            out.append((kind, e))
    for e in sorted(final_events, key=lambda e: e["id"]):
        if e.get("start") == tomorrow:
            add("tomorrow", e)
    for kind in ("confirmed", "date_changed", "new_event"):
        for c in changes:
            if c.get("kind") == kind and (kind != "new_event" or c.get("start")):
                add(kind, c)
    return out


def build_digest(changes: list, final_events: list, brands: dict, today: date, site_url: str = "",
                 max_items: int = 10, max_bytes: int = 3500) -> Optional[dict]:
    """返回 {title, message, click}；没有可报的内容返回 None。"""
    items = digest_items(changes, final_events, today)
    if not items:
        return None
    lines = [_line(k, e, brands) for k, e in items]
    shown = min(max_items, len(lines))
    while True:
        body = lines[:shown]
        rest = len(lines) - shown
        if rest:
            body.append(f"还有 {rest} 条")
        if site_url:
            body += ["", site_url]
        message = "\n".join(body)
        if len(message.encode("utf-8")) <= max_bytes or shown == 0:
            break
        shown -= 1
    if len(message.encode("utf-8")) > max_bytes:      # 连一条都放不下（链接太长之类）：硬截断
        message = message.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")
    return {"title": f"Deal Radar · {today.month}月{today.day}日", "message": message, "click": site_url or None}


def build_alert(health: dict, threshold: int, ai_aborted: Optional[str], ai_errors: list,
                batches: int, processed: int) -> Optional[str]:
    """信息源连续失败、AI 整体失败或额度不足 → 告警正文；都没有返回 None。"""
    lines = []
    bad = sorted((sid, h) for sid, h in (health or {}).items()
                 if int(h.get("consecutive_failures") or 0) >= threshold)
    if bad:
        lines.append(f"信息源连续失败 ≥{threshold} 次（{len(bad)} 个）：")
        for sid, h in bad[:10]:
            lines.append(f"- {sid}：连续 {h.get('consecutive_failures')} 次，最近错误 {h.get('last_error') or '?'}")
        if len(bad) > 10:
            lines.append(f"- 还有 {len(bad) - 10} 个")
    if ai_aborted:
        lines.append(f"AI 提取中止（Key 无效或额度不足？）：{ai_aborted}"[:300])
    elif batches and not processed:
        lines.append(f"AI 提取整体失败：{batches} 批全部失败。{'; '.join(ai_errors[:3])}"[:300])
    return "\n".join(lines) if lines else None


# ---------- 发送 ----------
class Notifier:
    def __init__(self, server: str, topic: str, transport=None, timeout: float = 15):
        self.url = server.rstrip("/") + "/"
        self.topic = topic
        self.http = httpx.Client(timeout=timeout, transport=transport)

    def close(self) -> None:
        self.http.close()

    def send(self, title: str, message: str, tags: list, priority: Optional[int] = None,
             click: Optional[str] = None) -> bool:
        body = {"topic": self.topic, "title": title, "message": message, "tags": tags}
        if priority:
            body["priority"] = priority
        if click:
            body["click"] = click
        try:
            r = self.http.post(self.url, json=body)
        except httpx.HTTPError as e:
            log.warning("推送失败：%s", type(e).__name__)
            return False
        if r.status_code != 200:
            log.warning("推送失败：HTTP %s", r.status_code)
            return False
        return True


def plan_messages(settings: dict, changes: list, final_events: list, brands: dict, health: dict,
                  first_run: bool, today: date, ai_aborted: Optional[str] = None, ai_errors: Optional[list] = None,
                  batches: int = 0, processed: int = 0) -> tuple:
    """要发的消息：([{kind, title, tags, priority, click, message}], 说明)。告警在前，摘要在后，各最多 1 条。"""
    msgs, notes = [], []
    site = settings.get("site_url") or None
    alert = build_alert(health, int(settings.get("alert_source_failures", 3)), ai_aborted, ai_errors or [],
                        batches, processed)
    if alert:
        msgs.append({"kind": "alert", "title": f"Deal Radar 告警 · {today.month}月{today.day}日", "tags": ["warning"],
                     "priority": PRIORITY_HIGH, "click": site, "message": alert})
    if first_run:
        notes.append("首次运行：只建库，不发摘要")
    else:
        d = build_digest(changes, final_events, brands, today, settings.get("site_url") or "",
                         int(settings.get("notify_max_items", 10)), int(settings.get("notify_max_bytes", 3500)))
        if d is None:
            notes.append("没有要推送的变化")
        else:
            msgs.append({"kind": "digest", "title": d["title"], "tags": ["shopping_cart"], "priority": None,
                         "click": d["click"], "message": d["message"]})
    return msgs, notes


def push_run(settings: dict, changes: list, final_events: list, brands: dict, health: dict, first_run: bool,
             today: date, ai_aborted: Optional[str] = None, ai_errors: Optional[list] = None, batches: int = 0,
             processed: int = 0, transport=None, topic: Optional[str] = None) -> dict:
    """一次运行结束后的推送：最多 1 条摘要 + 最多 1 条告警。返回 {"digest": bool, "alert": bool}。"""
    sent = {"digest": False, "alert": False}
    topic = topic if topic is not None else os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        log.warning("没有配置 NTFY_TOPIC（环境变量或 app/.env），不推送")
        return sent
    msgs, notes = plan_messages(settings, changes, final_events, brands, health, first_run, today, ai_aborted,
                                ai_errors, batches, processed)
    for note in notes:
        log.info("推送：%s", note)
    n = Notifier(settings.get("ntfy_server") or "https://ntfy.sh", topic, transport)
    try:
        for m in msgs:
            sent[m["kind"]] = n.send(m["title"], m["message"], m["tags"], priority=m["priority"], click=m["click"])
            log.info("%s推送：%s", "告警" if m["kind"] == "alert" else "摘要", "已发送" if sent[m["kind"]] else "失败")
    finally:
        n.close()
    return sent


# ---------- 预览（不发送） ----------
def render_message(m: dict) -> str:
    head = [f"Title: {m['title']}", f"Tags: {','.join(m['tags'])}"]
    if m.get("priority"):
        head.append(f"Priority: {m['priority']}（high）" if m["priority"] == PRIORITY_HIGH else f"Priority: {m['priority']}")
    if m.get("click"):
        head.append(f"Click: {m['click']}")
    size = len(m["message"].encode("utf-8"))
    return "\n".join(head + [f"（正文 {size} 字节）", "", m["message"]])


def preview(data_dir: Path, config_dir: Path, as_if_not_first_run: bool = False) -> str:
    """按 data_dir 里上一次运行的结果，拼出将要发的推送（不发送）。"""
    from .merge import apply_overrides
    paths = Paths(config_dir=config_dir, data_dir=data_dir)
    cfg = load_config(paths)
    s = cfg.settings
    sales = load_json(paths.sales, None)
    ch = load_json(paths.changes, None)
    if sales is None or ch is None:
        return f"在 {paths.news_dir} 里没找到 sales.json 或 changes.json，先运行一次 dealradar.run。"
    health = load_json(paths.health, {})
    gen = ch.get("generated_at") or sales.get("updated")
    today = date.fromisoformat(local_date(datetime.fromisoformat(gen), s.get("timezone", "UTC"))) if gen \
        else date.today()
    first = bool(ch.get("first_run")) and not as_if_not_first_run
    final = apply_overrides(sales.get("events", []), cfg.overrides)
    msgs, notes = plan_messages(s, ch.get("changes", []), final, cfg.brands, health, first, today,
                                ch.get("ai_aborted"), ch.get("ai_errors"), int(ch.get("ai_batches") or 0),
                                int(ch.get("ai_processed") or 0))
    out = [f"预览（不发送）：{data_dir}，运行时间 {gen}，"
           f"{'首次运行' if ch.get('first_run') else '不是首次运行'}"
           f"{'（按不是首次运行预览）' if as_if_not_first_run and ch.get('first_run') else ''}"]
    for note in notes:
        out.append(f"· {note}")
    if not msgs:
        out.append("（没有要发的推送）")
    for i, m in enumerate(msgs, 1):
        out += ["", f"==== 第 {i} 条：{'告警' if m['kind'] == 'alert' else '摘要'} ====", render_message(m)]
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Deal Radar 推送预览（不发送）")
    ap.add_argument("--preview", action="store_true", help="打印将要发的摘要和告警，不发送")
    ap.add_argument("--data-dir", type=Path, default=Paths.data_dir)
    ap.add_argument("--config-dir", type=Path, default=Paths.config_dir)
    ap.add_argument("--as-if-not-first-run", action="store_true", help="按“不是首次运行”预览摘要")
    args = ap.parse_args(argv)
    if not args.preview:
        ap.error("目前只支持 --preview（真发推送由 dealradar.run 负责）")
    print(preview(args.data_dir, args.config_dir, args.as_if_not_first_run))
    return 0


if __name__ == "__main__":
    sys.exit(main())
