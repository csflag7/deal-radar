"""汇总邮件的两处补丁（一审 ③）：数据目录跟 runner 用同一份 settings；发信前先记 sending、发完改 sent。
真实发送只用假的 osascript，绝不真的发。"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from dealradar.cart import mail
from dealradar.cart.conf import Files

NOW = datetime(2026, 11, 20, 13, 5, tzinfo=timezone.utc)
SUBJECT, BODY = "Deal Radar 汇总", "正文\n第二行\n"
KEYS = ["price:c1:689.50"]


def no_runner(*a, **k):
    raise AssertionError("不该调用 osascript")


def setup(tmp_path, day="2026-11-20"):
    cfgp = tmp_path / "mail.json"
    cfgp.write_text(json.dumps({"to": "me@example.com", "allow": ["me@example.com"]}), encoding="utf-8")
    f = Files(tmp_path / "data")
    f.dir.mkdir(parents=True, exist_ok=True)
    f.pending.write_text(json.dumps({"date": day, "hash": mail.body_hash(SUBJECT, BODY), "keys": KEYS}),
                         encoding="utf-8")
    return cfgp, f


def send(f, cfgp, runner, now=NOW):
    return mail.send_digest(SUBJECT, BODY, False, data_dir=f.dir, config_path=cfgp, runner=runner, now=now,
                            platform="darwin")


def notified(f):
    return json.loads(f.notified.read_text(encoding="utf-8"))


def test_preview_uses_settings_cart_data_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("DEALRADAR_CART_DATA_DIR", raising=False)
    d = tmp_path / "from-settings"
    out = mail.send_digest(SUBJECT, BODY, True, settings={"cart_data_dir": str(d)}, runner=no_runner, now=NOW)
    assert out["text_path"].startswith(str(d / "mail_preview"))        # 和 runner 读写同一个位置
    assert mail._files(None, {"cart_data_dir": str(d)}).pending == d / "cart_pending.json"


def test_sending_marker_then_sent(tmp_path):
    cfgp, f = setup(tmp_path)
    states = []

    def runner(cmd, **kw):
        states.append(notified(f)["digests"]["2026-11-20"]["state"])     # 发信那一刻已经记了 sending
        return SimpleNamespace(returncode=0, stderr="")
    out = send(f, cfgp, runner)
    assert out["sent"] is True and states == ["sending"]
    n = notified(f)
    assert n["digests"]["2026-11-20"]["state"] == "sent" and list(n["keys"]) == KEYS


def test_failed_send_restores_previous_notified(tmp_path):
    cfgp, f = setup(tmp_path)
    f.notified.write_text(json.dumps({"keys": {"old": "x"}, "digests": {"2026-11-19": "2026-11-19T13:00:00"}}),
                          encoding="utf-8")
    before = notified(f)
    with pytest.raises(mail.MailError, match="发送失败"):
        send(f, cfgp, lambda *a, **k: SimpleNamespace(returncode=1, stderr="no"))
    assert notified(f) == before                                          # 明确没发出去：撤掉 sending


def test_sent_but_marking_failed_is_not_resent_and_reminded_next_time(tmp_path, monkeypatch):
    cfgp, f = setup(tmp_path)
    sent_bodies = []

    def runner(cmd, **kw):
        sent_bodies.append(cmd[-2])
        return SimpleNamespace(returncode=0, stderr="")

    def broken_mark(*a, **k):
        raise OSError("写不进去")
    monkeypatch.setattr(mail, "mark_sent", broken_mark)
    with pytest.raises(OSError):
        send(f, cfgp, runner)                                             # 邮件已经发出，但没记下来
    assert notified(f)["digests"]["2026-11-20"]["state"] == "sending"
    monkeypatch.undo()
    again = send(f, cfgp, no_runner, NOW + timedelta(hours=3))           # 当天：不自动重发
    assert again["sent"] is False and again["uncertain"] is True and "已发送" in again["reason"]
    # 第二天的汇总：开头提醒“上次可能已发”；去重按原正文算，提醒照常记进去
    f.pending.write_text(json.dumps({"date": "2026-11-21", "hash": mail.body_hash(SUBJECT, BODY), "keys": KEYS}),
                         encoding="utf-8")
    out = send(f, cfgp, runner, NOW + timedelta(days=1))
    assert out["sent"] is True and out["marked"] == 1
    assert sent_bodies[-1].startswith("提醒：2026-11-20 的汇总上次可能已经发出") and sent_bodies[-1].endswith(BODY)
    n = notified(f)
    assert n["digests"]["2026-11-20"]["state"] == "uncertain_reminded" and n["digests"]["2026-11-21"]["state"] == "sent"
    # 再下一天就不再提醒了
    f.pending.write_text(json.dumps({"date": "2026-11-22", "hash": mail.body_hash(SUBJECT, BODY), "keys": []}),
                         encoding="utf-8")
    send(f, cfgp, runner, NOW + timedelta(days=2))
    assert sent_bodies[-1] == BODY


def test_old_timestamp_format_still_counts_as_sent(tmp_path):
    cfgp, f = setup(tmp_path)
    f.notified.write_text(json.dumps({"digests": {"2026-11-20": "2026-11-20T13:00:00"}}), encoding="utf-8")
    out = send(f, cfgp, no_runner)
    assert out["sent"] is False and "已经发过" in out["reason"] and not out.get("uncertain")
