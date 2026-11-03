from __future__ import annotations

import json
import shutil

import pytest
import yaml

from conftest import APP, FIXTURES
from dealradar.combine import build_final
from dealradar.config import Paths, load_yaml
from dealradar.manual import load_manual_candidates
from dealradar.merge import merge_run
from dealradar.run import main
from dealradar.status import finalize_candidates

T1 = "2026-11-01T12:00:00+00:00"
GOOD = {"brand_id": "bestbuy", "region": "CA", "event_type": "early_bf", "event_name": "My Best Buy member early access",
        "start": "2026-11-02", "end": "2026-11-05", "discount": None,
        "quote": "From November 2 to 5, shop Black Friday prices on select products when you're signed in.",
        "url": "https://www.bestbuy.ca/en-ca", "seen": "2026-11-01"}


def test_example_manual_file_is_valid(cfg):
    """config/manual_events.example.yaml（进仓库的样例）能读、没有真实记录。"""
    doc = load_yaml(APP / "config" / "manual_events.example.yaml")
    assert doc.get("events") == []
    text = (APP / "config" / "manual_events.example.yaml").read_text(encoding="utf-8")
    for field in ("brand_id", "region", "event_type", "quote", "url", "seen"):
        assert f"#   {field}" in text or f"#     {field}" in text, field


def test_real_manual_file_is_valid(cfg):
    path = APP / "config" / "manual_events.yaml"
    if not path.exists():                       # 本地文件（不进仓库）：云端没有就跳过
        pytest.skip("没有 config/manual_events.yaml（本地文件）")
    entries = load_yaml(path)["events"]
    dropped = {}
    cands = load_manual_candidates(entries, cfg.brands, 2026, dropped)
    assert len(cands) == len(entries) >= 2 and dropped == {}        # 文件里每一条都要合格（条数会随人工记录增加）
    assert [(c["event_type"], c["start"], c["end"]) for c in cands[:2]] == [
        ("early_bf", "2026-11-02", "2026-11-05"), ("black_friday", "2026-11-06", None)]
    src = cands[0]["source"]
    assert (src["level"], src["quote_in"], src["manual"], src["published"][:10]) == \
        ("official", "official_page", True, "2026-11-01")
    # 引句里没有 Black Friday 字样也保持 black_friday：人工记录跳过类型校正
    assert [c["event_type"] for c in finalize_candidates(cands[:2], cfg.brands, {})] == ["early_bf", "black_friday"]


def test_invalid_manual_entries_dropped(cfg, caplog):
    bad = [dict(GOOD, brand_id="zara"), dict(GOOD, start="2026-11-03"), dict(GOOD, end="2026/11/05"),
           dict(GOOD, region="unknown"), dict(GOOD, url="bestbuy.ca"), dict(GOOD, quote=""),
           dict(GOOD, event_type="flash")]
    dropped = {}
    assert load_manual_candidates(bad + [GOOD], cfg.brands, 2026, dropped) == \
        load_manual_candidates([GOOD], cfg.brands, 2026)
    assert dropped == {"人工记录不合格": 7}
    assert "manual_events.yaml 第 2 条不合格" in caplog.text and "不在原文 quote 里" in caplog.text


def test_manual_merges_as_confirmed_and_overrides_win(cfg, make_candidate):
    man = finalize_candidates(load_manual_candidates([GOOD], cfg.brands, 2026), cfg.brands, {})
    ai = make_candidate(brand_id="bestbuy", region="CA", event_type="early_bf", url="https://blog/1",
                        start="2026-11-03", quote="early Black Friday from 2026-11-03")
    base, final, _ = merge_run([], ai and [ai] + man, {}, T1)
    e = final[0]
    assert e["id"] == "bestbuy-CA-early_bf-2026" and e["status"] == "confirmed"
    assert (e["start"], e["end"]) == ("2026-11-02", "2026-11-05")       # manual（官网级）胜过 AI 传闻
    _, final2, _ = merge_run([], [ai] + man, {"bestbuy-CA-early_bf-2026": {"start": "2026-11-04",
                                                                           "status": "rumored"}}, T1)
    assert (final2[0]["start"], final2[0]["status"]) == ("2026-11-04", "rumored")   # overrides > manual
    assert base[0]["start"] == "2026-11-02"                              # sales.json 里仍是合并结果


def test_combine_reads_manual_events(tmp_path):
    """人工记录不进 data/news/sales.json，每次由 combine 从 manual_events.yaml 读（§0.1 修订）。"""
    conf = tmp_path / "config"
    shutil.copytree(APP / "config", conf)
    shutil.copy(FIXTURES / "run" / "overrides.yaml", conf / "overrides.yaml")
    bf = dict(GOOD, event_type="black_friday", event_name="Black Friday", start="2026-11-06", end=None,
              quote="Black Friday deals start November 6.")
    (conf / "manual_events.yaml").write_text(yaml.safe_dump({"events": [GOOD, bf]}, allow_unicode=True),
                                             encoding="utf-8")
    data = tmp_path / "data"
    assert main(["--fixture", str(FIXTURES / "run"), "--config-dir", str(conf), "--data-dir", str(data)]) == 0
    news = json.loads((data / "news" / "sales.json").read_text(encoding="utf-8"))["events"]
    assert not any(s.get("manual") for e in news for s in e["sources"])          # 新闻部分里没有人工记录
    paths = Paths(config_dir=conf, data_dir=data)
    doc = build_final(paths)
    events = {e["id"]: e for e in doc["events"]}
    ea, bfe = events["bestbuy-CA-early_bf-2026"], events["bestbuy-CA-black_friday-2026"]
    assert (ea["start"], ea["end"], ea["status"]) == ("2026-11-02", "2026-11-05", "confirmed")
    assert (bfe["start"], bfe["end"], bfe["status"]) == ("2026-11-06", None, "confirmed")
    assert ea["sources"][0]["manual"] is True and ea["sources"][0]["quote_in"] == "official_page"
    assert ea["first_seen"] == "2026-11-01T00:00:00+00:00"                      # 用查看日期，不随运行时间变
    # 从 yaml 删掉一条，下次算出来就没有了（每次从头算，不累积）
    (conf / "manual_events.yaml").write_text(yaml.safe_dump({"events": [bf]}), encoding="utf-8")
    assert "bestbuy-CA-early_bf-2026" not in {e["id"] for e in build_final(paths)["events"]}
