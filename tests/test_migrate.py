"""scripts/migrate_split.py：旧 data/sales.json 按来源拆成 news / official，人工记录不存（§0.1 修订）。"""
from __future__ import annotations

import importlib.util
import json
import shutil

import pytest
import yaml

from conftest import APP
from dealradar.combine import combine
from dealradar.merge import merge_candidates

spec = importlib.util.spec_from_file_location("migrate_split", APP / "scripts" / "migrate_split.py")
mig = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mig)

T1 = "2026-11-01T12:00:00+00:00"
OFF_URL = "https://www.nike.com/black-friday"
MANUAL = {"brand_id": "nike", "region": "US", "event_type": "black_friday", "event_name": "Nike Black Friday",
          "start": "2026-11-21", "end": None, "discount": None, "quote": "Black Friday starts November 21.",
          "url": "https://www.nike.com/bf-manual", "seen": "2026-11-01"}


@pytest.fixture
def old_data(tmp_path, make_candidate, cfg):
    """旧格式的 data/：sales.json 里一个事件同时有新闻、官网监控、人工来源，另一个只有新闻。"""
    from dealradar.manual import load_manual_candidates
    from dealradar.status import finalize_candidates
    off = make_candidate(level="official", url=OFF_URL, start="2026-11-20")
    off["source"]["name"] = "Nike official website (nike.com)"
    news = make_candidate(level="other", url="https://blog.example.com/nike", start="2026-11-22")
    gap = make_candidate(level="media", url="https://www.cnbc.com/gap", brand_id="gap", discount="up to 40% off")
    man = finalize_candidates(load_manual_candidates([MANUAL], cfg.brands, 2026), cfg.brands, {})
    events = merge_candidates([], [off, news, gap] + man, T1)
    conf = tmp_path / "config"
    shutil.copytree(APP / "config", conf)
    (conf / "overrides.yaml").write_text("events: {}\n", encoding="utf-8")
    (conf / "manual_events.yaml").write_text(yaml.safe_dump({"events": [MANUAL]}), encoding="utf-8")
    data = tmp_path / "data"
    data.mkdir()
    (data / "sales.json").write_text(json.dumps({"updated": T1, "events": events}), encoding="utf-8")
    (data / "official_state.json").write_text(json.dumps({"pages": {OFF_URL: {"hash": "h"}}}), encoding="utf-8")
    for name in ("seen.json", "source_health.json", "changes.json", "manual_review.json"):
        (data / name).write_text("{}", encoding="utf-8")
    return data, conf, events


def test_source_kind():
    assert mig.source_kind({"manual": True, "level": "official"}, set()) == "manual"
    assert mig.source_kind({"level": "official", "name": "Gap official website (gap.com)"}, set()) == "official"
    assert mig.source_kind({"level": "official", "url": "https://x/p"}, {"https://x/p"}) == "official"
    # 品牌自己的新闻稿（官网级，但不是官网监控抓的）算新闻
    assert mig.source_kind({"level": "official", "name": "About Amazon", "url": "https://a/n"}, set()) == "news"
    assert mig.source_kind({"level": "other"}, set()) == "news"


def test_dry_run_writes_nothing(old_data, capsys):
    data, conf, _ = old_data
    before = sorted(p.name for p in data.iterdir())
    assert mig.main(["--data-dir", str(data), "--config-dir", str(conf)]) == 0
    out = capsys.readouterr().out
    assert sorted(p.name for p in data.iterdir()) == before
    assert "新闻 2，官网监控 1" in out and "人工记录 1" in out and "和原来完全一致" in out and "dry-run" in out


def test_apply_splits_and_round_trips(old_data, cfg, capsys):
    data, conf, original = old_data
    assert mig.main(["--data-dir", str(data), "--config-dir", str(conf), "--apply"]) == 0
    news = json.loads((data / "news" / "sales.json").read_text(encoding="utf-8"))["events"]
    off = json.loads((data / "local" / "official_base.json").read_text(encoding="utf-8"))["events"]
    assert [e["id"] for e in news] == ["gap-US-black_friday-2026", "nike-US-black_friday-2026"]
    assert [e["id"] for e in off] == ["nike-US-black_friday-2026"]
    assert [s["url"] for s in off[0]["sources"]] == [OFF_URL]
    assert not any(s.get("manual") for e in news + off for s in e["sources"])         # 人工记录不存
    nike_news = next(e for e in news if e["brand_id"] == "nike")
    assert (nike_news["start"], nike_news["status"]) == ("2026-11-22", "rumored")      # 按留下的来源重算
    # 原文件留档、其余文件挪到新位置
    assert not (data / "sales.json").exists() and (data / "sales.pre-split.json").exists()
    assert sorted(p.name for p in (data / "news").iterdir()) == ["changes.json", "sales.json", "seen.json",
                                                                 "source_health.json"]
    assert sorted(p.name for p in (data / "local").iterdir()) == ["manual_review.json", "official_base.json",
                                                                  "official_state.json"]
    # 合回去和原来一致
    from dealradar.combine import load_manual
    from dealradar.config import Paths, load_config
    back = {e["id"]: e for e in combine(news, off, load_manual(load_config(Paths(config_dir=conf))), {})}
    orig = {e["id"]: e for e in original}
    assert set(back) == set(orig)
    for i in orig:
        for f in ("start", "end", "status", "discount", "event_name"):
            assert back[i].get(f) == orig[i].get(f), (i, f)
        assert len(back[i]["sources"]) == len(orig[i]["sources"])
    # 再跑一次：已经迁移过，不动
    capsys.readouterr()
    assert mig.main(["--data-dir", str(data), "--config-dir", str(conf), "--apply"]) == 0
    assert "不需要迁移" in capsys.readouterr().out


def test_refuses_to_overwrite(old_data, capsys):
    data, conf, _ = old_data
    (data / "news").mkdir()
    (data / "news" / "sales.json").write_text("{}", encoding="utf-8")
    assert mig.main(["--data-dir", str(data), "--config-dir", str(conf), "--apply"]) == 2
    assert (data / "sales.json").exists()
