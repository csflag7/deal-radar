"""把 final.json 的活动同步进 Mac「日历」App 里的「黑五购物」日历（只在 Mac 上用；§0.1 修订）。

用法（在 app 目录下）：
  .venv312/bin/python -m dealradar.calendar_sync            默认 dry-run：只打印将要新增 / 修改 / 删除的清单，不碰日历
  .venv312/bin/python -m dealradar.calendar_sync --check    只读检查：iCloud 的「黑五购物」在不在、有没有重名，比对两边 ID
  .venv312/bin/python -m dealradar.calendar_sync --apply    真的写日历：还要 settings.yaml 的 calendar_apply 是 true，
                                                            否则照样按 dry-run 运行（第一次要先给用户看 dry-run 清单）
Mac 上一般由 dealradar.mac_run 调用；settings.yaml 的 calendar_apply 不是 true 时一律 dry-run。

规则：
- 目标日历「黑五购物」由用户在 iCloud 里手动建。用 EventKit 找：名字是「黑五购物」、账户是 iCloud（CalDAV）的日历
  必须恰好 1 个，否则停下并报出来；「我的 Mac 上」的同名本地日历不算数，只提示可以删掉。
- 增、改、删一律走 EventKit（按上面找到的 calendarIdentifier）。AppleScript 只在 --check 里用：把同名日历的 ID
  打印出来和 EventKit 比对（macOS 26 的「日历」读 calendarIdentifier 会报 -10000，所以不拿它来写）。
- 新增前先在目标日历里按 [dealradar:<id>] 标记找已有事件，找到就接管（不重复新建），calendar_map.json 丢了也不怕。
- 按 uid 拿到的事件（包括 calendar_map 里记的 uid）一律再核对两点：属于目标日历、备注里带这个事件的标记；
  任何一点不满足都当“找不到”，不修改、不删除（不会碰别的日历里同 uid、同标记的事件）。
- 只写有开始日期的活动：起止日期都有的写成跨天全天事件；只有开始日期的写成单日全天事件，标题末尾加“起”。
  清单里的 end_excl 是“最后一天 +1”（iCalendar 的约定）；写进 EventKit 时换成它的约定（见 EventKitStore._fill）。
  日期用 NSDateComponents 分别设年、月、日，不解析日期字符串。
- 每个事件的备注最后一行写 [dealradar:<事件 id>]。本地 data/local/calendar_map.json 记 {id: {uid, hash}}：
  hash（标题、日期、备注）变了才修改；final 里没有这个 id 了才删除，而且只删备注里带这个标记的事件。
- 保护：final 为空，或者要写的活动比上次（calendar_map 里的条数）少了一半以上，这次不做任何删除，并报警。
- 没有权限（EventKit 被拒、osascript 报 -1743）：中文提示同时说「隐私与安全性 → 日历」和「自动化」。
- EventKit 来自 pyobjc-framework-EventKit（只在 Mac 上装）；第一次请求权限时 Mac 会弹窗，属正常。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Callable, Optional

from .build_web import display_name
from .config import Paths, load_config, load_json, save_json
from .obsidian_sync import REGION_FLAG, STATUS_ZH, shop_url

log = logging.getLogger(__name__)

CALENDAR_NAME = "黑五购物"
MARK = "[dealradar:{}]"
MAX_NOTE_SOURCES = 3
SOURCE_TITLE = "iCloud"                 # 只认 iCloud 账户（CalDAV）下的「黑五购物」
EK_SOURCE_LOCAL, EK_SOURCE_CALDAV = 0, 2   # EKSourceTypeLocal / EKSourceTypeCalDAV
MARK_RE = re.compile(r"\[dealradar:([^\]\s]+)\]")
PERMISSION_HINT = ("没有权限读写「日历」。请打开 系统设置 → 隐私与安全性：① 「日历」里给运行这个程序的应用"
                   "（终端 / Python）选“完全访问”；② 「自动化」里在这个应用下面勾选「日历」。然后再运行一次。")


class CalendarError(RuntimeError):
    """日历这一步要停下来的情况（找不到日历、重名、没有权限、osascript 出错）。"""


# ---------- 事件 → 日历条目（纯函数）----------
def _d(s) -> Optional[date]:
    try:
        return date.fromisoformat(str(s)[:10]) if s else None
    except ValueError:
        return None


def writable(e: dict) -> bool:
    """只写有开始日期的活动。"""
    return _d(e.get("start")) is not None


def marker(event_id: str) -> str:
    return MARK.format(event_id)


def entry_for(e: dict, brands: dict) -> dict:
    """一个活动 → {id, title, start, end_excl, open_end, notes, hash}。start / end_excl 是 date。"""
    start = _d(e["start"])
    end = _d(e.get("end"))
    if end is not None and end < start:
        end = None
    brand = brands.get(e["brand_id"]) or {}
    region = e.get("region") or "unknown"
    title = f"{REGION_FLAG.get(region, '❔')} {brand.get('name') or e['brand_id']}｜{display_name(e)}"
    if e.get("status") != "confirmed":
        title += "（传闻）"
    if end is None:
        title += " 起"
    lines = []
    if e.get("discount"):
        lines.append(f"折扣：{e['discount']}")
    lines.append(f"状态：{STATUS_ZH.get(e.get('status'), '传闻')}")
    if end is None:
        lines.append("只知道开始日期，结束日期未定")
    if e.get("date_conflict"):
        lines.append("日期有分歧：" + " / ".join(str(x) for x in e["date_conflict"]))
    shop = shop_url(brand, region) if brand else None
    if shop:
        lines.append(f"官网：{shop}")
    srcs = [s for s in e.get("sources") or [] if s.get("url")][:MAX_NOTE_SOURCES]
    if srcs:
        lines.append("来源：")
        lines += [f"- {s.get('name') or s.get('domain') or '来源'}：{s['url']}" for s in srcs]
    lines += ["", "（Deal Radar 自动同步，改动会在下次同步时被覆盖）", marker(e["id"])]
    notes = "\n".join(lines)
    end_excl = (end or start) + timedelta(days=1)
    h = hashlib.sha256(json.dumps([title, start.isoformat(), end_excl.isoformat(), notes],
                                  ensure_ascii=False).encode("utf-8")).hexdigest()[:16]
    return {"id": e["id"], "title": title, "start": start, "end_excl": end_excl, "open_end": end is None,
            "notes": notes, "hash": h}


@dataclass
class Plan:
    add: list = field(default_factory=list)              # entry
    update: list = field(default_factory=list)           # entry + uid
    delete: list = field(default_factory=list)           # {"id", "uid"}
    unchanged: list = field(default_factory=list)        # id
    held: list = field(default_factory=list)             # final 里还有、但没有开始日期了：日历里的保留不动
    blocked_deletes: list = field(default_factory=list)  # 保护触发时，本来要删的
    warnings: list = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.add or self.update or self.delete)


def plan_sync(final_events: list, brands: dict, cal_map: dict) -> Plan:
    """算出要新增、修改、删除的清单（不碰日历）。"""
    p = Plan()
    want = {}
    for e in final_events:
        if writable(e):
            want[e["id"]] = entry_for(e, brands)
    final_ids = {e["id"] for e in final_events}
    for eid in sorted(want, key=lambda i: (want[i]["start"], i)):
        ent = want[eid]
        old = cal_map.get(eid)
        if old is None:
            p.add.append(ent)
        elif old.get("hash") != ent["hash"]:
            p.update.append(dict(ent, uid=old["uid"]))
        else:
            p.unchanged.append(eid)
    gone = [{"id": i, "uid": v.get("uid")} for i, v in sorted(cal_map.items()) if i not in final_ids]
    p.held = sorted(i for i in cal_map if i in final_ids and i not in want)
    prev = len(cal_map)
    protect = None
    if not final_events:
        protect = "final.json 里一个活动都没有：这次不删除日历里的任何事件（可能是数据出了问题）"
    elif prev and len(want) < prev / 2:
        protect = (f"要写的活动从上次的 {prev} 条降到 {len(want)} 条（少了一半以上）：这次不删除任何事件，"
                   "请检查数据")
    if protect:
        p.warnings.append(protect)
        p.blocked_deletes = gone
    else:
        p.delete = gone
    return p


def _md(d: date) -> str:
    return f"{d.month}/{d.day}"


def _span(ent: dict) -> str:
    last = ent["end_excl"] - timedelta(days=1)
    return _md(ent["start"]) if last == ent["start"] else f"{_md(ent['start'])}–{_md(last)}"


def describe_plan(p: Plan, apply: bool = False) -> list:
    head = "将要写入日历" if apply else "dry-run（不写日历）"
    lines = [f"Mac 日历「{CALENDAR_NAME}」{head}：新增 {len(p.add)}，修改 {len(p.update)}，删除 {len(p.delete)}，"
             f"不变 {len(p.unchanged)}"]
    for w in p.warnings:
        lines.append(f"  ⚠ {w}")
    for ent in p.add:
        lines.append(f"  + 新增 {_span(ent):<11} {ent['title']}")
    for ent in p.update:
        lines.append(f"  ~ 修改 {_span(ent):<11} {ent['title']}")
    for d in p.delete:
        lines.append(f"  - 删除 {d['id']}")
    for d in p.blocked_deletes:
        lines.append(f"  ! 本该删除但被保护拦下：{d['id']}")
    for i in p.held:
        lines.append(f"  = 保留（活动还在，但没有开始日期了）：{i}")
    return lines


# ---------- AppleScript（只在 --check 里显示同名日历的 ID，不用来写）----------
IDS_SCRIPT = """
on run argv
	set calName to item 1 of argv
	set out to ""
	tell application "Calendar"
		repeat with c in (every calendar whose name is calName)
			set out to out & (calendarIdentifier of c) & linefeed
		end repeat
	end tell
	return out
end run
"""

ALL_SCRIPTS = {"ids": IDS_SCRIPT}


def osascript(script: str, args: list, runner: Optional[Callable] = None, timeout: int = 60) -> str:
    """用 osascript 跑一段 AppleScript（脚本从标准输入读，参数走 argv，不拼字符串、不用转义）。"""
    runner = runner or subprocess.run
    try:
        r = runner(["osascript", "-", *[str(a) for a in args]], input=script, capture_output=True, text=True,
                   timeout=timeout)
    except FileNotFoundError as e:
        raise CalendarError("找不到 osascript（日历同步只能在 Mac 上运行）") from e
    except subprocess.TimeoutExpired as e:
        raise CalendarError(f"osascript 超过 {timeout} 秒没有返回") from e
    if r.returncode != 0:
        err = (r.stderr or "").strip()
        if "-1743" in err:
            raise CalendarError(PERMISSION_HINT + "（osascript 报 -1743）")
        raise CalendarError(f"osascript 出错：{err or r.returncode}")
    return (r.stdout or "").strip("\n ")


def applescript_ids(runner=None, name: str = CALENDAR_NAME) -> list:
    """AppleScript 里所有名为「黑五购物」的日历的 calendarIdentifier（只给 --check 显示用）。"""
    return [x.strip() for x in osascript(IDS_SCRIPT, [name], runner).splitlines() if x.strip()]


def parse_marked(pairs) -> tuple:
    """[(uid, 备注)] → ({事件 id: uid}, 重复的事件 id)。同一个标记出现多次，用第一个。"""
    found, dup = {}, []
    for uid, notes in pairs:
        m = MARK_RE.search(notes or "")
        if not m or not uid:
            continue
        if m.group(1) in found:
            dup.append(m.group(1))
            continue
        found[m.group(1)] = uid
    return found, dup


# ---------- EventKit（找 iCloud 日历，增、改、删）----------
class EventKitStore:
    """EventKit 的薄封装（只在 Mac 上用，需要 pyobjc-framework-EventKit）。每个写操作都带目标日历的 cal_id。"""

    def __init__(self, timeout: float = 120):
        try:
            import EventKit
            import Foundation
        except ImportError as e:
            raise CalendarError("没有装 pyobjc-framework-EventKit（.venv312/bin/pip install -r requirements.txt）") from e
        self.EK, self.F = EventKit, Foundation
        self.store = EventKit.EKEventStore.alloc().init()
        self.timeout = timeout

    def ensure_access(self) -> None:
        EK = self.EK
        st = EK.EKEventStore.authorizationStatusForEntityType_(EK.EKEntityTypeEvent)
        if st == EK.EKAuthorizationStatusFullAccess:
            return
        if st in (EK.EKAuthorizationStatusDenied, EK.EKAuthorizationStatusRestricted):
            raise CalendarError(PERMISSION_HINT + "（EventKit：被拒绝）")
        done, res = threading.Event(), {}

        def handler(granted, error):
            res["granted"], res["error"] = bool(granted), error
            done.set()
        log.info("向系统申请「日历」的完全访问权限（Mac 上会弹窗，属正常）")
        if hasattr(self.store, "requestFullAccessToEventsWithCompletion_"):
            self.store.requestFullAccessToEventsWithCompletion_(handler)
        else:
            self.store.requestAccessToEntityType_completion_(EK.EKEntityTypeEvent, handler)
        if not done.wait(self.timeout):
            raise CalendarError(f"等了 {self.timeout:.0f} 秒还没有拿到「日历」权限（弹窗没有点？）。" + PERMISSION_HINT)
        if not res.get("granted"):
            if st == EK.EKAuthorizationStatusWriteOnly:
                raise CalendarError("现在只有“仅添加事件”的日历权限，读不到日历清单，系统也没有再弹窗。请打开 系统设置 → "
                                    "隐私与安全性 → 日历，把运行这个程序的应用（终端、VS Code 等）改成“完全访问”；"
                                    "「自动化」里也在这个应用下面勾选「日历」。然后再运行一次。")
            raise CalendarError(PERMISSION_HINT + f"（EventKit：没有同意，{res.get('error')}）")
        self.store.reset()

    def calendars(self) -> list:
        out = []
        for c in self.store.calendarsForEntityType_(self.EK.EKEntityTypeEvent) or []:
            src = c.source()
            out.append({"title": str(c.title()), "id": str(c.calendarIdentifier()),
                        "source_title": str(src.title()) if src is not None else "",
                        "source_type": int(src.sourceType()) if src is not None else -1})
        return out

    def _cal(self, cal_id: str):
        cal = self.store.calendarWithIdentifier_(cal_id)
        if cal is None:
            raise CalendarError(f"EventKit 找不到 ID 为 {cal_id} 的日历")
        return cal

    @staticmethod
    def _in_calendar(ev, cal_id: str) -> bool:
        cal = ev.calendar()
        return cal is not None and str(cal.calendarIdentifier()) == cal_id

    def _owned(self, cal_id: str, uid: Optional[str], event_id: str):
        """按 uid 拿事件，再核对：属于目标日历、备注里带这个事件的标记。任何一点不满足 → None（当“找不到”）。"""
        ev = self.store.calendarItemWithIdentifier_(uid) if uid else None
        if ev is None or not self._in_calendar(ev, cal_id):
            return None
        if marker(event_id) not in str(ev.notes() or ""):
            return None
        return ev

    def _nsdate(self, d: date, end_of_day: bool = False):
        comps = self.F.NSDateComponents.alloc().init()     # 年、月、日分开设，不解析日期字符串
        comps.setYear_(d.year)
        comps.setMonth_(d.month)
        comps.setDay_(d.day)
        comps.setHour_(23 if end_of_day else 0)
        comps.setMinute_(59 if end_of_day else 0)
        comps.setSecond_(59 if end_of_day else 0)
        return self.F.NSCalendar.currentCalendar().dateFromComponents_(comps)

    def _fill(self, ev, ent: dict) -> None:
        ev.setTitle_(ent["title"])
        ev.setNotes_(ent["notes"])
        ev.setAllDay_(True)
        ev.setStartDate_(self._nsdate(ent["start"]))
        # 全天事件的结束日按 EventKit 的约定写：endDate 落在“最后一天”（这里取最后一天 23:59:59），
        # 不是 iCalendar / AppleScript 那样的“最后一天 +1 的 0 点”。依据：EventKit 读出的全天事件，endDate 就是
        # 最后一天当天（「日历」App 里建的单日全天事件，startDate 和 endDate 是同一天，endDate 为 23:59:59）；
        # 照 +1 写会多出一天。清单里的 end_excl 是 +1 的，所以这里减回一天。
        ev.setEndDate_(self._nsdate(ent["end_excl"] - timedelta(days=1), end_of_day=True))

    def _save(self, ev) -> None:
        ok, err = self.store.saveEvent_span_commit_error_(ev, self.EK.EKSpanThisEvent, True, None)
        if not ok:
            raise CalendarError(f"EventKit 保存事件失败：{err}")

    def find_marked(self, cal_id: str) -> tuple:
        now = self.F.NSDate.date()
        pred = self.store.predicateForEventsWithStartDate_endDate_calendars_(
            now.dateByAddingTimeInterval_(-700 * 86400), now.dateByAddingTimeInterval_(700 * 86400),
            [self._cal(cal_id)])
        return parse_marked([(str(ev.calendarItemIdentifier()), str(ev.notes() or ""))
                             for ev in self.store.eventsMatchingPredicate_(pred) or []
                             if self._in_calendar(ev, cal_id)])

    def create(self, cal_id: str, ent: dict) -> str:
        ev = self.EK.EKEvent.eventWithEventStore_(self.store)
        ev.setCalendar_(self._cal(cal_id))
        self._fill(ev, ent)
        self._save(ev)
        return str(ev.calendarItemIdentifier())

    def update(self, cal_id: str, uid: Optional[str], ent: dict) -> bool:
        ev = self._owned(cal_id, uid, ent["id"])
        if ev is None:
            return False
        self._fill(ev, ent)
        self._save(ev)
        return True

    def delete(self, cal_id: str, uid: Optional[str], event_id: str) -> str:
        ev = self._owned(cal_id, uid, event_id)
        if ev is None:
            return "missing"
        ok, err = self.store.removeEvent_span_commit_error_(ev, self.EK.EKSpanThisEvent, True, None)
        if not ok:
            raise CalendarError(f"EventKit 删除事件失败：{err}")
        return "deleted"


class EventKitBackend:
    """把目标日历的 cal_id 绑在一起，apply_plan 用。"""
    name = "EventKit"

    def __init__(self, ek, cal_id: str):
        self.ek, self.cal_id = ek, cal_id

    def find_marked(self) -> tuple:
        return self.ek.find_marked(self.cal_id)

    def create(self, ent: dict) -> str:
        return self.ek.create(self.cal_id, ent)

    def update(self, uid: Optional[str], ent: dict) -> bool:
        return self.ek.update(self.cal_id, uid, ent)

    def delete(self, uid: Optional[str], event_id: str) -> str:
        return self.ek.delete(self.cal_id, uid, event_id)


def find_target_calendar(ek, name: str = CALENDAR_NAME) -> tuple:
    """EventKit 里找 iCloud 账户下名为「黑五购物」的日历，必须恰好 1 个。返回 (日历信息, 提示)。"""
    ek.ensure_access()
    named = [c for c in ek.calendars() if c["title"] == name]
    icloud = [c for c in named if c["source_type"] == EK_SOURCE_CALDAV and c["source_title"] == SOURCE_TITLE]
    local = [c for c in named if c["source_type"] == EK_SOURCE_LOCAL]
    others = [c for c in named if c not in icloud and c not in local]
    if not icloud:
        msg = f"iCloud 里找不到名为「{name}」的日历。请在「日历」App 左栏 iCloud 下面新建一个，名字写「{name}」。"
        if local:
            msg += f"现在只有「我的 Mac 上」下面有一个同名的本地日历，程序不认它；iCloud 那个建好后，把『我的 Mac 上』下面那个删掉。"
        raise CalendarError(msg)
    if len(icloud) > 1:
        raise CalendarError(f"iCloud 里有 {len(icloud)} 个名为「{name}」的日历，不知道该写哪一个。请删掉或改名，只留一个。")
    notes = []
    if local:
        notes.append(f"「我的 Mac 上」还有一个同名的本地日历「{name}」：程序不写它，可以删掉『我的 Mac 上』下面那个。")
    for c in others:
        notes.append(f"另一个账户「{c['source_title']}」下面也有同名日历：程序不写它。")
    return icloud[0], notes


def describe_check(info: dict) -> list:
    t = info["target"]
    lines = [f"EventKit 找到：账户 {t['source_title']}，名字「{t['title']}」，ID {t['id']}"]
    if info["error"]:
        lines.append(f"AppleScript 查不到日历 ID：{info['error']}"
                     "（macOS 26 的「日历」读 calendarIdentifier 会报 -10000；没有自动化权限会报 -1743）")
    else:
        lines.append("AppleScript 里名为「%s」的日历 ID：%s" % (t["title"], "、".join(info["applescript_ids"]) or "（一个也没有）"))
    lines.append(f"两边 ID {'一致' if info['match'] else '对不上（或无法比对）'}；增、改、删一律走 EventKit")
    lines += [f"提示：{n}" for n in info["notes"]]
    return lines


def apply_plan(p: Plan, cal_map: dict, backend) -> dict:
    """照清单写日历；cal_map 原地更新（调用方负责存盘）。先按 [dealradar:id] 标记找出目标日历里已有的事件：
    新增的找到了就接管（不重复新建）；修改、删除先用标记找到的 uid，没有再用 calendar_map 里的 uid；
    后端会再核对“属于目标日历 + 带标记”，不满足就当找不到（修改改成新建，删除跳过）。返回各项条数。"""
    marked, dup = backend.find_marked()
    st = {"added": 0, "adopted": 0, "updated": 0, "recreated": 0, "deleted": 0, "missing": 0, "duplicates": len(dup)}
    if dup:
        log.warning("日历里有重复标记的事件（只管第一个）：%s", sorted(set(dup)))
    for ent in p.add:
        uid = marked.get(ent["id"])
        if uid and backend.update(uid, ent):
            st["adopted"] += 1
        else:
            uid = backend.create(ent)
            st["added"] += 1
        cal_map[ent["id"]] = {"uid": uid, "hash": ent["hash"]}
    for ent in p.update:
        uid = marked.get(ent["id"]) or ent.get("uid")
        if backend.update(uid, ent):
            st["updated"] += 1
        else:                     # 找不到（被用户删了、或不在目标日历里）：在目标日历里重新建一个
            uid = backend.create(ent)
            st["recreated"] += 1
        cal_map[ent["id"]] = {"uid": uid, "hash": ent["hash"]}
    for d in p.delete:
        uid = marked.get(d["id"]) or d.get("uid")
        r = backend.delete(uid, d["id"]) if uid else "missing"
        st["deleted" if r == "deleted" else "missing"] += 1
        cal_map.pop(d["id"], None)
    return st


def sync(final_events: list, brands: dict, map_path: Path, apply: bool = False, runner=None,
         plan_out: Optional[Path] = None, name: str = CALENDAR_NAME, ek=None,
         confirmed_only: bool = False) -> dict:
    """算清单并打印；apply=True 才写日历（一律走 EventKit）和 calendar_map.json。plan_out：清单另存一份文本。
    dry-run 不碰日历（不调用 EventKit，也不调用 osascript）。runner 参数保留不用（写入不再走 AppleScript）。"""
    cal_map = load_json(map_path, {}) or {}
    if confirmed_only:   # 只写已官宣的；传闻不算在 final 里，以前写过的传闻会被当作“已消失”删掉
        final_events = [e for e in final_events if e.get("status") == "confirmed"]
    p = plan_sync(final_events, brands, cal_map)
    lines = describe_plan(p, apply)
    for line in lines:
        log.info(line)
    if plan_out is not None:
        plan_out.parent.mkdir(parents=True, exist_ok=True)
        plan_out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    out = {"plan": p, "applied": False, "stats": None, "backend": None}
    if not apply:
        return out
    if p.empty:
        log.info("日历：没有要改的")
        out["applied"] = True
        return out
    ek = ek or EventKitStore()
    target, notes = find_target_calendar(ek, name)
    log.info("写入 EventKit：账户 %s，日历「%s」，ID %s", target["source_title"], target["title"], target["id"])
    for n in notes:
        log.info("提示：%s", n)
    backend = EventKitBackend(ek, target["id"])
    out["backend"] = backend.name
    try:
        out["stats"] = apply_plan(p, cal_map, backend)
        out["applied"] = True
    finally:
        save_json(map_path, cal_map)      # 中途出错也记下已经写进去的部分
    log.info("日历已更新（EventKit）：%s", out["stats"])
    return out


def check(runner=None, ek=None, name: str = CALENDAR_NAME) -> dict:
    """只读检查：EventKit 找 iCloud 日历，AppleScript 查同名日历的 ID，两边比对（只是显示，写入一律走 EventKit）。
    EventKit 出错就抛 CalendarError（先把 AppleScript 那边查完、打印出来）；AppleScript 查不到 ID 不算错。"""
    try:
        target, notes = find_target_calendar(ek or EventKitStore(), name)
    except CalendarError as e:
        print(f"EventKit 这边停下了：{e}")
        try:
            ids = applescript_ids(runner, name)
            print("AppleScript 里名为「%s」的日历 ID：%s" % (name, "、".join(ids) or "（一个也没有）"))
        except CalendarError as e2:
            print(f"AppleScript 也查不到日历 ID：{e2}")
        print("两边 ID 是否一致：无法比对（EventKit 那边没有结果）")
        raise
    info = {"target": target, "notes": notes, "applescript_ids": None, "match": False, "error": None,
            "backend": "EventKit"}
    try:
        info["applescript_ids"] = applescript_ids(runner, name)
        info["match"] = target["id"] in info["applescript_ids"]
    except CalendarError as e:
        info["error"] = str(e)
    for line in describe_check(info):
        print(line)
    return info


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="把 final.json 同步进 Mac「日历」的「黑五购物」日历（默认 dry-run）")
    ap.add_argument("--final", type=Path, default=None, help="final.json（默认 data/local/final.json）")
    ap.add_argument("--apply", action="store_true", help="真的写日历（不加就是 dry-run，只打印清单）")
    ap.add_argument("--check", action="store_true", help="只读检查：找 iCloud 的「黑五购物」，比对 EventKit 和 AppleScript 的 ID")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
    paths = Paths()
    try:
        if args.check:
            check()
            return 0
        final_path = args.final or paths.final
        doc = load_json(final_path, None)
        if doc is None:
            print(f"找不到 {final_path}（先运行 python -m dealradar.combine）")
            return 2
        cfg = load_config(paths)
        apply = args.apply and cfg.settings.get("calendar_apply") is True
        if args.apply and not apply:
            print("settings 里 calendar_apply 不是 true，按 dry-run 运行")
        sync(doc.get("events") or [], cfg.brands, paths.calendar_map, apply=apply,
             confirmed_only=cfg.settings.get("calendar_confirmed_only") is True,
             plan_out=paths.local_dir / "calendar_dryrun.txt")
    except CalendarError as e:
        print(f"日历同步停下了：{e}")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
