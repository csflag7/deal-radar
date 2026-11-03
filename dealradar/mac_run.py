"""Mac 本地入口（launchd 每天 2、5、8…23 点各跑一次，开机 / 登录时也跑；§0.1 修订、ADR-008）。

用法（在 app 目录下）：
  .venv312/bin/python -m dealradar.mac_run            正常运行（距上次成功不到 2.5 小时就跳过）
  .venv312/bin/python -m dealradar.mac_run --force    不管上次什么时候成功，都跑一次

每次依次做：
  0. 拿锁（fcntl.flock，拿不到说明上一次还在跑，直接退出）；距上次成功不到 2.5 小时就跳过（--force 除外）
  1. 取云端新闻：git fetch + git show origin/main:data/news/sales.json → data/local/news_cache.json
     （只读远端，不在开发目录里 git pull；没有仓库、没有远端或者取失败，就沿用旧缓存；连缓存都没有，用本地 data/news/sales.json）
  2. 官网监控：python -m dealradar.run --official（只写 data/local/）
  3. combine：新闻 + 官网 + 人工记录 + 人工纠错 → data/local/final.json（每次从头算）
  4. 同步 Obsidian：final.json → 黑五购物工作台（events.json 带 meta：上次成功的时间等）
  5. 同步 Mac 日历：settings.yaml 的 calendar_apply 不是 true 时只打印 dry-run 清单（另存 data/local/calendar_dryrun.txt）
  6. 购物车：dealradar.cart 存在才调用 cart_run(final_events, now, settings)
  7. 汇总邮件：多伦多时间 ≥ 8:00、今天还没发过（data/local/digest_sent.json）、购物车给了 digest 才发；
     settings.yaml 的 mail_live 不是 true 时一律 preview=True（预览不记 digest_sent）；真的发送时先记 sending，
     send_digest 返回 sent is True 才改成 sent，结果不明就不自动重发、提醒用户去看已发送邮件
  过了 8 点、当天还没发过汇总时，不受“距上次成功 2.5 小时”的限制。
结果写 data/local/status.json；日志写 ~/Library/Logs/DealRadar/mac_run.log；有步骤出错时发一条 macOS 本地通知。
"""
from __future__ import annotations

import argparse
import fcntl
import importlib
import importlib.util
import inspect
import json
import logging
import logging.handlers
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

from .config import APP_ROOT, Paths, load_config, load_json, load_yaml, now_utc, save_json

log = logging.getLogger("dealradar.mac_run")

DEFAULT_LOG_DIR = Path.home() / "Library" / "Logs" / "DealRadar"
MIN_INTERVAL = timedelta(hours=2.5)
DIGEST_HOUR = 8
DIGEST_TZ = "America/Toronto"
REMOTE = "origin"
BRANCH = "main"
NEWS_REL = Path("data") / "news" / "sales.json"
NOTIFY_SCRIPT = """
on run argv
	display notification (item 1 of argv) with title (item 2 of argv)
end run
"""


# ---------- 锁、跳过 ----------
def acquire_lock(path: Path):
    """拿到锁返回打开的文件（进程结束或 release_lock 时释放）；拿不到返回 None。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "a+", encoding="utf-8")
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    f.seek(0)
    f.truncate()
    f.write(f"{os.getpid()}\n")
    f.flush()
    return f


def release_lock(f) -> None:
    if f is None:
        return
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    finally:
        f.close()


def skip_reason(status: dict, now: datetime, force: bool = False, digest_is_due: bool = False) -> Optional[str]:
    """距上次成功不到 2.5 小时 → 返回跳过的原因；否则 None。
    已经过了 8 点、当天还没发过汇总（digest_is_due）时不受 2.5 小时限制。"""
    if force or digest_is_due:
        return None
    last = (status or {}).get("last_success")
    if not last:
        return None
    try:
        t = datetime.fromisoformat(last)
    except ValueError:
        return None
    if t > now + timedelta(minutes=5):          # 时间记错了（比现在还晚）：不当回事，照常跑
        return None
    if now - t < MIN_INTERVAL:
        return f"距上次成功（{last}）不到 2.5 小时，这次跳过（加 --force 可以强制运行）"
    return None


# ---------- 1. 取云端新闻 ----------
def _git(app_dir: Path, args: list, runner: Callable, timeout: int = 120):
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", LC_ALL="C")
    return runner(["git", "-C", str(app_dir), *args], capture_output=True, text=True, timeout=timeout, env=env)


def refresh_news_cache(app_dir: Path, cache_path: Path, runner: Optional[Callable] = None) -> tuple:
    """git fetch + git show origin/main:<data/news/sales.json> → 写缓存。返回 (状态, 说明)；
    状态：ok 取到了 / skip 还没有仓库或远端（正常跳过）/ fail 取失败（沿用旧缓存）。"""
    runner = runner or subprocess.run
    try:
        r = _git(app_dir, ["rev-parse", "--show-toplevel"], runner, 30)
        if r.returncode != 0:
            return "skip", "app/ 还不是 git 仓库，跳过取云端新闻"
        top = Path(r.stdout.strip())
        if top.resolve() != app_dir.resolve():      # 仓库根目录就是 app/（见 daily.yml）；上层目录碰巧是别的仓库时不去动它
            return "skip", "app/ 不是仓库的根目录，跳过取云端新闻"
        r = _git(app_dir, ["remote", "get-url", REMOTE], runner, 30)
        if r.returncode != 0:
            return "skip", f"还没有远端仓库 {REMOTE}，跳过取云端新闻"
        r = _git(app_dir, ["fetch", "--quiet", REMOTE, BRANCH], runner, 180)
        if r.returncode != 0:
            return "fail", f"git fetch 失败：{(r.stderr or '').strip()[:300]}"
        r = _git(app_dir, ["show", f"{REMOTE}/{BRANCH}:{NEWS_REL.as_posix()}"], runner, 60)
        if r.returncode != 0:
            return "fail", f"git show 失败：{(r.stderr or '').strip()[:300]}"
        doc = json.loads(r.stdout)
        if not isinstance(doc, dict) or not isinstance(doc.get("events"), list):
            return "fail", "云端的 sales.json 格式不对（没有 events 列表）"
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as e:
        return "fail", f"取云端新闻出错：{type(e).__name__}: {e}"
    save_json(cache_path, doc)
    return "ok", f"云端新闻 {len(doc['events'])} 个事件（云端更新于 {doc.get('updated')}）"


# ---------- 2. 官网监控 ----------
def official_step(paths: Paths) -> tuple:
    """跑 dealradar.run --official（只写 data/local/）。返回 (成功?, 说明)。"""
    from . import run as runmod
    rc = runmod.main(["--official", "--no-push", "--config-dir", str(paths.config_dir),
                      "--data-dir", str(paths.data_dir)])
    return rc == 0, ("完成" if rc == 0 else f"退出码 {rc}")


# ---------- 6./7. 购物车、汇总邮件（dealradar.cart 由另一部分实现；不存在就跳过）----------
def _fn(obj, name: str):
    """from dealradar.cart import run：run 可能是函数，也可能是子模块（那就取模块里的同名函数）。"""
    return getattr(obj, name) if inspect.ismodule(obj) else obj


def load_cart() -> tuple:
    """返回 (cart_run, send_digest)；购物车模块还不存在 → (None, None)。"""
    if importlib.util.find_spec("dealradar.cart") is None:
        return None, None
    cart = importlib.import_module("dealradar.cart")
    cart_run = _fn(getattr(cart, "run"), "run")
    send = None
    if importlib.util.find_spec("dealradar.cart.mail") is not None:
        send = _fn(getattr(importlib.import_module("dealradar.cart.mail"), "send_digest"), "send_digest")
    return cart_run, send


def local_now(now: datetime, tz_name: str = DIGEST_TZ) -> datetime:
    from zoneinfo import ZoneInfo
    return now.astimezone(ZoneInfo(tz_name))


def digest_due(now: datetime, sent_path: Path, tz_name: str = DIGEST_TZ) -> bool:
    """已经过了 8 点（多伦多时间），并且 digest_sent.json 里没有今天的记录。"""
    local = local_now(now, tz_name)
    rec = load_json(sent_path, {}) or {}
    return local.hour >= DIGEST_HOUR and rec.get("date") != local.date().isoformat()


def _restore(path: Path, rec: dict) -> None:
    if rec:
        save_json(path, rec)
    elif path.exists():
        path.unlink()


def _send_kwargs(send: Callable, preview: bool, settings: dict) -> dict:
    """send_digest(subject, body, preview)；接受 settings 参数的话也传进去（和购物车用同一份 cart_data_dir）。"""
    kw = {"preview": preview}
    try:
        if "settings" in inspect.signature(send).parameters:
            kw["settings"] = settings
    except (TypeError, ValueError):
        pass
    return kw


def maybe_send_digest(digest: Optional[dict], now: datetime, settings: dict, sent_path: Path,
                      send: Optional[Callable]) -> tuple:
    """8 点规则：多伦多时间 ≥ 8:00、今天还没发过才发。返回 (说明, 是否要提醒用户)。
    - mail_live 不是 true → preview=True，预览不写 digest_sent。
    - 真的发送：先在 digest_sent.json 记 state=sending 再发；send 返回 sent is True 才改成 sent。
      明确没发出去（MailError、sent 不是 True）就撤掉标记；结果不明（其他异常）就留着 sending：
      今天不再自动重发，提醒用户去「邮件」App 的“已发送”里看。"""
    if not digest:
        return "今天没有要发的汇总", False
    if send is None:
        return "发信模块（dealradar.cart.mail）还没有，跳过", False
    local = local_now(now, settings.get("timezone") or DIGEST_TZ)
    if local.hour < DIGEST_HOUR:
        return f"还没到 {DIGEST_HOUR} 点（多伦多时间 {local:%H:%M}），不发", False
    today = local.date().isoformat()
    now_iso = now.isoformat(timespec="seconds")
    rec = load_json(sent_path, {}) or {}
    if rec.get("date") == today:
        if rec.get("state") in ("sending", "uncertain"):
            return ("今天的汇总上次发送结果不明（可能已经发出）：不自动重发，请到「邮件」App 的“已发送”里看一下", True)
        return f"今天（{today}）已经发过", False
    preview = settings.get("mail_live") is not True
    subject, body = digest.get("subject") or "", digest.get("body") or ""
    kw = _send_kwargs(send, preview, settings)
    if preview:
        res = send(subject, body, **kw)
        return f"预览（mail_live 不是 true，没有真的发送，不记 digest_sent）：{res}", False
    save_json(sent_path, {"date": today, "state": "sending", "started_at": now_iso, "subject": subject})
    try:
        res = send(subject, body, **kw)
    except Exception as e:
        if type(e).__name__ == "MailError":          # 明确没发出去：撤掉标记，下次可以再试
            _restore(sent_path, rec)
            return f"发送失败：{e}", True
        return (f"发送结果不明（{type(e).__name__}: {e}）：今天不再自动重发，请到「邮件」App 的“已发送”里看一下", True)
    if isinstance(res, dict) and res.get("sent") is True:
        save_json(sent_path, {"date": today, "state": "sent", "sent_at": now_iso, "subject": subject})
        return "已发送", False
    if isinstance(res, dict) and res.get("uncertain"):
        save_json(sent_path, {"date": today, "state": "uncertain", "at": now_iso, "subject": subject})
        return f"发送结果不明：{res.get('reason')}", True
    _restore(sent_path, rec)
    already = isinstance(res, dict) and "已经发过" in str(res.get("reason") or "")
    return f"没有发出：{res}", not already


def _timezone(paths: Paths) -> str:
    try:
        return (load_yaml(paths.config_dir / "settings.yaml") or {}).get("timezone") or DIGEST_TZ
    except Exception:
        return DIGEST_TZ


# ---------- 通知、日志 ----------
def notify(message: str, runner: Optional[Callable] = None) -> bool:
    """macOS 本地通知（osascript display notification）；不是 Mac 或者出错就只记日志。"""
    if sys.platform != "darwin" and runner is None:
        return False
    try:
        r = (runner or subprocess.run)(["osascript", "-", message[:220], "Deal Radar"], input=NOTIFY_SCRIPT,
                                       capture_output=True, text=True, timeout=20)
        return getattr(r, "returncode", 1) == 0
    except Exception as e:      # 通知发不出去不影响别的
        log.warning("本地通知发不出去：%s: %s", type(e).__name__, e)
        return False


def setup_logging(log_dir: Path):
    from .run import setup_logging as stdout_logging
    stdout_logging()
    log_dir.mkdir(parents=True, exist_ok=True)
    fh = logging.handlers.RotatingFileHandler(log_dir / "mac_run.log", maxBytes=2_000_000, backupCount=5,
                                              encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S"))
    logging.getLogger().addHandler(fh)
    return fh


# ---------- 主流程 ----------
def run_once(paths: Paths, now: datetime, vault: Path, subdir: str, skip_official: bool = False,
             git_runner: Optional[Callable] = None, app_dir: Path = APP_ROOT) -> dict:
    """跑一遍各步骤，返回 status（不含锁和跳过判断）。任何一步出错都继续往下走，记进 problems。"""
    from . import calendar_sync, obsidian_sync
    from .combine import build_final, news_input

    t0 = time.monotonic()
    now_iso = now.isoformat(timespec="seconds")
    prev = load_json(paths.status, {}) or {}
    steps: dict = {}
    problems: list = []
    core_ok = True

    def problem(step: str, msg: str, core: bool = False) -> None:
        nonlocal core_ok
        steps[step] = msg
        problems.append(f"{step}：{msg}")
        log.warning("【%s】%s", step, msg)
        if core:
            core_ok = False

    # 1. 云端新闻
    state, msg = refresh_news_cache(app_dir, paths.news_cache, git_runner)
    news_events, news_path = news_input(paths)
    used = {paths.news_cache: "缓存 news_cache.json", paths.sales: "本地 data/news/sales.json"}.get(news_path, "无")
    steps["news"] = f"{msg}；这次用：{used}（{len(news_events)} 个事件）"
    log.info("【新闻】%s", steps["news"])
    if state == "fail":
        problems.append(f"news：{msg}（沿用旧数据）")
    if news_path is None:
        problem("news", "没有任何新闻数据（缓存和本地文件都没有）")

    # 2. 官网监控
    if skip_official:
        steps["official"] = "跳过（--skip-official）"
    else:
        log.info("【官网监控】开始")
        try:
            ok, msg = official_step(paths)
            steps["official"] = msg
            if not ok:
                problem("official", msg, core=True)
        except Exception as e:
            problem("official", f"出错：{type(e).__name__}: {e}", core=True)

    # 3. combine
    final_doc = None
    try:
        cfg = load_config(paths)
        final_doc = build_final(paths, cfg, news_events, now_iso)
        steps["combine"] = f"{len(final_doc['events'])} 个事件"
    except Exception as e:
        cfg = None
        problem("combine", f"出错：{type(e).__name__}: {e}", core=True)
    if final_doc is None:
        old = load_json(paths.final, None)
        final_doc = old if isinstance(old, dict) else {"generated_at": None, "events": []}
    final_events = final_doc.get("events") or []
    settings = cfg.settings if cfg else {}

    # 4. Obsidian（meta：首页显示上次成功的时间）
    meta = {"last_success": now_iso if core_ok else prev.get("last_success"), "last_run": now_iso,
            "news": steps.get("news"), "problems": list(problems)}
    try:
        st = obsidian_sync.sync(vault, paths.final, paths, now=now, subdir=subdir, overrides_applied=True, meta=meta)
        steps["obsidian"] = (f"{st['events']} 条活动；品牌笔记 新建 {len(st['created'])}、更新 {len(st['updated'])}、"
                             f"跳过 {len(st['skipped'])}")
        log.info("【Obsidian】%s", steps["obsidian"])
    except Exception as e:
        problem("obsidian", f"出错：{type(e).__name__}: {e}", core=True)

    # 5. Mac 日历（默认 dry-run）
    apply = settings.get("calendar_apply") is True
    try:
        if apply and sys.platform != "darwin":
            steps["calendar"] = "不是 Mac，跳过"
        else:
            res = calendar_sync.sync(final_events, cfg.brands if cfg else {}, paths.calendar_map, apply=apply,
                                     confirmed_only=settings.get("calendar_confirmed_only") is True,
                                     plan_out=paths.local_dir / "calendar_dryrun.txt")
            p = res["plan"]
            steps["calendar"] = (f"{'已写入' if res['applied'] else 'dry-run'}：新增 {len(p.add)}、修改 {len(p.update)}、"
                                 f"删除 {len(p.delete)}、不变 {len(p.unchanged)}")
            for w in p.warnings:
                problems.append(f"calendar：{w}")
    except calendar_sync.CalendarError as e:
        problem("calendar", str(e))
    except Exception as e:
        problem("calendar", f"出错：{type(e).__name__}: {e}")

    # 6. 购物车 + 7. 汇总邮件
    try:
        cart_run, send = load_cart()
        if cart_run is None:
            steps["cart"] = "购物车模块（dealradar.cart）还没有，跳过"
            steps["digest"] = "跳过"
        else:
            out = cart_run(final_events, now, settings) or {}
            steps["cart"] = f"提醒 {len(out.get('reminders') or [])} 条"
            steps["digest"], bad = maybe_send_digest(out.get("digest"), now, settings, paths.digest_sent, send)
            if bad:
                problems.append(f"digest：{steps['digest']}")
        log.info("【购物车】%s；【汇总邮件】%s", steps["cart"], steps["digest"])
    except Exception as e:
        problem("cart", f"出错：{type(e).__name__}: {e}")

    status = {
        "last_success": now_iso if core_ok else prev.get("last_success"),
        "last_run": now_iso,
        "last_result": "ok" if core_ok and not problems else ("partial" if core_ok else "failed"),
        "seconds": round(time.monotonic() - t0),
        "events": len(final_events),
        "steps": steps,
        "problems": problems,
    }
    save_json(paths.status, status)
    return status


def main(argv=None) -> int:
    from .obsidian_sync import DEFAULT_SUBDIR, DEFAULT_VAULT
    ap = argparse.ArgumentParser(description="Deal Radar Mac 本地任务：官网监控 → 合并 → Obsidian → 日历 → 购物车 → 邮件")
    ap.add_argument("--force", action="store_true", help="不管距上次成功多久，都跑一次")
    ap.add_argument("--skip-official", action="store_true", help="这次不跑官网监控（调试用）")
    ap.add_argument("--data-dir", type=Path, default=Paths.data_dir)
    ap.add_argument("--config-dir", type=Path, default=Paths.config_dir)
    ap.add_argument("--vault", type=Path, default=DEFAULT_VAULT)
    ap.add_argument("--subdir", default=DEFAULT_SUBDIR)
    ap.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR)
    args = ap.parse_args(argv)

    fh = setup_logging(args.log_dir)
    lock = None
    try:
        paths = Paths(config_dir=args.config_dir, data_dir=args.data_dir)
        lock = acquire_lock(paths.lock)
        if lock is None:
            log.info("上一次运行还没结束（拿不到锁），这次直接退出")
            return 0
        now = now_utc()
        due = digest_due(now, paths.digest_sent, _timezone(paths))
        why = skip_reason(load_json(paths.status, {}) or {}, now, args.force, due)
        if why:
            log.info(why)
            return 0
        log.info("==== Deal Radar Mac 任务开始 %s ====", now.isoformat(timespec="seconds"))
        try:
            status = run_once(paths, now, args.vault.expanduser(), args.subdir, args.skip_official)
        except Exception as e:      # 意料之外的错误：记下来、发通知
            log.exception("Mac 任务出错")
            notify(f"Deal Radar 运行出错：{type(e).__name__}: {e}")
            return 1
        for k, v in status["steps"].items():
            log.info("  %-9s %s", k, v)
        if status["problems"]:
            notify("Deal Radar：" + "；".join(status["problems"])[:200])
        log.info("==== 结束：%s（用时 %s 秒）====", status["last_result"], status["seconds"])
        return 0 if status["last_result"] != "failed" else 1
    finally:
        release_lock(lock)
        logging.getLogger().removeHandler(fh)
        fh.close()


if __name__ == "__main__":
    sys.exit(main())
