"""购物车：汇总邮件。预览只写文件；真实发送只测“假的 osascript”，绝不真的发。"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from dealradar.cart import mail
from dealradar.cart.conf import Files

NOW = datetime(2026, 11, 20, 8, 5, tzinfo=timezone.utc)


def no_runner(*a, **k):
    raise AssertionError("测试里不能真的调用 osascript")


def test_preview_writes_files_and_never_sends(tmp_path):
    out = mail.send_digest("主题 <b>", "第一行\n链接 https://www.bestbuy.ca/a?b=1&c=2\n<script>x</script>", True,
                           data_dir=tmp_path, runner=no_runner, now=NOW)
    assert out["sent"] is False and out["preview"] is True
    html = open(out["html_path"], encoding="utf-8").read()
    txt = open(out["text_path"], encoding="utf-8").read()
    assert out["html_path"].startswith(str(tmp_path / "mail_preview"))
    assert "<script>" not in html and "&lt;script&gt;" in html and "【预览】" in html
    assert '<a href="https://www.bestbuy.ca/a?b=1">' in html or "https://www.bestbuy.ca/a?b=1&amp;c=2" in html
    assert txt.startswith("主题：主题 <b>\n\n第一行")
    assert not (tmp_path / "notified.json").exists()                     # 预览不写去重记录


def _setup(tmp_path, to="me@example.com", allow=("me@example.com",)):
    cfgp = tmp_path / "mail.json"
    cfgp.write_text(json.dumps({"to": to, "allow": list(allow)}), encoding="utf-8")
    f = Files(tmp_path)
    subject, body = '提醒 "引号" end tell', "正文\n第二行\n"
    f.pending.write_text(json.dumps({"date": "2026-11-20", "hash": mail.body_hash(subject, body),
                                     "keys": ["price:c1:689.50", "deadline:c1:2026-11-23"]}), encoding="utf-8")
    return cfgp, f, subject, body


def test_send_uses_argv_whitelist_and_marks_notified(tmp_path):
    cfgp, f, s, b = _setup(tmp_path)
    calls = []

    def runner(cmd, **kw):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stderr="")
    out = mail.send_digest(s, b, False, data_dir=tmp_path, config_path=cfgp, runner=runner, now=NOW,
                           platform="darwin")
    assert out == {"sent": True, "preview": False, "marked": 2}
    cmd = calls[0]
    assert cmd[0] == "osascript" and cmd[-3:] == [s, b, "me@example.com"]
    assert cmd[1:-3] == [x for line in mail.APPLESCRIPT for x in ("-e", line)]   # 内容走 argv，不拼进脚本
    n = json.loads(f.notified.read_text(encoding="utf-8"))
    assert set(n["keys"]) == {"price:c1:689.50", "deadline:c1:2026-11-23"} and "2026-11-20" in n["digests"]
    again = mail.send_digest(s, b, False, data_dir=tmp_path, config_path=cfgp, runner=no_runner, now=NOW,
                             platform="darwin")
    assert again["sent"] is False and "已经发过" in again["reason"]      # 每天最多 1 封


def test_send_refuses_recipient_not_in_whitelist(tmp_path):
    cfgp, f, s, b = _setup(tmp_path, to="other@example.com")
    with pytest.raises(mail.MailError, match="白名单"):
        mail.send_digest(s, b, False, data_dir=tmp_path, config_path=cfgp, runner=no_runner, now=NOW,
                         platform="darwin")
    with pytest.raises(mail.MailError, match="没有收件人设置文件"):
        mail.send_digest(s, b, False, data_dir=tmp_path, config_path=tmp_path / "none.json", runner=no_runner,
                         now=NOW, platform="darwin")
    assert not f.notified.exists()


def test_send_failure_does_not_mark(tmp_path):
    cfgp, f, s, b = _setup(tmp_path)
    with pytest.raises(mail.MailError, match="发送失败"):
        mail.send_digest(s, b, False, data_dir=tmp_path, config_path=cfgp,
                         runner=lambda *a, **k: SimpleNamespace(returncode=1, stderr="no"), now=NOW, platform="darwin")
    assert not f.notified.exists()


def test_mismatched_body_marks_day_but_not_keys(tmp_path):
    cfgp, f, s, b = _setup(tmp_path)
    mail.send_digest(s, "别的正文", False, data_dir=tmp_path, config_path=cfgp,
                     runner=lambda *a, **k: SimpleNamespace(returncode=0, stderr=""), now=NOW, platform="darwin")
    n = json.loads(f.notified.read_text(encoding="utf-8"))
    assert n["keys"] == {} and "2026-11-20" in n["digests"]


def test_send_only_on_macos(tmp_path):
    cfgp, f, s, b = _setup(tmp_path)
    with pytest.raises(mail.MailError, match="macOS"):
        mail.send_digest(s, b, False, data_dir=tmp_path, config_path=cfgp, runner=no_runner, now=NOW,
                         platform="linux")


@pytest.mark.skipif(sys.platform != "darwin", reason="只在 macOS 上用「邮件」App 发送")
def test_macos_default_path_checks_recipient_file_first(tmp_path):
    """macOS 上不传 platform：先读本地收件人文件，读不到就停（不会走到 osascript）。"""
    with pytest.raises(mail.MailError, match="没有收件人设置文件"):
        mail.send_digest("S", "B", False, data_dir=tmp_path, config_path=tmp_path / "missing.json",
                         runner=no_runner, now=NOW)
    assert str(mail.MAIL_CONFIG).endswith("Library/Application Support/DealRadar/mail.json")
