"""combine：新闻 + 官网 + 人工记录 + 人工纠错 → final（§0.1 修订：每次从头算；优先级 overrides > manual > official > 新闻）。"""
from __future__ import annotations

import copy
import json
import shutil

import yaml

from conftest import APP
from dealradar.combine import build_final, combine, events_of, load_manual, rebuild_part
from dealradar.config import Paths, load_config
from dealradar.manual import load_manual_candidates
from dealradar.merge import merge_candidates
from dealradar.status import finalize_candidates

T1 = "2026-11-01T12:00:00+00:00"
T2 = "2026-11-02T12:00:00+00:00"
EVENT_FIELDS = {"id", "brand_id", "region", "event_type", "event_name", "start", "end", "discount", "status",
                "sources", "first_seen", "updated"}


def manual(cfg, **kw):
    ev = {"brand_id": "nike", "region": "US", "event_type": "black_friday", "event_name": "Nike Black Friday",
          "start": "2026-11-21", "end": None, "discount": None,
          "quote": "Black Friday starts November 21.", "url": "https://www.nike.com/bf-manual", "seen": "2026-11-01"}
    ev.update(kw)
    return finalize_candidates(load_manual_candidates([ev], cfg.brands, 2026), cfg.brands, {})


def news_events(make_candidate, **kw):
    kw.setdefault("start", "2026-11-22")
    return merge_candidates([], [make_candidate(level="other", url="https://blog.example.com/nike", **kw)], T1)


def official_events(make_candidate, **kw):
    kw.setdefault("start", "2026-11-20")
    return merge_candidates([], [make_candidate(level="official", url="https://www.nike.com/black-friday",
                                                published=T2, **kw)], T2)


def test_official_beats_news(make_candidate):
    news = news_events(make_candidate, discount="up to 30% off")
    off = official_events(make_candidate, discount="up to 50% off")
    final = combine(news, off, [], {})
    assert len(final) == 1
    e = final[0]
    assert e["id"] == "nike-US-black_friday-2026"
    assert (e["start"], e["status"], e["discount"]) == ("2026-11-20", "confirmed", "up to 50% off")
    assert {s["url"] for s in e["sources"]} == {"https://blog.example.com/nike", "https://www.nike.com/black-friday"}
    assert e["first_seen"] == T1 and e["updated"] == T2          # 早的 first_seen、晚的 updated
    assert EVENT_FIELDS <= set(e)


def test_manual_beats_official_and_overrides_beat_manual(cfg, make_candidate):
    news, off = news_events(make_candidate), official_events(make_candidate)
    final = combine(news, off, manual(cfg), {})
    assert final[0]["start"] == "2026-11-21"                     # manual > official
    assert any(s.get("manual") for s in final[0]["sources"])
    ov = {"nike-US-black_friday-2026": {"start": "2026-11-23", "status": "rumored"}}
    e = combine(news, off, manual(cfg), ov)[0]
    assert (e["start"], e["status"], e["overridden"]) == ("2026-11-23", "rumored", ["start", "status"])
    assert combine(news, off, manual(cfg), {"nike-US-black_friday-2026": {"delete": True}}) == []


def test_pure_and_recomputed_from_scratch(cfg, make_candidate):
    news, off, man = news_events(make_candidate), official_events(make_candidate), manual(cfg, brand_id="gap",
                                                                                           url="https://www.gap.com/x")
    before = copy.deepcopy((news, off, man))
    a = combine(news, off, man, {})
    assert (news, off, man) == before                            # 不改动传入的参数
    assert combine(news, off, man, {}) == a                      # 同样的输入 → 同样的输出
    assert {e["id"] for e in a} == {"nike-US-black_friday-2026", "gap-US-black_friday-2026"}
    gap = next(e for e in a if e["brand_id"] == "gap")
    assert gap["first_seen"] == "2026-11-01T00:00:00+00:00"      # 人工记录新建的：用查看日期
    # 不累积：这次不给人工记录，它就没了；也不会因为上一次的结果多出东西
    b = combine(news, off, [], {})
    assert [e["id"] for e in b] == ["nike-US-black_friday-2026"]
    assert combine(news, [], [], {})[0]["start"] == "2026-11-22"   # 没有官网部分时只看新闻


def test_official_only_and_news_only_events_both_kept(make_candidate):
    news = news_events(make_candidate, brand_id="gap")
    off = official_events(make_candidate)
    ids = [e["id"] for e in combine(news, off, [], {})]
    assert ids == ["gap-US-black_friday-2026", "nike-US-black_friday-2026"]


def test_unnamed_other_from_official_folds_into_news_event(make_candidate):
    news = news_events(make_candidate, start="2026-11-20", end="2026-11-30")
    off = merge_candidates([], [make_candidate(level="official", url="https://www.nike.com/deals",
                                               event_type="other", start="2026-11-20", sale_key="2026-11-20",
                                               sale_key_kind="start")], T2)
    assert off[0]["id"] == "nike-US-other-2026-2026-11-20"
    final = combine(news, off, [], {})
    assert [e["id"] for e in final] == ["nike-US-black_friday-2026"]
    assert final[0]["status"] == "confirmed" and len(final[0]["sources"]) == 2


def test_rebuild_part_recomputes_dates_and_status(make_candidate):
    both = merge_candidates([], [make_candidate(level="other", url="https://blog/a", start="2026-11-22"),
                                 make_candidate(level="official", url="https://www.nike.com/a", start="2026-11-20",
                                                discount="up to 50% off")], T1)[0]
    assert (both["start"], both["status"]) == ("2026-11-20", "confirmed")
    news_src = [s for s in both["sources"] if s["level"] == "other"]
    part = rebuild_part(both, news_src)
    assert (part["start"], part["status"]) == ("2026-11-22", "rumored")
    assert part["field_levels"]["discount"] == 1                 # 级别不超过留下的来源
    assert both["start"] == "2026-11-20"                          # 不改动原事件


def test_build_final_writes_final_json(tmp_path, make_candidate):
    conf = tmp_path / "config"
    shutil.copytree(APP / "config", conf)
    (conf / "overrides.yaml").write_text("events: {}\n", encoding="utf-8")
    (conf / "manual_events.yaml").write_text("events: []\n", encoding="utf-8")
    paths = Paths(config_dir=conf, data_dir=tmp_path / "data")
    paths.news_dir.mkdir(parents=True)
    paths.sales.write_text(json.dumps({"updated": T1, "events": news_events(make_candidate)}), encoding="utf-8")
    paths.local_dir.mkdir(parents=True)
    paths.official_base.write_text(json.dumps({"updated": T2, "events": official_events(make_candidate)}),
                                   encoding="utf-8")
    doc = build_final(paths, now_iso="2026-11-03T00:00:00+00:00")
    on_disk = json.loads(paths.final.read_text(encoding="utf-8"))
    assert on_disk == doc and list(doc) == ["generated_at", "events"]
    assert doc["generated_at"] == "2026-11-03T00:00:00+00:00" and doc["events"][0]["start"] == "2026-11-20"
    # news_cache.json 有就优先用它（Mac 从云端取来的）
    paths.news_cache.write_text(json.dumps({"events": news_events(make_candidate, brand_id="gap")}), encoding="utf-8")
    ids = [e["id"] for e in build_final(paths)["events"]]
    assert ids == ["gap-US-black_friday-2026", "nike-US-black_friday-2026"]


def test_load_manual_reads_yaml(tmp_path):
    conf = tmp_path / "config"
    shutil.copytree(APP / "config", conf)
    ev = {"brand_id": "nike", "region": "US", "event_type": "other", "event_name": "Member Days",
          "start": "2026-11-10", "end": None, "discount": None, "quote": "Member Days start November 10.",
          "url": "https://www.nike.com/m", "seen": "2026-11-01"}
    (conf / "manual_events.yaml").write_text(yaml.safe_dump({"events": [ev]}), encoding="utf-8")
    cands = load_manual(load_config(Paths(config_dir=conf)))
    assert len(cands) == 1 and cands[0]["sale_key"] == "memberday" and cands[0]["source"]["manual"] is True
    assert events_of(None) == [] and events_of({"events": [1]}) == [1]
