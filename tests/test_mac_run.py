"""Mac 本地入口（mac_run）：锁、2.5 小时跳过、取云端新闻、各步骤串起来、8 点规则、通知；launchd 配置。
官网监控、git、购物车、通知全部换成假的。"""
from __future__ import annotations

import importlib.util
import json
import plistlib
import shutil
import subprocess
import types
from datetime import datetime, timedelta, timezone

import pytest

from conftest import APP
from dealradar import mac_run as mr
from dealradar.config import Paths
from dealradar.merge import merge_candidates

pytestmark = pytest.mark.mac

T1 = "2026-11-01T12:00:00+00:00"
# 多伦多 11 月是 EST（UTC-5）
AT_0759 = datetime(2026, 11, 20, 12, 59, tzinfo=timezone.utc)
AT_0800 = datetime(2026, 11, 20, 13, 0, tzinfo=timezone.utc)
DIGEST = {"subject": "Deal Radar 汇总", "body": "Nike 明天开始"}


# ---------- 锁、跳过 ----------
def test_lock_is_exclusive(tmp_path):
    p = tmp_path / "local" / "mac_run.lock"
    a = mr.acquire_lock(p)
    assert a is not None
    assert mr.acquire_lock(p) is None                    # 上一次还在跑：拿不到
    mr.release_lock(a)
    b = mr.acquire_lock(p)
    assert b is not None
    mr.release_lock(b)


def test_skip_reason():
    now = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
    iso = lambda dt: dt.isoformat(timespec="seconds")      # noqa: E731
    assert "不到 2.5 小时" in mr.skip_reason({"last_success": iso(now - timedelta(hours=2))}, now)
    assert mr.skip_reason({"last_success": iso(now - timedelta(hours=2, minutes=31))}, now) is None
    assert mr.skip_reason({"last_success": iso(now - timedelta(hours=1))}, now, force=True) is None
    assert mr.skip_reason({}, now) is None
    assert mr.skip_reason({"last_success": "坏的"}, now) is None
    assert mr.skip_reason({"last_success": iso(now + timedelta(days=1))}, now) is None   # 时间记错了：照常跑


# ---------- 取云端新闻 ----------
class FakeGit:
    def __init__(self, top=None, remote=True, fetch_ok=True, show=None):
        self.top, self.remote, self.fetch_ok, self.show = top, remote, fetch_ok, show
        self.calls = []

    def __call__(self, cmd, capture_output=True, text=True, timeout=None, env=None):
        assert cmd[0] == "git" and cmd[1] == "-C" and env["GIT_TERMINAL_PROMPT"] == "0"
        sub = cmd[3:]
        self.calls.append(sub[0])
        ok = lambda out="": subprocess.CompletedProcess(cmd, 0, out, "")          # noqa: E731
        bad = lambda err="fatal": subprocess.CompletedProcess(cmd, 128, "", err)  # noqa: E731
        if sub[0] == "rev-parse":
            return ok(str(self.top) + "\n") if self.top else bad("not a git repository")
        if sub[0] == "remote":
            return ok("https://github.com/x/y.git\n") if self.remote else bad("No such remote")
        if sub[0] == "fetch":
            return ok() if self.fetch_ok else bad("Could not resolve host")
        if sub[0] == "show":
            assert sub[1] == "origin/main:data/news/sales.json"
            return ok(self.show) if self.show is not None else bad("does not exist")
        raise AssertionError(sub)


def test_refresh_news_cache(tmp_path):
    cache = tmp_path / "local" / "news_cache.json"
    assert mr.refresh_news_cache(tmp_path, cache, FakeGit())[0] == "skip"                  # 还不是 git 仓库
    assert mr.refresh_news_cache(tmp_path, cache, FakeGit(top=tmp_path.parent))[0] == "skip"   # 上层是别的仓库
    g = FakeGit(top=tmp_path, remote=False)
    assert mr.refresh_news_cache(tmp_path, cache, g) == ("skip", "还没有远端仓库 origin，跳过取云端新闻")
    assert "fetch" not in g.calls
    assert mr.refresh_news_cache(tmp_path, cache, FakeGit(top=tmp_path, fetch_ok=False))[0] == "fail"
    assert mr.refresh_news_cache(tmp_path, cache, FakeGit(top=tmp_path, show="不是 JSON"))[0] == "fail"
    assert not cache.exists()                                                             # 失败不动缓存
    doc = {"updated": T1, "events": [{"id": "x"}]}
    g = FakeGit(top=tmp_path, show=json.dumps(doc))
    st, msg = mr.refresh_news_cache(tmp_path, cache, g)
    assert st == "ok" and "1 个事件" in msg and json.loads(cache.read_text(encoding="utf-8")) == doc
    assert g.calls == ["rev-parse", "remote", "fetch", "show"]                             # 只读远端，不 pull


def test_refresh_news_cache_real_git_outside_repo(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("没有 git")
    st, _ = mr.refresh_news_cache(tmp_path, tmp_path / "c.json")
    assert st == "skip"


# ---------- 8 点规则 ----------
class Sender:
    """假的 send_digest：记下调用；result 可以是 dict，也可以是要抛出的异常。"""

    def __init__(self, result=None):
        self.calls, self.result = [], result

    def __call__(self, subject, body, preview, settings=None):
        self.calls.append((subject, preview, settings))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result if self.result is not None else {"sent": not preview, "preview": preview}


class MailError(Exception):
    """和 dealradar.cart.mail.MailError 同名：表示明确没发出去。"""


def test_digest_8am_rule_and_preview(tmp_path):
    sent_path = tmp_path / "digest_sent.json"
    send = Sender()
    s = {"timezone": "America/Toronto", "mail_live": False}
    msg, bad = mr.maybe_send_digest(DIGEST, AT_0759, s, sent_path, send)
    assert "还没到 8 点" in msg and not bad and send.calls == []
    assert mr.maybe_send_digest(None, AT_0800, s, sent_path, send)[0] == "今天没有要发的汇总" and send.calls == []
    msg, bad = mr.maybe_send_digest(DIGEST, AT_0800, s, sent_path, send)
    assert "预览" in msg and not bad and send.calls == [("Deal Radar 汇总", True, s)]   # settings 一起传给发信
    assert not sent_path.exists()                                       # 预览不写 digest_sent
    s_str = {"timezone": "America/Toronto", "mail_live": "true"}      # 只认 YAML 的 true，字符串不算
    mr.maybe_send_digest(DIGEST, AT_0800, s_str, sent_path, send)
    assert send.calls[-1][1] is True and not sent_path.exists()
    live = {"timezone": "America/Toronto", "mail_live": True}
    assert mr.maybe_send_digest(DIGEST, AT_0800, live, sent_path, send) == ("已发送", False)
    assert send.calls[-1][1] is False
    rec = json.loads(sent_path.read_text(encoding="utf-8"))
    assert (rec["date"], rec["state"]) == ("2026-11-20", "sent")
    n = len(send.calls)
    assert "已经发过" in mr.maybe_send_digest(DIGEST, AT_0800 + timedelta(hours=3), live, sent_path, send)[0]
    assert len(send.calls) == n
    # 第二天 8 点以后又可以发
    assert mr.maybe_send_digest(DIGEST, AT_0800 + timedelta(days=1), live, sent_path, send)[0] == "已发送"
    assert "还没有" in mr.maybe_send_digest(DIGEST, AT_0800, live, sent_path, None)[0]


def test_only_sent_true_counts_as_sent(tmp_path):
    """send_digest 返回 sent is True 才算发出；其余（ok、没有 error 的 dict）都不记。"""
    sent_path = tmp_path / "digest_sent.json"
    live = {"timezone": "America/Toronto", "mail_live": True}
    for res in ({"ok": True}, {"sent": "yes"}, {"sent": False, "reason": "没有收件人"}, {}):
        msg, bad = mr.maybe_send_digest(DIGEST, AT_0800, live, sent_path, Sender(res))
        assert msg.startswith("没有发出") and bad and not sent_path.exists(), res
    msg, bad = mr.maybe_send_digest(DIGEST, AT_0800, live, sent_path,
                                    Sender({"sent": False, "reason": "2026-11-20 已经发过汇总"}))
    assert not bad and not sent_path.exists()                           # 购物车那边已经发过：不算问题


def test_sending_marker_when_result_unknown(tmp_path):
    """先记 sending 再发信：明确失败就撤掉；结果不明（发信后写文件出错等）就留着，当天不再自动重发。"""
    sent_path = tmp_path / "digest_sent.json"
    live = {"timezone": "America/Toronto", "mail_live": True}
    seen = []

    def peek(subject, body, preview):
        seen.append(json.loads(sent_path.read_text(encoding="utf-8"))["state"])
        return {"sent": True}
    mr.maybe_send_digest(DIGEST, AT_0800, live, sent_path, peek)
    assert seen == ["sending"]                                          # 发信时已经记了 sending
    assert json.loads(sent_path.read_text(encoding="utf-8"))["state"] == "sent"
    sent_path.unlink()
    msg, bad = mr.maybe_send_digest(DIGEST, AT_0800, live, sent_path, Sender(MailError("「邮件」App 发送失败")))
    assert "发送失败" in msg and bad and not sent_path.exists()          # 明确没发出去：撤掉标记
    msg, bad = mr.maybe_send_digest(DIGEST, AT_0800, live, sent_path, Sender(OSError("磁盘满了")))
    assert "结果不明" in msg and bad
    assert json.loads(sent_path.read_text(encoding="utf-8"))["state"] == "sending"
    again = Sender()
    msg, bad = mr.maybe_send_digest(DIGEST, AT_0800 + timedelta(hours=3), live, sent_path, again)
    assert again.calls == [] and bad and "已发送" in msg                # 不自动重发，提醒去看已发送邮件
    sent_path.unlink()
    msg, bad = mr.maybe_send_digest(DIGEST, AT_0800, live, sent_path,
                                    Sender({"sent": False, "uncertain": True, "reason": "上次可能已发"}))
    assert bad and "不明" in msg and json.loads(sent_path.read_text(encoding="utf-8"))["state"] == "uncertain"


def test_skip_rule_ignores_interval_when_digest_due(tmp_path):
    sent_path = tmp_path / "digest_sent.json"
    recent = {"last_success": (AT_0800 - timedelta(hours=1)).isoformat(timespec="seconds")}
    assert mr.digest_due(AT_0800, sent_path) and not mr.digest_due(AT_0759, sent_path)
    assert mr.skip_reason(recent, AT_0800, digest_is_due=True) is None
    assert mr.skip_reason(recent, AT_0759, digest_is_due=mr.digest_due(AT_0759, sent_path))
    sent_path.write_text(json.dumps({"date": "2026-11-20", "state": "sent"}), encoding="utf-8")
    assert not mr.digest_due(AT_0800, sent_path)                       # 今天发过了：照常按 2.5 小时


# ---------- 购物车接口 ----------
def test_cart_function_or_module():
    def run(final, now, settings):
        return {}
    mod = types.ModuleType("m")
    mod.run = run
    assert mr._fn(run, "run") is run and mr._fn(mod, "run") is run


# ---------- 整个流程 ----------
@pytest.fixture
def env(tmp_path, monkeypatch, make_candidate):
    conf = tmp_path / "config"
    shutil.copytree(APP / "config", conf)
    (conf / "overrides.yaml").write_text("events: {}\n", encoding="utf-8")
    (conf / "manual_events.yaml").write_text("events: []\n", encoding="utf-8")
    paths = Paths(config_dir=conf, data_dir=tmp_path / "data")
    news = merge_candidates([], [make_candidate(level="other", url="https://blog/n", start="2026-11-22",
                                                end="2026-11-30")], T1)
    paths.news_dir.mkdir(parents=True)
    paths.sales.write_text(json.dumps({"updated": T1, "events": news}), encoding="utf-8")
    vault = tmp_path / "vault"
    vault.mkdir()
    rec = {"official": 0, "notify": [], "cart": [], "send": []}

    def fake_official(p):
        rec["official"] += 1
        off = merge_candidates([], [make_candidate(level="official", url="https://www.nike.com/bf",
                                                   start="2026-11-20", end="2026-11-30")], T1)
        p.local_dir.mkdir(parents=True, exist_ok=True)
        p.official_base.write_text(json.dumps({"updated": T1, "events": off}), encoding="utf-8")
        return True, "完成"

    def fake_cart(final_events, now, settings):
        rec["cart"].append(len(final_events))
        return {"reminders": [{"x": 1}], "digest": DIGEST}

    def fake_send(subject, body, preview, settings=None):
        rec["send"].append(preview)
        return {"sent": False, "preview": preview}
    monkeypatch.setattr(mr, "official_step", fake_official)
    monkeypatch.setattr(mr, "refresh_news_cache", lambda app_dir, cache, runner=None: ("skip", "还没有远端仓库"))
    monkeypatch.setattr(mr, "load_cart", lambda: (fake_cart, fake_send))
    monkeypatch.setattr(mr, "notify", lambda msg, runner=None: rec["notify"].append(msg) or True)
    monkeypatch.setattr(mr, "now_utc", lambda: AT_0800)
    args = ["--force", "--data-dir", str(paths.data_dir), "--config-dir", str(conf), "--vault", str(vault),
            "--log-dir", str(tmp_path / "logs")]
    return paths, vault, args, rec, tmp_path


def test_full_run(env):
    paths, vault, args, rec, tmp = env
    assert mr.main(args) == 0
    assert rec["official"] == 1 and rec["cart"] == [1] and rec["send"] == [True] and rec["notify"] == []
    final = json.loads(paths.final.read_text(encoding="utf-8"))
    assert list(final) == ["generated_at", "events"]
    e = final["events"][0]
    assert (e["id"], e["start"], e["status"]) == ("nike-US-black_friday-2026", "2026-11-20", "confirmed")
    status = json.loads(paths.status.read_text(encoding="utf-8"))
    assert status["last_success"] == AT_0800.isoformat(timespec="seconds") and status["last_result"] == "ok"
    assert "dry-run：新增 1" in status["steps"]["calendar"]
    assert not paths.calendar_map.exists()                              # 默认 dry-run：不写日历
    assert "新增 1" in (paths.local_dir / "calendar_dryrun.txt").read_text(encoding="utf-8")
    assert not paths.digest_sent.exists()                               # mail_live=false → 预览，不记
    events = json.loads((vault / "黑五购物" / "_黑五" / "data" / "events.json").read_text(encoding="utf-8"))
    assert events["meta"]["last_success"] == status["last_success"]     # 首页显示上次成功的时间
    assert [r["id"] for r in events["events"]] == ["nike-US-black_friday-2026"]
    assert "Mac 任务开始" in (tmp / "logs" / "mac_run.log").read_text(encoding="utf-8")
    # 刚成功过：不加 --force 就跳过——但过了 8 点、今天还没发汇总（预览不算发过）时照常跑
    rec["official"] = 0
    assert mr.main([a for a in args if a != "--force"]) == 0 and rec["official"] == 1
    paths.digest_sent.write_text(json.dumps({"date": "2026-11-20", "state": "sent"}), encoding="utf-8")
    assert mr.main([a for a in args if a != "--force"]) == 0 and rec["official"] == 1


def test_skip_when_locked(env, monkeypatch):
    paths, vault, args, rec, tmp = env
    held = mr.acquire_lock(paths.lock)
    try:
        monkeypatch.setattr(mr, "run_once", lambda *a, **k: (_ for _ in ()).throw(AssertionError("不该运行")))
        assert mr.main(args) == 0
    finally:
        mr.release_lock(held)


def test_official_failure_notifies_and_keeps_last_success(env, monkeypatch):
    paths, vault, args, rec, tmp = env
    paths.local_dir.mkdir(parents=True, exist_ok=True)
    paths.status.write_text(json.dumps({"last_success": T1}), encoding="utf-8")

    def boom(p):
        raise RuntimeError("浏览器起不来")
    monkeypatch.setattr(mr, "official_step", boom)
    assert mr.main(args) == 1
    status = json.loads(paths.status.read_text(encoding="utf-8"))
    assert status["last_success"] == T1 and status["last_result"] == "failed"
    assert paths.final.exists()                                        # 后面的步骤照常跑（用旧的官网部分）
    assert len(rec["notify"]) == 1 and "浏览器起不来" in rec["notify"][0]


def test_cart_missing_is_skipped(env, monkeypatch):
    paths, vault, args, rec, tmp = env
    monkeypatch.setattr(mr, "load_cart", lambda: (None, None))
    assert mr.main(args) == 0
    status = json.loads(paths.status.read_text(encoding="utf-8"))
    assert "还没有" in status["steps"]["cart"] and status["last_result"] == "ok" and rec["notify"] == []


def test_load_cart_when_module_absent(monkeypatch):
    monkeypatch.setattr(mr.importlib.util, "find_spec", lambda name: None)
    assert mr.load_cart() == (None, None)


# ---------- launchd 配置 ----------
def test_launchd_plist(tmp_path):
    spec = importlib.util.spec_from_file_location("make_plist", APP / "scripts" / "make_plist.py")
    mp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mp)
    template = (APP / "deploy" / "com.dealradar.mac.plist.template").read_text(encoding="utf-8")
    assert "__APP_DIR__" in template and "/Users/" not in template      # 模板里没有用户目录
    app_dir, log_dir = tmp_path / "app", tmp_path / "Logs" / "DealRadar"
    d = plistlib.loads(mp.render(app_dir, log_dir).encode("utf-8"))
    assert d["Label"] == "com.dealradar.mac"
    assert d["ProgramArguments"] == [f"{app_dir}/.venv312/bin/python", "-m", "dealradar.mac_run"]
    assert d["WorkingDirectory"] == str(app_dir)
    assert [x["Hour"] for x in d["StartCalendarInterval"]] == [2, 5, 8, 11, 14, 17, 20, 23]
    assert all(x["Minute"] == 0 for x in d["StartCalendarInterval"])
    assert d["RunAtLoad"] is True
    env = d["EnvironmentVariables"]
    assert "/opt/homebrew/bin" in env["PATH"].split(":") and env["PYTHONIOENCODING"] == "UTF-8"
    assert env["LANG"].endswith("UTF-8")
    assert d["StandardOutPath"].startswith(str(log_dir)) and d["StandardErrorPath"].startswith(str(log_dir))
