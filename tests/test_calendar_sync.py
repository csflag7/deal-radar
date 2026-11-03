"""Mac 日历同步（calendar_sync）：清单（新增 / 修改 / 删除 / 保护）；写入一律走 EventKit。
EventKit / Foundation 换成假的模块（patch sys.modules），用真的 EventKitStore 跑；osascript 也是假的。"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import types
from datetime import date, datetime, timedelta

import pytest

from dealradar import calendar_sync as cs

pytestmark = pytest.mark.mac
on_mac = pytest.mark.skipif(sys.platform != "darwin", reason="只在 macOS 上跑")

ICLOUD_ID, LOCAL_ID = "ICLOUD-CAL-1", "LOCAL-CAL-1"


def ev(eid="nike-US-black_friday-2026", brand="nike", start="2026-11-20", end="2026-11-30", status="confirmed",
       **kw):
    e = {"id": eid, "brand_id": brand, "region": "US", "event_type": "black_friday", "event_name": None,
         "start": start, "end": end, "discount": "up to 50% off", "status": status,
         "sources": [{"name": "CNBC", "url": "https://www.cnbc.com/nike", "domain": "cnbc.com"}]}
    e.update(kw)
    return e


# ---------- 假的 EventKit / Foundation ----------
class World:
    """假 EventKit 背后的“系统”：权限状态、日历、事件。"""

    def __init__(self, status=3, grant=True, cals=(("黑五购物", ICLOUD_ID, "iCloud", 2),)):
        self.status, self.grant = status, grant          # grant=None：申请权限后永远不回调（等超时）
        self.cals = [FakeCalendar(cid, title, FakeSource(st, tp)) for title, cid, st, tp in cals]
        self.events, self.saved, self.removed, self.requests, self.resets, self.n = {}, [], [], 0, 0, 0

    def cal(self, cid):
        return next((c for c in self.cals if c.calendarIdentifier() == cid), None)

    def put(self, uid, cid, notes, title="手动建的"):
        e = FakeEvent()
        e.uid, e.cal, e.n, e.t = uid, self.cal(cid), notes, title
        self.events[uid] = e
        return e


class FakeSource:
    def __init__(self, title, typ):
        self._t, self._typ = title, typ

    def title(self):
        return self._t

    def sourceType(self):
        return self._typ


class FakeCalendar:
    def __init__(self, cid, title, source):
        self._id, self._t, self._s = cid, title, source

    def calendarIdentifier(self):
        return self._id

    def title(self):
        return self._t

    def source(self):
        return self._s


class FakeEvent:
    def __init__(self):
        self.uid = self.cal = self.n = self.t = self.s = self.e = None
        self.allday = False

    def setTitle_(self, v):
        self.t = v

    def setNotes_(self, v):
        self.n = v

    def setAllDay_(self, v):
        self.allday = v

    def setStartDate_(self, v):
        self.s = v

    def setEndDate_(self, v):
        self.e = v

    def setCalendar_(self, c):
        self.cal = c

    def calendar(self):
        return self.cal

    def notes(self):
        return self.n

    def calendarItemIdentifier(self):
        return self.uid


class FakeStore:
    def __init__(self, w):
        self.w = w

    def requestFullAccessToEventsWithCompletion_(self, handler):
        self.w.requests += 1
        if self.w.grant is None:
            return
        if self.w.grant:
            self.w.status = 3
        threading.Thread(target=handler, args=(self.w.grant, None)).start()

    def reset(self):
        self.w.resets += 1

    def calendarsForEntityType_(self, t):
        return list(self.w.cals)

    def calendarWithIdentifier_(self, cid):
        return self.w.cal(cid)

    def predicateForEventsWithStartDate_endDate_calendars_(self, a, b, cals):
        return [c.calendarIdentifier() for c in cals]

    def eventsMatchingPredicate_(self, pred):
        return [e for e in self.w.events.values() if e.cal and e.cal.calendarIdentifier() in pred]

    def calendarItemWithIdentifier_(self, uid):
        return self.w.events.get(uid)

    def saveEvent_span_commit_error_(self, ev, span, commit, err):
        if ev.uid is None:
            self.w.n += 1
            ev.uid = f"EK-{self.w.n}"
            self.w.events[ev.uid] = ev
        self.w.saved.append(ev.uid)
        return True, None

    def removeEvent_span_commit_error_(self, ev, span, commit, err):
        del self.w.events[ev.uid]
        self.w.removed.append(ev.uid)
        return True, None


class FakeNSDate:
    def __init__(self, dt):
        self.dt = dt

    @classmethod
    def date(cls):
        return cls(datetime(2026, 11, 3))

    def dateByAddingTimeInterval_(self, sec):
        return FakeNSDate(self.dt + timedelta(seconds=sec))


class FakeComps:
    def __init__(self):
        self.v = {}

    def __getattr__(self, name):
        if name.startswith("set") and name.endswith("_"):
            return lambda x: self.v.__setitem__(name[3:-1].lower(), x)
        raise AttributeError(name)


def fake_modules(w):
    ek = types.ModuleType("EventKit")
    ek.EKEntityTypeEvent, ek.EKSpanThisEvent = 0, 0
    (ek.EKAuthorizationStatusNotDetermined, ek.EKAuthorizationStatusRestricted, ek.EKAuthorizationStatusDenied,
     ek.EKAuthorizationStatusFullAccess, ek.EKAuthorizationStatusWriteOnly) = 0, 1, 2, 3, 4

    class EKEventStore:
        @staticmethod
        def alloc():
            return types.SimpleNamespace(init=lambda: FakeStore(w))

        @staticmethod
        def authorizationStatusForEntityType_(t):
            return w.status
    ek.EKEventStore = EKEventStore
    ek.EKEvent = types.SimpleNamespace(eventWithEventStore_=lambda store: FakeEvent())
    fd = types.ModuleType("Foundation")
    fd.NSDate = FakeNSDate
    fd.NSDateComponents = types.SimpleNamespace(alloc=lambda: types.SimpleNamespace(init=FakeComps))
    cal = types.SimpleNamespace(dateFromComponents_=lambda c: datetime(c.v["year"], c.v["month"], c.v["day"],
                                                                       c.v["hour"], c.v["minute"], c.v["second"]))
    fd.NSCalendar = types.SimpleNamespace(currentCalendar=lambda: cal)
    return ek, fd


@pytest.fixture
def world(monkeypatch):
    w = World()
    ek, fd = fake_modules(w)
    monkeypatch.setitem(sys.modules, "EventKit", ek)
    monkeypatch.setitem(sys.modules, "Foundation", fd)
    return w


class FakeOsa:
    """假的 osascript（只有 --check 用到：列出同名日历的 ID）。"""

    def __init__(self, ids=(ICLOUD_ID,), err=None):
        self.ids, self.err, self.calls = list(ids), err, []

    def __call__(self, cmd, input=None, capture_output=True, text=True, timeout=None):
        assert cmd[:2] == ["osascript", "-"] and input == cs.IDS_SCRIPT
        self.calls.append(cmd[2:])
        if self.err:
            return subprocess.CompletedProcess(cmd, 1, "", self.err)
        return subprocess.CompletedProcess(cmd, 0, "\n".join(self.ids) + "\n", "")


def no_osa(*a, **k):
    raise AssertionError("写日历不该调用 osascript")


def store(timeout=5):
    return cs.EventKitStore(timeout=timeout)


def test_entry_dates_title_and_marker(cfg):
    a = cs.entry_for(ev(), cfg.brands)
    assert (a["start"], a["end_excl"]) == (date(2026, 11, 20), date(2026, 12, 1))     # 全天事件结束日 +1
    assert a["title"] == "🇺🇸 Nike｜黑五" and not a["open_end"]
    assert a["notes"].splitlines()[-1] == "[dealradar:nike-US-black_friday-2026]"      # 备注最后一行是标记
    assert "折扣：up to 50% off" in a["notes"] and "https://www.cnbc.com/nike" in a["notes"]
    b = cs.entry_for(ev(end=None, status="rumored"), cfg.brands)                       # 只有开始日期：单日，标题加“起”
    assert (b["start"], b["end_excl"]) == (date(2026, 11, 20), date(2026, 11, 21))
    assert b["title"] == "🇺🇸 Nike｜黑五（传闻） 起" and b["open_end"]
    assert cs.entry_for(ev(end="2026-11-01"), cfg.brands)["open_end"]                  # 结束早于开始：当作没有结束日
    assert a["hash"] != b["hash"] and cs.entry_for(ev(), cfg.brands)["hash"] == a["hash"]
    assert not cs.writable(ev(start=None)) and cs.writable(ev(end=None))


def test_plan_add_update_delete_unchanged_held(cfg):
    keep, change, gone, nodate = (ev("a-US-black_friday-2026"), ev("b-US-black_friday-2026"),
                                  ev("c-US-black_friday-2026"), ev("d-US-black_friday-2026"))
    cal_map = {e["id"]: {"uid": f"U{e['id'][0]}", "hash": cs.entry_for(e, cfg.brands)["hash"]}
               for e in (keep, change, gone, nodate)}
    final = [keep, dict(change, start="2026-11-21"), dict(nodate, start=None), ev("new-US-black_friday-2026")]
    p = cs.plan_sync(final, cfg.brands, cal_map)
    assert [x["id"] for x in p.add] == ["new-US-black_friday-2026"]
    assert [(x["id"], x["uid"]) for x in p.update] == [("b-US-black_friday-2026", "Ub")]
    assert p.delete == [{"id": "c-US-black_friday-2026", "uid": "Uc"}]                 # final 里没有了才删
    assert p.unchanged == ["a-US-black_friday-2026"] and p.held == ["d-US-black_friday-2026"]
    assert not p.warnings and not p.blocked_deletes
    lines = cs.describe_plan(p)
    assert lines[0].startswith("Mac 日历「黑五购物」dry-run（不写日历）：新增 1，修改 1，删除 1，不变 1")
    assert any(l.strip().startswith("+ 新增 11/20–11/30") for l in lines)


def test_protection_empty_or_big_drop_blocks_deletes(cfg):
    cal_map = {f"e{i}": {"uid": f"U{i}", "hash": "x"} for i in range(6)}
    p = cs.plan_sync([], cfg.brands, cal_map)
    assert p.delete == [] and len(p.blocked_deletes) == 6 and "一个活动都没有" in p.warnings[0]
    two = [ev(f"e{i}") for i in range(2)]                     # 6 → 2：少了一半以上
    p = cs.plan_sync(two, cfg.brands, cal_map)
    assert p.delete == [] and len(p.blocked_deletes) == 4 and "少了一半以上" in p.warnings[0]
    three = [ev(f"e{i}") for i in range(3)]                   # 6 → 3：正好一半，不算
    p = cs.plan_sync(three, cfg.brands, cal_map)
    assert len(p.delete) == 3 and not p.warnings


def test_dry_run_does_not_touch_calendar_or_map(cfg, tmp_path):
    osa = FakeOsa()
    map_path, plan_out = tmp_path / "calendar_map.json", tmp_path / "calendar_dryrun.txt"
    res = cs.sync([ev()], cfg.brands, map_path, apply=False, runner=osa, plan_out=plan_out)
    assert osa.calls == [] and not map_path.exists() and not res["applied"]
    assert "新增 1" in plan_out.read_text(encoding="utf-8")


# ---------- EventKit 写入 ----------
def test_all_day_end_uses_eventkit_convention(world):
    """EventKit 的全天事件：endDate 落在最后一天（23:59:59），不是 +1 天的 0 点。"""
    ent = cs.entry_for(ev(), {})
    st = store()
    uid = st.create(ICLOUD_ID, ent)
    e = world.events[uid]
    assert e.allday is True and e.cal.calendarIdentifier() == ICLOUD_ID
    assert (e.s, e.e) == (datetime(2026, 11, 20, 0, 0, 0), datetime(2026, 11, 30, 23, 59, 59))
    one = cs.entry_for(ev("gap-US-black_friday-2026", end=None), {})            # 单日：开始、结束是同一天
    e1 = world.events[st.create(ICLOUD_ID, one)]
    assert (e1.s.date(), e1.e.date()) == (date(2026, 11, 20), date(2026, 11, 20))
    assert e.n.endswith("[dealradar:nike-US-black_friday-2026]")


def test_never_touches_other_calendars_or_unmarked_events(world):
    world.cals.append(FakeCalendar(LOCAL_ID, "黑五购物", FakeSource("我的 Mac 上", 0)))
    mark = cs.marker("nike-US-black_friday-2026")
    world.put("SAME", LOCAL_ID, f"别的日历里\n{mark}")                     # 别的日历：同 uid、同标记
    world.put("NOMARK", ICLOUD_ID, "目标日历里，但没有标记")
    st = store()
    ent = cs.entry_for(ev(), {})
    assert st.update(ICLOUD_ID, "SAME", ent) is False and world.events["SAME"].n.startswith("别的日历里")
    assert st.delete(ICLOUD_ID, "SAME", "nike-US-black_friday-2026") == "missing" and "SAME" in world.events
    assert st.update(ICLOUD_ID, "NOMARK", ent) is False and st.delete(ICLOUD_ID, "NOMARK", ent["id"]) == "missing"
    assert st.find_marked(ICLOUD_ID) == ({}, [])                            # 只看目标日历
    assert world.saved == [] and world.removed == []


def test_permission_denied_writeonly_and_timeout_raise(world):
    world.status = 2                                                        # 被拒绝
    with pytest.raises(cs.CalendarError, match="隐私与安全性"):
        store().ensure_access()
    world.status, world.grant = 4, False                                    # 只有“仅添加事件”权限，申请完全访问被拒
    with pytest.raises(cs.CalendarError, match="完全访问"):
        store().ensure_access()
    world.status, world.grant = 0, None                                     # 弹窗一直没人点
    with pytest.raises(cs.CalendarError, match="还没有拿到"):
        store(timeout=0.05).ensure_access()
    world.status, world.grant = 0, True                                     # 同意了
    store().ensure_access()
    assert world.resets == 1 and world.requests == 3


def test_add_update_delete_and_adopt_all_go_through_eventkit(cfg, tmp_path, world):
    map_path = tmp_path / "calendar_map.json"
    final = [ev(), ev("gap-US-black_friday-2026", brand="gap", end=None)]
    res = cs.sync(final, cfg.brands, map_path, apply=True, runner=no_osa, ek=store())
    assert res["backend"] == "EventKit" and res["stats"]["added"] == 2
    m = json.loads(map_path.read_text(encoding="utf-8"))
    nike_uid, gap_uid = m["nike-US-black_friday-2026"]["uid"], m["gap-US-black_friday-2026"]["uid"]
    assert {e.cal.calendarIdentifier() for e in world.events.values()} == {ICLOUD_ID}
    # 没变化：不碰日历
    world.saved.clear()
    cs.sync(final, cfg.brands, map_path, apply=True, runner=no_osa, ek=store())
    assert world.saved == []
    # 改日期 → 修改；gap 从 final 里没了 → 删除
    res = cs.sync([ev(end="2026-12-02")], cfg.brands, map_path, apply=True, runner=no_osa, ek=store())
    assert (res["stats"]["updated"], res["stats"]["deleted"]) == (1, 1)
    assert world.saved == [nike_uid] and world.removed == [gap_uid]
    assert world.events[nike_uid].e == datetime(2026, 12, 2, 23, 59, 59)
    # calendar_map 丢了：按标记接管，不重复新建
    map_path.unlink()
    res = cs.sync([ev(discount="up to 60% off")], cfg.brands, map_path, apply=True, runner=no_osa, ek=store())
    assert (res["stats"]["adopted"], res["stats"]["added"]) == (1, 0) and len(world.events) == 1
    assert "60%" in world.events[nike_uid].n
    assert json.loads(map_path.read_text(encoding="utf-8"))["nike-US-black_friday-2026"]["uid"] == nike_uid


def test_map_uid_pointing_elsewhere_is_not_modified(cfg, tmp_path, world):
    """calendar_map 里的 uid 指向别的日历里的同标记事件：当找不到，在目标日历里重建，别的日历不动。"""
    world.cals.append(FakeCalendar(LOCAL_ID, "黑五购物", FakeSource("我的 Mac 上", 0)))
    mark = cs.marker("nike-US-black_friday-2026")
    world.put("LOCAL-EV", LOCAL_ID, f"旧的\n{mark}")
    world.put("LOCAL-GONE", LOCAL_ID, f"旧的\n{cs.marker('zz-US-black_friday-2026')}")
    map_path = tmp_path / "calendar_map.json"
    map_path.write_text(json.dumps({"nike-US-black_friday-2026": {"uid": "LOCAL-EV", "hash": "x"},
                                    "zz-US-black_friday-2026": {"uid": "LOCAL-GONE", "hash": "y"}}), encoding="utf-8")
    res = cs.sync([ev()], cfg.brands, map_path, apply=True, runner=no_osa, ek=store())
    assert (res["stats"]["recreated"], res["stats"]["missing"]) == (1, 1)
    assert world.events["LOCAL-EV"].n == f"旧的\n{mark}" and "LOCAL-GONE" in world.events
    new_uid = json.loads(map_path.read_text(encoding="utf-8"))["nike-US-black_friday-2026"]["uid"]
    assert world.events[new_uid].cal.calendarIdentifier() == ICLOUD_ID


def test_only_icloud_calendar_counts(world, capsys):
    world.cals.insert(0, FakeCalendar(LOCAL_ID, "黑五购物", FakeSource("我的 Mac 上", 0)))
    info = cs.check(runner=FakeOsa(ids=[ICLOUD_ID, LOCAL_ID]), ek=store())
    out = capsys.readouterr().out
    assert info["target"]["id"] == ICLOUD_ID and info["match"] and info["backend"] == "EventKit"
    assert f"账户 iCloud，名字「黑五购物」，ID {ICLOUD_ID}" in out and "两边 ID 一致" in out
    assert "删掉『我的 Mac 上』下面那个" in out                              # 本地同名日历：只提示
    world.cals = [c for c in world.cals if c.calendarIdentifier() == LOCAL_ID]
    with pytest.raises(cs.CalendarError, match="iCloud 里找不到.*『我的 Mac 上』下面那个删掉"):
        cs.check(runner=FakeOsa(), ek=store())
    world.cals = [FakeCalendar(ICLOUD_ID, "黑五购物", FakeSource("iCloud", 2)),
                  FakeCalendar("ICLOUD-CAL-2", "黑五购物", FakeSource("iCloud", 2))]
    with pytest.raises(cs.CalendarError, match="iCloud 里有 2 个"):
        cs.check(runner=FakeOsa(), ek=store())
    world.cals = [FakeCalendar("G", "黑五购物", FakeSource("Google", 2))]   # 别的 CalDAV 账户不算 iCloud
    with pytest.raises(cs.CalendarError, match="iCloud 里找不到"):
        cs.check(runner=FakeOsa(), ek=store())


def test_check_reports_both_sides(world, capsys):
    world.status, world.grant = 4, False
    with pytest.raises(cs.CalendarError, match="完全访问"):
        cs.check(runner=FakeOsa(ids=["A1", "A2"]), ek=store())
    out = capsys.readouterr().out
    assert "EventKit 这边停下了" in out and "A1、A2" in out and "无法比对" in out
    world.status = 3
    for err in ("(-1743)", "AppleEvent处理程序失败。 (-10000)"):         # AppleScript 拿不到 ID：不算错
        info = cs.check(runner=FakeOsa(err=err), ek=store())
        assert info["error"] and not info["match"]
    assert "一律走 EventKit" in capsys.readouterr().out


def test_calendar_problems_stop_before_writing(cfg, tmp_path, world):
    world.cals = [FakeCalendar(LOCAL_ID, "黑五购物", FakeSource("我的 Mac 上", 0))]
    with pytest.raises(cs.CalendarError, match="iCloud 里找不到"):
        cs.sync([ev()], cfg.brands, tmp_path / "m.json", apply=True, runner=no_osa, ek=store())
    assert world.saved == [] and not (tmp_path / "m.json").exists()


def test_osascript_permission_hint():
    with pytest.raises(cs.CalendarError) as ei:
        cs.osascript(cs.IDS_SCRIPT, ["黑五购物"], FakeOsa(err="execution error: Not authorized (-1743)"))
    assert "-1743" in str(ei.value) and "「日历」" in str(ei.value) and "「自动化」" in str(ei.value)


def test_cli_apply_needs_calendar_apply_setting(monkeypatch, capsys, tmp_path):
    """单独运行 --apply：settings 的 calendar_apply 不是 true 就按 dry-run。"""
    calls = []
    monkeypatch.setattr(cs, "sync", lambda *a, **k: calls.append(k["apply"]))
    monkeypatch.setattr(cs, "load_json", lambda p, d: {"events": []})
    monkeypatch.setattr(cs, "load_config", lambda p: types.SimpleNamespace(settings={"calendar_apply": False},
                                                                           brands={}))
    assert cs.main(["--apply"]) == 0 and calls == [False]
    assert "calendar_apply 不是 true，按 dry-run 运行" in capsys.readouterr().out
    monkeypatch.setattr(cs, "load_config", lambda p: types.SimpleNamespace(settings={"calendar_apply": True},
                                                                           brands={}))
    assert cs.main(["--apply"]) == 0 and calls == [False, True]
    assert cs.main([]) == 0 and calls == [False, True, False]


def test_parse_marked():
    found, dup = cs.parse_marked([("U1", "a\n[dealradar:x-1]"), ("U2", "b"), ("U3", "[dealradar:x-1]"),
                                  ("U4", "前面\n[dealradar:y-2]\n后面")])
    assert found == {"x-1": "U1", "y-2": "U4"} and dup == ["x-1"]


@on_mac
def test_applescript_compiles():
    """只编译、不运行（不会打开「日历」，也不需要自动化权限）。"""
    r = subprocess.run(["osacompile", "-o", "/dev/null", "-"], input=cs.IDS_SCRIPT, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_confirmed_only_filters_rumored(tmp_path):
    """用户 2026-11-03：日历只写已官宣；以前写过的传闻会被删除。"""
    ev = [
        {"id": "a-US-black_friday-2026", "brand_id": "a", "region": "US", "event_type": "black_friday",
         "start": "2026-11-27", "end": "2026-11-30", "status": "confirmed"},
        {"id": "b-US-black_friday-2026", "brand_id": "b", "region": "US", "event_type": "black_friday",
         "start": "2026-11-27", "end": None, "status": "rumored"},
    ]
    map_path = tmp_path / "map.json"
    map_path.write_text('{"b-US-black_friday-2026": {"uid": "u1", "hash": "x"}, '
                        '"a-US-black_friday-2026": {"uid": "u0", "hash": "y"}}', encoding="utf-8")
    res = cs.sync(ev, {}, map_path, apply=False, confirmed_only=True)
    p = res["plan"]
    assert [e["id"] for e in p.update] == ["a-US-black_friday-2026"]
    assert [d["id"] for d in p.delete] == ["b-US-black_friday-2026"]
