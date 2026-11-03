from __future__ import annotations

import json
import shutil
from datetime import date

import httpx

from conftest import APP, FIXTURES
from dealradar import notify
from dealradar.notify import build_alert, build_digest, push_run

TODAY = date(2026, 11, 1)
SETTINGS = {"ntfy_server": "https://ntfy.example", "site_url": "https://example.github.io/deals/",
            "notify_max_items": 10, "notify_max_bytes": 3500, "alert_source_failures": 3}


def change(kind, i, **kw):
    c = {"id": f"nike-US-black_friday-{i}", "brand_id": "nike", "region": "US", "event_type": "black_friday",
         "event_name": "Nike Big Sale from another article", "start": "2026-11-20", "end": "2026-11-30",
         "discount": "up to 50% off", "status": "rumored", "kind": kind}
    c.update(kw)
    return c


def recorder():
    sent = []

    def handler(req):
        sent.append((str(req.url), json.loads(req.content)))
        return httpx.Response(200, json={"id": "x"})
    return sent, httpx.MockTransport(handler)


def test_digest_content_and_order(cfg):
    tomorrow = {"id": "bestbuy-CA-early_bf-2026", "brand_id": "bestbuy", "region": "CA", "event_type": "early_bf",
                "event_name": "My Best Buy member early access", "start": "2026-11-02", "end": "2026-11-05"}
    changes = [change("new_event", 1), change("new_event", 2, start=None, end=None),      # 没日期的新事件不列
               change("confirmed", 3), change("date_changed", 4, old_start="2026-11-21", old_end=None),
               change("new_event", 5, start="2026-10-06", end="2026-10-07")]                # 已结束的不列
    d = build_digest(changes, [tomorrow], cfg.brands, TODAY, SETTINGS["site_url"])
    lines = d["message"].split("\n")
    assert d["title"] == "Deal Radar · 11月1日"
    assert lines[0] == "【明天开始】Best Buy · 提前黑五（加拿大）11/2–11/5"
    assert lines[1].startswith("【新官宣】Nike · 黑五（美国）11/20–11/30 · up to 50% off")
    assert lines[2] == "【日期变动】Nike · 黑五（美国）11/20–11/30（原 11/21 起） · up to 50% off"
    assert lines[3].startswith("【新活动】Nike · 黑五")
    assert len(lines) == 6 and lines[-1] == SETTINGS["site_url"] and lines[-2] == ""
    assert "another article" not in d["message"]        # 非 other 用类型中文名，不用 AI 的活动名
    assert build_digest([change("new_event", 9, start=None)], [], cfg.brands, TODAY) is None


def test_digest_truncation_by_count_and_bytes(cfg):
    many = [change("new_event", i) for i in range(14)]
    d = build_digest(many, [], cfg.brands, TODAY, SETTINGS["site_url"], max_items=10)
    lines = d["message"].split("\n")
    assert sum(1 for x in lines if x.startswith("【")) == 10 and "还有 4 条" in lines
    long = [change("new_event", i, discount="x" * 150) for i in range(14)]
    d2 = build_digest(long, [], cfg.brands, TODAY, SETTINGS["site_url"], max_items=10, max_bytes=1000)
    assert len(d2["message"].encode("utf-8")) <= 1000
    shown = sum(1 for x in d2["message"].split("\n") if x.startswith("【"))
    assert shown < 10 and f"还有 {14 - shown} 条" in d2["message"]


def test_push_sends_digest_with_headers(cfg):
    sent, transport = recorder()
    r = push_run(SETTINGS, [change("confirmed", 1)], [], cfg.brands, {}, False, TODAY, transport=transport,
                 topic="secret-topic")
    assert r == {"digest": True, "alert": False}
    assert len(sent) == 1
    url, body = sent[0]
    assert url == "https://ntfy.example/"
    assert body["topic"] == "secret-topic" and body["title"] == "Deal Radar · 11月1日"
    assert body["tags"] == ["shopping_cart"] and "priority" not in body
    assert body["click"] == SETTINGS["site_url"] and body["message"].startswith("【新官宣】")


def test_first_run_is_silent_but_alerts_still_go(cfg):
    sent, transport = recorder()
    r = push_run(SETTINGS, [change("new_event", 1)], [], cfg.brands, {}, True, TODAY, transport=transport, topic="t")
    assert r == {"digest": False, "alert": False} and sent == []
    health = {"dealnews": {"consecutive_failures": 3, "last_error": "HTTP 503"}}
    r = push_run(SETTINGS, [change("new_event", 1)], [], cfg.brands, health, True, TODAY, transport=transport,
                 topic="t")
    assert r == {"digest": False, "alert": True} and len(sent) == 1 and sent[0][1]["priority"] == 4


def test_alert_separate_from_digest(cfg):
    sent, transport = recorder()
    health = {"dealnews": {"consecutive_failures": 4, "last_error": "HTTP 503"},
              "bing-US-nike": {"consecutive_failures": 1, "last_error": "ReadTimeout"}}
    r = push_run(SETTINGS, [change("confirmed", 1)], [], cfg.brands, health, False, TODAY,
                 ai_aborted="HTTP 402：Insufficient Balance", transport=transport, topic="t")
    assert r == {"digest": True, "alert": True} and len(sent) == 2
    alert = sent[0][1]
    assert alert["priority"] == 4 and alert["tags"] == ["warning"]
    assert "dealnews" in alert["message"] and "bing-US-nike" not in alert["message"]
    assert "Insufficient Balance" in alert["message"]
    assert sent[1][1]["tags"] == ["shopping_cart"]


def test_build_alert_ai_all_batches_failed():
    assert build_alert({}, 3, None, ["第 1 批：HTTP 500"], 3, 0).startswith("AI 提取整体失败")
    assert build_alert({}, 3, None, ["第 1 批：HTTP 500"], 3, 10) is None      # 只是部分失败
    assert build_alert({}, 3, None, [], 0, 0) is None


def test_no_topic_only_warns(cfg, caplog, monkeypatch):
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    sent, transport = recorder()
    r = push_run(SETTINGS, [change("confirmed", 1)], [], cfg.brands, {}, False, TODAY, transport=transport)
    assert r == {"digest": False, "alert": False} and sent == []
    assert "没有配置 NTFY_TOPIC" in caplog.text


def test_send_failure_does_not_raise(cfg):
    transport = httpx.MockTransport(lambda req: httpx.Response(500))
    r = push_run(SETTINGS, [change("confirmed", 1)], [], cfg.brands, {}, False, TODAY, transport=transport, topic="t")
    assert r == {"digest": False, "alert": False}


def test_run_calls_push_except_dry_run_and_no_push(tmp_path, no_real_push):
    from dealradar.run import main
    conf = tmp_path / "config"
    shutil.copytree(APP / "config", conf)
    (conf / "manual_events.yaml").write_text("events: []\n", encoding="utf-8")
    base = ["--fixture", str(FIXTURES / "run"), "--config-dir", str(conf), "--data-dir", str(tmp_path / "d"),
            "--web-out", str(tmp_path / "w.js")]
    assert main(base + ["--dry-run"]) == 0 and no_real_push == []
    assert main(base + ["--no-push"]) == 0 and no_real_push == []
    assert main(base) == 0 and len(no_real_push) == 1
    args = no_real_push[0][0]
    assert args[5] is False                              # 第二次真实写入：不是首次运行


def test_preview_prints_messages_without_sending(tmp_path, monkeypatch):
    from dealradar.run import main
    conf = tmp_path / "config"
    shutil.copytree(APP / "config", conf)
    (conf / "manual_events.yaml").write_text("events: []\n", encoding="utf-8")
    data = tmp_path / "data"
    assert main(["--fixture", str(FIXTURES / "run"), "--config-dir", str(conf), "--data-dir", str(data),
                 "--web-out", str(tmp_path / "w.js"), "--no-push"]) == 0
    monkeypatch.setattr(notify.Notifier, "send", lambda *a, **k: (_ for _ in ()).throw(AssertionError("sent")))
    out = notify.preview(data, conf)
    assert "首次运行：只建库，不发摘要" in out and "（没有要发的推送）" in out
    out2 = notify.preview(data, conf, as_if_not_first_run=True)
    assert "==== 第 1 条：摘要 ====" in out2 and "Title: Deal Radar · " in out2 and "Tags: shopping_cart" in out2
    assert "【新活动】" in out2
