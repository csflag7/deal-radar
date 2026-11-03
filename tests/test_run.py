from __future__ import annotations

import json
import shutil

import pytest

from conftest import APP, FIXTURES
from dealradar import build_web
from dealradar.run import main

FIX = FIXTURES / "run"


@pytest.fixture
def env(tmp_path):
    conf = tmp_path / "config"
    shutil.copytree(APP / "config", conf)
    shutil.copy(FIX / "overrides.yaml", conf / "overrides.yaml")
    (conf / "manual_events.yaml").write_text("events: []\n", encoding="utf-8")   # 人工记录另有测试
    data, web = tmp_path / "data", tmp_path / "web" / "data.js"
    args = ["--fixture", str(FIX), "--config-dir", str(conf), "--data-dir", str(data), "--web-out", str(web)]
    return args, data, web


def load(p):
    return json.loads(p.read_text(encoding="utf-8"))


def test_fixture_run_end_to_end(env):
    args, data, web = env
    assert main(args) == 0

    base = {e["id"]: e for e in load(data / "news" / "sales.json")["events"]}
    assert sorted(base) == ["amazon-unknown-other-2026-airpodpro3", "canadiantire-CA-black_friday-2026",
                            "lululemon-US-black_friday-2026", "nike-CA-black_friday-2026",
                            "nike-US-black_friday-2026"]
    nike = base["nike-US-black_friday-2026"]
    assert nike["status"] == "confirmed"
    assert nike["start"] == "2026-11-20"  # 传闻博客说 11-22，级别低，改不了 CNBC 的日期
    assert len(nike["sources"]) == 2
    assert base["lululemon-US-black_friday-2026"]["sources"][0]["level"] == "official"
    assert base["nike-CA-black_friday-2026"]["status"] == "confirmed"  # retail-insider 在白名单里
    assert base["canadiantire-CA-black_friday-2026"]["status"] == "rumored"
    # 引句只有日期（出自标题），类型校正时连标题一起看：标题里有 Black Friday，所以仍是 black_friday
    ct_src = base["canadiantire-CA-black_friday-2026"]["sources"][0]
    assert ct_src["quote"] == "up to 60% off, Nov 20 - Nov 27" and ct_src["quote_in"] == "title"
    assert "nike-US-cyber_monday-2026" not in base      # 引句对不上，丢掉
    assert "canadiantire-CA-boxing_day-2025" not in base  # 年份不对，丢掉

    d = build_web.parse(web.read_text(encoding="utf-8"))
    sales = {s["id"]: s for s in d["sales"]}
    assert "amazon-unknown-other-2026-airpodpro3" not in sales      # overrides 删除
    ct = sales["canadiantire-CA-black_friday-2026"]
    assert ct["start"] == "2026-11-21" and ct["status"] == "confirmed"  # overrides 改字段
    assert d["meta"]["sample"] is False

    ch = load(data / "news" / "changes.json")
    assert ch["first_run"] is True and ch["failed_sources"] == []
    assert sorted(c["id"] for c in ch["changes"]) == sorted(sales)
    seen = load(data / "news" / "seen.json")
    assert "CBMiNIKE001" in seen and "CBMiNIKEOLD" not in seen
    health = load(data / "news" / "source_health.json")
    assert health["gnews-camedia1"]["last_success"] and health["gnews-camedia1"]["consecutive_failures"] == 0


def test_second_run_no_duplicates(env):
    args, data, web = env
    assert main(args) == 0
    first = load(data / "news" / "sales.json")["events"]
    assert main(args) == 0
    second = load(data / "news" / "sales.json")["events"]
    ids = [e["id"] for e in second]
    assert len(ids) == len(set(ids)) == len(first)
    assert [len(e["sources"]) for e in second] == [len(e["sources"]) for e in first]
    ch = load(data / "news" / "changes.json")
    assert ch["first_run"] is False and ch["changes"] == []


def test_second_run_without_seen_still_no_duplicates(env):
    args, data, web = env
    main(args)
    first = load(data / "news" / "sales.json")["events"]
    (data / "news" / "seen.json").unlink()  # 就算去重记录丢了，合并也不会产生重复
    main(args)
    second = load(data / "news" / "sales.json")["events"]
    assert [e["id"] for e in second] == [e["id"] for e in first]
    assert [len(e["sources"]) for e in second] == [len(e["sources"]) for e in first]
    assert load(data / "news" / "changes.json")["changes"] == []


def test_dry_run_writes_nothing(env):
    args, data, web = env
    assert main(args + ["--dry-run"]) == 0
    assert not data.exists() and not web.exists()


def test_no_ai_dry_run(env, caplog):
    args, data, web = env
    assert main(args + ["--no-ai", "--dry-run"]) == 0
    out = caplog.text
    assert "预筛" in out and "保留 6 条" in out
    assert not data.exists() and not web.exists()


def test_failed_source_keeps_events_and_is_reported(env, tmp_path):
    args, data, web = env
    assert main(args) == 0
    before = {e["id"]: e for e in load(data / "news" / "sales.json")["events"]}

    broken = tmp_path / "fixture2"
    shutil.copytree(FIX, broken)
    (broken / "feeds" / "gnews-camedia1.xml").write_text("<html>Service Unavailable", encoding="utf-8")
    (data / "news" / "seen.json").unlink()  # 让其他源的条目再走一遍
    args2 = list(args)
    args2[args2.index("--fixture") + 1] = str(broken)
    assert main(args2) == 0

    after = {e["id"]: e for e in load(data / "news" / "sales.json")["events"]}
    assert after == before  # 加拿大媒体源今天失败：加拿大轮胎的事件原样保留，不删不降级
    ch = load(data / "news" / "changes.json")
    assert [f["id"] for f in ch["failed_sources"]] == ["gnews-camedia1"] and ch["changes"] == []
    h = load(data / "news" / "source_health.json")["gnews-camedia1"]
    assert h["consecutive_failures"] == 1 and h["last_success"] and h["last_error"].startswith("解析出错")


# ---------- S3 v3：抓正文出错不影响整体；抓到正文的条目优先进上限 ----------
from dealradar import run as runmod  # noqa: E402
from dealradar.extract import FakeExtractor  # noqa: E402
from dealradar.sources import load_fixture  # noqa: E402


class DummyClient:
    usage = {"prompt_tokens": 0, "completion_tokens": 0}

    def __init__(self, *a, **k):
        pass

    def close(self):
        pass


def _online_like(monkeypatch, fetch_articles):
    """不联网地走非 fixture 的流程：抓取换成本地样本，AI 换成假的提取结果。"""
    monkeypatch.setattr(runmod, "fetch_all", lambda feeds, s: load_fixture(FIX, feeds))
    monkeypatch.setattr(runmod, "fetch_articles", fetch_articles)
    monkeypatch.setattr(runmod, "ChatClient", DummyClient)
    monkeypatch.setattr(runmod, "LLMExtractor",
                        lambda *a, **k: FakeExtractor(load(FIX / "extractions.json")))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")


def test_article_errors_do_not_stop_run(env, monkeypatch, caplog):
    args, data, web = env

    def boom(*a, **k):
        raise RuntimeError("article module broke")
    _online_like(monkeypatch, boom)
    no_fixture = [a for i, a in enumerate(args) if a != "--fixture" and (i == 0 or args[i - 1] != "--fixture")]
    assert runmod.main(no_fixture) == 0
    assert "抓正文出错" in caplog.text
    assert "nike-US-black_friday-2026" in {e["id"] for e in load(data / "news" / "sales.json")["events"]}


def test_items_with_excerpt_enter_cap_first(env, monkeypatch, tmp_path):
    args, data, web = env
    conf = tmp_path / "config"
    s = (conf / "settings.yaml").read_text(encoding="utf-8").replace("max_items_per_run: 300", "max_items_per_run: 2")
    (conf / "settings.yaml").write_text(s, encoding="utf-8")
    marked, batches = [], []

    class Stats:
        def describe(self):
            return "ok"

    def fake_articles(items, brands, settings):
        oldest = min(items, key=lambda it: it.get("published") or "")   # 按“新的优先”本来进不了前 2
        oldest["excerpt"] = "Nike Black Friday sale starts Nov. 20."
        marked.append(oldest["guid"])
        return Stats()

    class Capture(FakeExtractor):
        def extract_batch(self, batch):
            batches.append([it["guid"] for it in batch])
            return super().extract_batch(batch)

    _online_like(monkeypatch, fake_articles)
    monkeypatch.setattr(runmod, "LLMExtractor", lambda *a, **k: Capture({}))
    no_fixture = [a for i, a in enumerate(args) if a != "--fixture" and (i == 0 or args[i - 1] != "--fixture")]
    assert runmod.main(no_fixture + ["--dry-run"]) == 0
    flat = [g for b in batches for g in b]
    assert len(flat) == 2 and flat[0] == marked[0]        # 带正文的先入选、排在第一批


# ---------- S3 v4：候选存档 ----------
def test_candidate_archive_written_without_article_text(env, tmp_path):
    args, data, web = env
    assert main(args) == 0
    files = list((data / "debug").glob("candidates-*.jsonl"))
    assert len(files) == 1
    rows = [json.loads(line) for line in files[0].read_text(encoding="utf-8").splitlines()]
    assert rows and all(not ({"title", "summary", "excerpt"} & set(r)) for r in rows)
    for k in ("brand_id", "event_type", "start", "end", "ai_start", "ai_end", "quote", "url", "published",
              "quote_in", "event_id"):
        assert k in rows[0], k
    ids = {e["id"] for e in load(data / "news" / "sales.json")["events"]}
    assert {r["event_id"] for r in rows if r["kept"]} <= ids
    # 旧存档超过 7 天自动删掉
    old = data / "debug" / "candidates-2026-01-01.jsonl"
    old.write_text("{}\n", encoding="utf-8")
    assert main(args) == 0
    assert not old.exists()


def test_candidate_archive_can_be_turned_off(env, tmp_path):
    args, data, web = env
    conf = tmp_path / "config"
    s = (conf / "settings.yaml").read_text(encoding="utf-8").replace("debug_candidates: true", "debug_candidates: false")
    (conf / "settings.yaml").write_text(s, encoding="utf-8")
    assert main(args) == 0
    assert not (data / "debug").exists()


# ---------- §0.1 修订：文件分工 ----------
def test_web_output_off_by_default_and_news_only_writes_data_news(tmp_path):
    conf = tmp_path / "config"
    shutil.copytree(APP / "config", conf)
    (conf / "manual_events.yaml").write_text("events: []\n", encoding="utf-8")
    data = tmp_path / "data"
    assert main(["--fixture", str(FIX), "--config-dir", str(conf), "--data-dir", str(data), "--no-push"]) == 0
    assert sorted(p.name for p in data.iterdir()) == ["debug", "news"]          # 没有 data/local/、没有旧的 data/sales.json
    assert not list(tmp_path.rglob("data.js"))                                   # 不再写网页数据
