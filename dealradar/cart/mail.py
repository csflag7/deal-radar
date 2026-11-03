"""汇总邮件：send_digest(subject, body, preview) -> dict。

- preview=True：只把邮件写成 HTML + 文本文件，放进 Obsidian 的 _黑五/data/mail_preview/，返回路径；**绝不发送**，
  也不写 notified.json
- preview=False：只在 macOS 上；用 AppleScript 让「邮件」App 发送。收件人只从本地文件
  ~/Library/Application Support/DealRadar/mail.json 读（{"to": "...", "allow": ["..."]}，不进仓库），
  而且必须在白名单里；当天已经发过就不再发（每天最多 1 封）；发送成功后才把本次的提醒记进 notified.json
- 发信前先在 notified.json 的 digests[当天] 记 {"state": "sending"}，发完改成 {"state": "sent"}。
  发信成功但后面写文件失败时，sending 会留下来：当天不再自动重发；下一封汇总开头提醒“上次可能已发，请查看已发送邮件”
- 数据目录和购物车（runner）用同一份 settings（cart_data_dir），notified / pending 读写的是同一个位置
"""
from __future__ import annotations

import copy
import hashlib
import html
import json
import logging
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from .conf import MAIL_CONFIG, Files, load_cart_config, read_json, resolve_data_dir, write_json

log = logging.getLogger(__name__)

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")

# 参数从 argv 传进去（不拼进脚本），主题和正文里有引号、换行都没关系
APPLESCRIPT = [
    "on run argv",
    "set theSubject to item 1 of argv",
    "set theBody to item 2 of argv",
    "set theTo to item 3 of argv",
    'tell application "Mail"',
    "set m to make new outgoing message with properties {subject:theSubject, content:theBody, visible:false}",
    "tell m to make new to recipient at end of to recipients with properties {address:theTo}",
    "send m",
    "end tell",
    "end run",
]


class MailError(Exception):
    pass


def body_hash(subject: str, body: str) -> str:
    return hashlib.sha256((subject + "\n" + body).encode("utf-8")).hexdigest()


def render_html(subject: str, body: str) -> str:
    """纯文本正文 → 简单的 HTML（全部转义；网址变成链接）。"""
    rows = []
    for line in body.splitlines():
        esc = html.escape(line)
        esc = re.sub(r"(https?://[^\s<>&\"']+)", r'<a href="\1">\1</a>', esc)
        rows.append(esc if esc.strip() else "&nbsp;")
    return ("<!doctype html><html lang=\"zh\"><head><meta charset=\"utf-8\">"
            f"<title>{html.escape(subject)}</title></head>"
            "<body style=\"font-family:-apple-system,'PingFang SC',sans-serif;max-width:720px;margin:24px auto;"
            "line-height:1.6;color:#222\">"
            "<p style=\"color:#a15c00;font-size:12px\">【预览】这封邮件没有发送。</p>"
            f"<h2 style=\"font-size:18px\">{html.escape(subject)}</h2>"
            "<div style=\"white-space:pre-wrap\">" + "<br>".join(rows) + "</div></body></html>\n")


def load_recipient(path: Path = MAIL_CONFIG) -> str:
    """收件人只从本地文件读，而且必须在白名单里。"""
    if not path.exists():
        raise MailError(f"没有收件人设置文件：{path}")
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as e:
        raise MailError("收件人设置文件不是合法 JSON") from e
    to = str((cfg or {}).get("to") or "").strip()
    allow = [str(a).strip().lower() for a in (cfg or {}).get("allow") or [] if a]
    if not EMAIL_RE.match(to):
        raise MailError("收件人地址格式不对")
    if to.lower() not in allow:
        raise MailError("收件人不在白名单里")
    return to


def _files(data_dir: Optional[Path], settings: Optional[dict] = None) -> Files:
    """和 runner 一样按 settings['cart_data_dir'] 等定位数据目录。"""
    return Files(resolve_data_dir(settings, load_cart_config(), data_dir))


def digest_state(v) -> str:
    """notified.json 里 digests[某天] 的状态：旧格式（时间字符串）算 sent。"""
    return (v.get("state") or "sent") if isinstance(v, dict) else "sent"


UNSURE_NOTE = "提醒：{days} 的汇总上次可能已经发出（发出后没来得及记下来），请到「邮件」App 的“已发送”里看一下。\n\n"


def write_preview(subject: str, body: str, files: Files, now: datetime) -> dict:
    d = files.preview_dir
    d.mkdir(parents=True, exist_ok=True)
    stamp = now.strftime("%Y%m%d-%H%M%S")
    txt = d / f"digest-{stamp}.txt"
    htm = d / f"digest-{stamp}.html"
    for p, text in ((txt, f"主题：{subject}\n\n{body}"), (htm, render_html(subject, body))):
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(p)
    return {"sent": False, "preview": True, "text_path": str(txt), "html_path": str(htm)}


def mark_sent(files: Files, subject: str, body: str, now: datetime, day: str, reminded: tuple = ()) -> int:
    """发送成功后：把待发清单（cart_pending.json）里的提醒记进 notified.json，记下当天已发（state=sent）；
    reminded：这封邮件里已经提醒过“可能已发”的那几天，改成 uncertain_reminded（以后不再提醒）。返回记了几条。"""
    notified = read_json(files.notified, {})
    notified.setdefault("keys", {})
    notified.setdefault("digests", {})
    pending = read_json(files.pending, {})
    n = 0
    if pending.get("hash") == body_hash(subject, body):
        for k in pending.get("keys") or []:
            if k not in notified["keys"]:
                notified["keys"][k] = now.isoformat(timespec="seconds")
                n += 1
    else:
        log.warning("发出的邮件和待发清单对不上，提醒没有记进去重记录")
    at = now.isoformat(timespec="seconds")
    notified["digests"][day] = {"state": "sent", "at": at}
    for d in reminded:
        notified["digests"][d] = {"state": "uncertain_reminded", "at": at}
    write_json(files.notified, notified)
    return n


def send_digest(subject: str, body: str, preview: bool, *, data_dir: Optional[Path] = None,
                config_path: Path = MAIL_CONFIG, runner=subprocess.run, now: Optional[datetime] = None,
                platform: Optional[str] = None, settings: Optional[dict] = None) -> dict:
    now = now or datetime.now().astimezone()
    files = _files(data_dir, settings)
    if preview:
        return write_preview(subject, body, files, now)
    if (platform or sys.platform) != "darwin":
        raise MailError("只能在 macOS 上用「邮件」App 发送")
    pending = read_json(files.pending, {})
    day = pending.get("date") or now.date().isoformat()
    existed = files.notified.exists()
    notified = read_json(files.notified, {})
    original = copy.deepcopy(notified)
    digests = notified.get("digests") or {}
    if day in digests:
        if digest_state(digests[day]) == "sending":
            return {"sent": False, "preview": False, "uncertain": True,
                    "reason": f"{day} 的汇总上次发送结果不明（可能已经发出），不自动重发；请到「邮件」App 的“已发送”里看一下"}
        return {"sent": False, "preview": False, "reason": f"{day} 已经发过汇总（每天最多 1 封）"}
    to = load_recipient(config_path)
    unsure = tuple(sorted(d for d, v in digests.items() if digest_state(v) == "sending"))
    out_body = UNSURE_NOTE.format(days="、".join(unsure)) + body if unsure else body
    # 先记“正在发”再发信：发出去以后哪一步写文件失败，下次都不会自动重发
    notified.setdefault("digests", {})[day] = {"state": "sending", "at": now.isoformat(timespec="seconds")}
    write_json(files.notified, notified)
    cmd = ["osascript"]
    for line in APPLESCRIPT:
        cmd += ["-e", line]
    cmd += [subject, out_body, to]
    r = runner(cmd, capture_output=True, text=True, timeout=120)
    if getattr(r, "returncode", 1) != 0:          # 明确没发出去：撤掉“正在发”
        if existed:
            write_json(files.notified, original)
        else:
            files.notified.unlink(missing_ok=True)
        raise MailError(f"「邮件」App 发送失败：{(getattr(r, 'stderr', '') or '')[:200]}")
    n = mark_sent(files, subject, body, now, day, unsure)     # 哈希按原正文算，和 cart_pending 对得上
    return {"sent": True, "preview": False, "marked": n}
