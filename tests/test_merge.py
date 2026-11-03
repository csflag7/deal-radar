from __future__ import annotations

from datetime import date

from dealradar.merge import apply_overrides, diff_changes, merge_candidates, merge_run

T1 = "2026-11-01T12:00:00+00:00"
T2 = "2026-11-02T12:00:00+00:00"


def one(events, eid):
    hits = [e for e in events if e["id"] == eid]
    assert len(hits) == 1, [e["id"] for e in events]
    return hits[0]


def test_same_key_becomes_one_event(make_candidate):
    a = make_candidate(url="https://a.com/1", start="2026-11-20")
    b = make_candidate(url="https://b.com/2", discount="40% off")
    ev = merge_candidates([], [a, b], T1)
    assert len(ev) == 1
    e = ev[0]
    assert e["id"] == "nike-US-black_friday-2026"
    assert e["start"] == "2026-11-20" and e["discount"] == "40% off"
    assert [s["url"] for s in e["sources"]] == ["https://a.com/1", "https://b.com/2"]
    assert e["first_seen"] == T1 and e["updated"] == T1


def test_sources_deduplicated(make_candidate):
    a = make_candidate(url="https://a.com/1", title="Nike sale")
    ev = merge_candidates([], [a, make_candidate(url="https://a.com/1", title="Nike sale (updated)")], T1)
    ev = merge_candidates(ev, [make_candidate(url="https://a.com/1?x", title="Nike  SALE")], T2)
    assert len(ev[0]["sources"]) == 1
    assert ev[0]["updated"] == T1


def test_different_keys_stay_apart(make_candidate):
    ev = merge_candidates([], [
        make_candidate(region="US"), make_candidate(region="unknown", url="https://u"),
        make_candidate(region="CA", url="https://c"), make_candidate(event_type="cyber_monday", url="https://m"),
    ], T1)
    assert sorted(e["id"] for e in ev) == sorted([
        "nike-US-black_friday-2026", "nike-unknown-black_friday-2026",
        "nike-CA-black_friday-2026", "nike-US-cyber_monday-2026"])


def test_status_only_goes_up(make_candidate):
    ev = merge_candidates([], [make_candidate(level="other", url="https://r1")], T1)
    assert ev[0]["status"] == "rumored"
    ev = merge_candidates(ev, [make_candidate(level="media", url="https://m1")], T2)
    assert ev[0]["status"] == "confirmed"
    ev = merge_candidates(ev, [make_candidate(level="other", url="https://r2")], T2)
    assert ev[0]["status"] == "confirmed"


def test_lower_source_cannot_change_dates(make_candidate):
    ev = merge_candidates([], [make_candidate(level="media", url="https://m", start="2026-11-20", end="2026-11-30")], T1)
    ev = merge_candidates(ev, [make_candidate(level="other", url="https://r", start="2026-11-22", end="2026-12-01")], T2)
    assert (ev[0]["start"], ev[0]["end"]) == ("2026-11-20", "2026-11-30")
    assert len(ev[0]["sources"]) == 2  # 来源仍然记下
    # 同级可以改
    ev = merge_candidates(ev, [make_candidate(level="media", url="https://m2", start="2026-11-21")], T2)
    assert ev[0]["start"] == "2026-11-21"
    # 更高级可以改
    ev = merge_candidates(ev, [make_candidate(level="official", url="https://o", start="2026-11-19")], T2)
    assert ev[0]["start"] == "2026-11-19"
    # 官网定下的日期，媒体也改不了
    ev = merge_candidates(ev, [make_candidate(level="media", url="https://m3", start="2026-11-25")], T2)
    assert ev[0]["start"] == "2026-11-19"


def test_empty_field_can_be_filled_by_any_source(make_candidate):
    ev = merge_candidates([], [make_candidate(level="official", url="https://o")], T1)
    ev = merge_candidates(ev, [make_candidate(level="other", url="https://r", start="2026-11-22")], T2)
    assert ev[0]["start"] == "2026-11-22" and ev[0]["status"] == "confirmed"


def test_failed_source_does_not_delete_or_downgrade(make_candidate):
    ev = merge_candidates([], [make_candidate(level="media", url="https://m", start="2026-11-20")], T1)
    again = merge_candidates(ev, [], T2)
    assert again == ev


def test_repeat_merge_no_duplicates(make_candidate):
    cands = [make_candidate(url="https://a", start="2026-11-20"), make_candidate(region="CA", url="https://b")]
    ev1 = merge_candidates([], cands, T1)
    ev2 = merge_candidates(ev1, cands, T2)
    assert ev1 == ev2
    assert diff_changes(ev1, ev2) == []


def test_newer_same_level_source_wins_within_run(make_candidate):
    old = make_candidate(url="https://a", start="2026-11-20", published="2026-10-20T00:00:00+00:00")
    new = make_candidate(url="https://b", start="2026-11-21", published="2026-10-30T00:00:00+00:00")
    assert merge_candidates([], [new, old], T1)[0]["start"] == "2026-11-21"


def test_changes_list(make_candidate):
    ev1 = merge_candidates([], [make_candidate(url="https://r", start="2026-11-20")], T1)
    ev2 = merge_candidates(ev1, [make_candidate(level="media", url="https://m", start="2026-11-21"),
                                 make_candidate(region="CA", url="https://c")], T2)
    kinds = sorted((c["kind"], c["id"]) for c in diff_changes(ev1, ev2))
    assert kinds == [("confirmed", "nike-US-black_friday-2026"), ("date_changed", "nike-US-black_friday-2026"),
                     ("new_event", "nike-CA-black_friday-2026")]
    dc = [c for c in diff_changes(ev1, ev2) if c["kind"] == "date_changed"][0]
    assert dc["old_start"] == "2026-11-20" and dc["start"] == "2026-11-21"


def test_overrides_win_over_ai(make_candidate):
    overrides = {
        "nike-US-black_friday-2026": {"start": date(2026, 11, 18), "status": "rumored", "discount": "人工"},
        "nike-CA-black_friday-2026": {"delete": True},
        "nike-US-boxing_day-2026": {"start": "2026-12-26"},  # 还不存在的事件：忽略
    }
    cands = [make_candidate(level="official", url="https://o", start="2026-11-20"),
             make_candidate(region="CA", url="https://c")]
    base, final, changes = merge_run([], cands, overrides, T1)
    assert len(base) == 2  # sales.json 里保留 AI 原始合并结果
    assert [e["id"] for e in final] == ["nike-US-black_friday-2026"]
    e = final[0]
    assert (e["start"], e["status"], e["discount"]) == ("2026-11-18", "rumored", "人工")
    assert [c["kind"] for c in changes] == ["new_event"] and changes[0]["start"] == "2026-11-18"

    # 第二天 AI 又看到新日期，最终输出仍然以人工为准，也不产生“日期变动”
    base2, final2, changes2 = merge_run(base, [make_candidate(level="official", url="https://o2", start="2026-11-21")],
                                        overrides, T2)
    assert one(base2, "nike-US-black_friday-2026")["start"] == "2026-11-21"
    assert one(final2, "nike-US-black_friday-2026")["start"] == "2026-11-18"
    assert "nike-CA-black_friday-2026" not in [e["id"] for e in final2]
    assert changes2 == []


def test_apply_overrides_does_not_touch_input(make_candidate):
    ev = merge_candidates([], [make_candidate(url="https://a", start="2026-11-20")], T1)
    out = apply_overrides(ev, {ev[0]["id"]: {"start": "2026-11-01", "bogus": 1, "status": "maybe"}})
    assert ev[0]["start"] == "2026-11-20"
    assert out[0]["start"] == "2026-11-01" and out[0]["status"] == "rumored"
    assert out[0]["overridden"] == ["start"]


# ---------- S3 v2：unknown 不升级、来源最多 8 个 ----------
from dealradar.merge import trim_sources  # noqa: E402


def test_unknown_evidence_never_upgrades_us_or_ca(make_candidate):
    us = make_candidate(region="US", level="other", url="https://blog/1")
    unk = make_candidate(region="unknown", level="official", url="https://www.nike.com/x", start="2026-11-20",
                         discount="50% off")
    ev = merge_candidates([], [us, unk], T1)
    by = {e["id"]: e for e in ev}
    assert set(by) == {"nike-US-black_friday-2026", "nike-unknown-black_friday-2026"}
    u = by["nike-US-black_friday-2026"]
    assert u["status"] == "rumored" and u["start"] is None and len(u["sources"]) == 1
    k = by["nike-unknown-black_friday-2026"]
    assert k["status"] == "confirmed" and k["start"] == "2026-11-20"
    # 以后再来 unknown 的证据，也只进 unknown 事件
    ev2 = merge_candidates(ev, [make_candidate(region="unknown", level="media", url="https://m/2",
                                               start="2026-11-21")], T2)
    assert {e["id"]: e for e in ev2}["nike-US-black_friday-2026"] == u


def test_event_status_is_highest_evidence_and_each_source_keeps_its_own(make_candidate):
    a = make_candidate(level="media", url="https://m/1", status="rumored")              # 权威媒体但没日期没折扣
    b = make_candidate(level="official", url="https://o/1", start="2026-11-20")          # confirmed
    c = make_candidate(level="other", url="https://r/1", start="2026-11-21")
    e = merge_candidates([], [a, b, c], T1)[0]
    assert e["status"] == "confirmed"
    assert {s["url"]: (s["level"], s["status"]) for s in e["sources"]} == {
        "https://m/1": ("media", "rumored"), "https://o/1": ("official", "confirmed"),
        "https://r/1": ("other", "rumored")}


def test_at_most_8_sources_priority(make_candidate):
    cands = []
    for i in range(5):   # 其他来源：较新、带日期的优先
        cands.append(make_candidate(level="other", url=f"https://r/{i}", start="2026-11-20" if i < 2 else None,
                                    published=f"2026-10-2{i}T00:00:00+00:00"))
    for i in range(3):
        cands.append(make_candidate(level="media", url=f"https://m/{i}", published="2026-10-01T00:00:00+00:00"))
    cands.append(make_candidate(level="official", url="https://o/0", published="2026-09-01T00:00:00+00:00"))
    cands.append(make_candidate(level="other", url="https://r/new", published="2026-10-31T00:00:00+00:00"))
    e = merge_candidates([], cands, T1)[0]
    urls = {s["url"] for s in e["sources"]}
    assert len(e["sources"]) == 8
    assert {"https://o/0", "https://m/0", "https://m/1", "https://m/2", "https://r/0", "https://r/1"} <= urls
    # 剩下 2 个名额给不带日期的里最新的：r/new（10-31）和 r/4（10-24）
    assert urls - {"https://o/0", "https://m/0", "https://m/1", "https://m/2", "https://r/0", "https://r/1"} == \
        {"https://r/new", "https://r/4"}
    # 被挤掉的来源下次再来，不会再加进去，也不算更新
    again = merge_candidates([e], [make_candidate(level="other", url="https://r/2",
                                                  published="2026-10-22T00:00:00+00:00")], T2)
    assert again[0]["sources"] == e["sources"] and again[0]["updated"] == T1


def test_trim_sources_keeps_order_and_short_lists():
    s = [{"url": str(i), "level": "other", "published": f"2026-10-{10 + i}"} for i in range(3)]
    assert trim_sources(s, 8) is s
    assert [x["url"] for x in trim_sources(s, 2)] == ["1", "2"]


# ---------- 第二轮：被挤掉的证据不能改日期和状态；日期、状态只看保留下来的来源 ----------
def test_evicted_evidence_changes_nothing(make_candidate):
    # 8 条已官宣证据（带折扣）占满名额；后来的传闻证据排不进去
    base = [make_candidate(level="official", url=f"https://o/{i}", discount="20% off") for i in range(8)]
    e = merge_candidates([], base, T1)[0]
    assert e["status"] == "confirmed" and e["start"] is None and len(e["sources"]) == 8
    late = [make_candidate(level="other", url="https://r/1", start="2026-11-20", discount="50% off"),
            make_candidate(level="media", url="https://m/1", status="rumored", event_name="Big Sale")]
    e2 = merge_candidates([e], late, T2)[0]
    assert e2 == e   # 两条都排不进前 8：不记来源，日期、状态、折扣、活动名都不变，也不算更新


def test_confirmed_and_dated_evidence_ranks_first(make_candidate):
    officials = [make_candidate(level="official", url=f"https://o/{i}", status="rumored") for i in range(8)]
    e = merge_candidates([], officials, T1)[0]
    dated = make_candidate(level="other", url="https://r/1", start="2026-11-20")            # 传闻，但带日期
    conf = make_candidate(level="media", url="https://m/1", discount="40% off")              # 已官宣
    e2 = merge_candidates([e], [dated, conf], T2)[0]
    urls = {s["url"] for s in e2["sources"]}
    assert {"https://r/1", "https://m/1"} <= urls and len(urls) == 8   # 挤掉两个没日期的官网传闻
    assert e2["start"] == "2026-11-20" and e2["status"] == "confirmed"


def test_dates_follow_kept_sources_when_a_source_is_pushed_out(make_candidate):
    first = make_candidate(level="other", url="https://r/1", start="2026-11-20", status="rumored")
    e = merge_candidates([], [first], T1)[0]
    assert e["start"] == "2026-11-20"
    confirmed = [make_candidate(level="official", url=f"https://o/{i}", discount="30% off") for i in range(8)]
    e2 = merge_candidates([e], confirmed, T2)[0]
    assert "https://r/1" not in {s["url"] for s in e2["sources"]}
    assert e2["start"] is None                     # 给出日期的来源被挤掉了，日期也跟着撤掉
    assert e2["status"] == "confirmed" and e2["updated"] == T2
    # 新日期为空（被撤掉）时不报“日期变动”
    assert [c["kind"] for c in diff_changes([e], [e2])] == ["confirmed"]


def test_status_only_goes_up_even_if_confirmed_source_is_pushed_out(make_candidate):
    a = make_candidate(level="media", url="https://m/1", discount="30% off")       # confirmed
    e = merge_candidates([], [a], T1)[0]
    assert e["status"] == "confirmed"
    more = [make_candidate(level="official", url=f"https://o/{i}", start="2026-11-2{i}", discount="10% off",
                           published=f"2026-10-2{i}T00:00:00+00:00") for i in range(8)]
    e2 = merge_candidates([e], more, T2)[0]
    assert "https://m/1" not in {s["url"] for s in e2["sources"]}
    assert e2["status"] == "confirmed"
    # 只有 overrides.yaml 能降级
    assert apply_overrides([e2], {e2["id"]: {"status": "rumored"}})[0]["status"] == "rumored"


def test_date_changed_only_reported_for_new_non_empty_dates(make_candidate):
    ev1 = merge_candidates([], [make_candidate(url="https://r", start="2026-11-20", end="2026-11-30")], T1)
    e2 = dict(ev1[0], end=None)
    assert diff_changes(ev1, [e2]) == []
    e3 = dict(ev1[0], start="2026-11-21")
    assert [c["kind"] for c in diff_changes(ev1, [e3])] == ["date_changed"]


def test_other_candidate_folds_into_matching_event(make_candidate):
    bf = make_candidate(event_type="black_friday", url="https://a/1", start="2026-11-20", end="2026-11-30")
    oth = make_candidate(event_type="other", url="https://b/1", start="2026-11-20", discount="up to 50% off")
    far = make_candidate(event_type="other", url="https://c/1", start="2026-12-05")
    ca = make_candidate(event_type="other", region="CA", url="https://d/1", start="2026-11-20")
    ev = merge_candidates([], [bf, oth, far, ca], T1)
    ids = sorted(e["id"] for e in ev)
    assert ids == ["nike-CA-other-2026-2026-11-20", "nike-US-black_friday-2026", "nike-US-other-2026-2026-12-05"]
    bfe = one(ev, "nike-US-black_friday-2026")
    assert {s["url"] for s in bfe["sources"]} == {"https://a/1", "https://b/1"}
    assert bfe["discount"] == "up to 50% off" and bfe["end"] == "2026-11-30"
    assert one(ev, "nike-US-other-2026-2026-12-05")["start"] == "2026-12-05"     # 开始日期不同：不并


def test_existing_other_event_folded_after_merge(make_candidate):
    # 先有 other 事件（当时还没有 black_friday 事件），后来出现日期相同的 black_friday 事件 → 合并后扫一遍并掉
    ev1 = merge_candidates([], [make_candidate(event_type="other", url="https://b/1", start="2026-11-20",
                                               end="2026-11-30")], T1)
    assert [e["id"] for e in ev1] == ["nike-US-other-2026-2026-11-20"]
    ev2 = merge_candidates(ev1, [make_candidate(event_type="black_friday", url="https://a/1", start="2026-11-20")], T2)
    assert [e["id"] for e in ev2] == ["nike-US-black_friday-2026"]
    e = ev2[0]
    assert {s["url"] for s in e["sources"]} == {"https://a/1", "https://b/1"}
    assert (e["start"], e["end"]) == ("2026-11-20", "2026-11-30") and e["first_seen"] == T1
    # end 不同（且都不为空）就不并
    ev3 = merge_candidates([], [make_candidate(event_type="other", url="https://b/1", start="2026-11-20", end="2026-11-22"),
                                make_candidate(event_type="black_friday", url="https://a/1", start="2026-11-20",
                                               end="2026-11-30")], T1)
    assert len(ev3) == 2


def test_same_source_again_fills_missing_date(make_candidate):
    a = make_candidate(level="media", url="https://m/1", status="rumored")
    b = make_candidate(level="media", url="https://m/1", start="2026-11-20")       # 同一篇文章的另一条证据
    e = merge_candidates([], [a, b], T1)[0]
    assert len(e["sources"]) == 1 and e["start"] == "2026-11-20" and e["status"] == "confirmed"
    assert e["sources"][0]["start"] == "2026-11-20" and e["sources"][0]["status"] == "confirmed"


# ---------- S3 v4：不同促销不能并进同一个事件 ----------
from dealradar.status import date_in_quote, finalize_candidates, judge  # noqa: E402


def bb(make_candidate, **kw):
    base = dict(brand_id="bestbuy", region="US", event_type="other")
    base.update(kw)
    return make_candidate(**base)


def all_source_dates_in_quotes(events):
    return all(date_in_quote(s[f], s["quote"]) for e in events for s in e["sources"] for f in ("start", "end")
               if s.get(f))


def test_same_url_two_candidates_become_two_other_events(cfg, make_item):
    url = "https://www.macrumors.com/2026/10/27/best-buy-black-friday-plans-26/"
    q_door = 'Best Buy will kick off its holiday sales with "DoorBOOsters" on the day before Halloween'
    q_last = ("starting December 18 and running through December 24 (aka Christmas Eve), you can expect to see "
              "more deals go live in what Best Buy is calling its Last-Minute Savings sale")
    item = make_item(source="bing", link=url, guid=url, source_url="https://www.macrumors.com", brand_hints=["bestbuy"],
                     title="Best Buy Details Black Friday Plans With Early Deals Available on Halloween",
                     excerpt=f"{q_door}.\n...\n{q_last}.", published="2026-10-27T10:28:00+00:00")
    ev = {"brand_id": "bestbuy", "region": "US", "event_type": "other", "year": 2026, "discount": None}
    pairs = [(item, dict(ev, event_name="DoorBOOsters", start="2026-12-18", end="2026-12-24", quote=q_door)),
             (item, dict(ev, event_name="Last-Minute Savings", start="2026-12-18", end="2026-12-24", quote=q_last))]
    dropped = {}
    cands = finalize_candidates(judge(pairs, cfg.brands, cfg.trusted_domains, dropped), cfg.brands, dropped)
    events = merge_candidates([], cands, T1)
    by = {e["id"]: e for e in events}
    assert set(by) == {"bestbuy-US-other-2026-doorbooster", "bestbuy-US-other-2026-lastminutesaving"}
    assert by["bestbuy-US-other-2026-doorbooster"]["start"] is None          # 引句里没有 12-18，置空
    assert (by["bestbuy-US-other-2026-lastminutesaving"]["start"],
            by["bestbuy-US-other-2026-lastminutesaving"]["end"]) == ("2026-12-18", "2026-12-24")
    assert all_source_dates_in_quotes(events)
    assert [c["event_id"] for c in cands] == ["bestbuy-US-other-2026-doorbooster",
                                              "bestbuy-US-other-2026-lastminutesaving"]


def test_bestbuy_doorboosters_and_last_minute_split(cfg, make_candidate):
    door = bb(make_candidate, url="https://ign/1", event_name="Door BOOsters", start="2026-10-30", end="2026-11-01",
              quote='its "Door BOOsters" sale starting on October 30 and running through to November 1')
    door2 = bb(make_candidate, url="https://ign/2", event_name="Best Buy DoorBOOsters Sale", start="2026-10-30",
               quote="DoorBOOsters kicks off Oct. 30")
    last = bb(make_candidate, url="https://ign/3", event_name="Last-Minute Savings", start="2026-12-18", end="2026-12-24",
              quote="starting December 18 and running through December 24")
    cands = finalize_candidates([door, door2, last], cfg.brands, {})
    by = {e["id"]: e for e in merge_candidates([], cands, T1)}
    d = by["bestbuy-US-other-2026-doorbooster"]
    assert (d["start"], d["end"], len(d["sources"])) == ("2026-10-30", "2026-11-01", 2)
    l = by["bestbuy-US-other-2026-lastminutesaving"]
    assert (l["start"], l["end"], len(l["sources"])) == ("2026-12-18", "2026-12-24", 1)
    assert "date_conflict" not in d and "date_conflict" not in l


def test_date_vote_majority_then_conflict_on_tie(make_candidate):
    a = bb(make_candidate, event_type="early_bf", url="https://ign/a", start="2026-11-06",
           published="2026-10-27T08:23:00+00:00")
    b = bb(make_candidate, event_type="early_bf", url="https://ign/b", start="2026-11-06",
           published="2026-10-27T10:42:00+00:00")
    c = bb(make_candidate, event_type="early_bf", url="https://tn/c", start="2026-10-30",
           published="2026-10-30T09:57:00+00:00")                     # 更新，但只有一票
    e = merge_candidates([], [a, b, c], T1)[0]
    assert e["start"] == "2026-11-06" and "date_conflict" not in e
    e2 = merge_candidates([], [a, c], T1)[0]                           # 1:1、同级 → 写出分歧
    assert e2["date_conflict"] == ["2026-10-30", "2026-11-06"]
    assert e2["start"] == "2026-10-30"                                 # 平票时取发布较新的
    # 级别更高的一方直接胜出，不算分歧
    m = bb(make_candidate, event_type="early_bf", url="https://usa/m", level="media", start="2026-11-06")
    e3 = merge_candidates([], [m, c], T1)[0]
    assert e3["start"] == "2026-11-06" and "date_conflict" not in e3


def test_named_other_not_folded_into_early_bf(cfg, make_candidate):
    ebf = bb(make_candidate, event_type="early_bf", url="https://a/1", start="2026-10-30",
             quote="early Black Friday deals begin 2026-10-30")
    door = bb(make_candidate, url="https://b/1", event_name="DoorBOOsters", start="2026-10-30",
              quote="DoorBOOsters starts Oct. 30")
    unnamed = bb(make_candidate, url="https://c/1", start="2026-10-30", quote="deals start Oct. 30")
    cands = finalize_candidates([ebf, door, unnamed], cfg.brands, {})
    ids = sorted(e["id"] for e in merge_candidates([], cands, T1))
    assert ids == ["bestbuy-US-early_bf-2026", "bestbuy-US-other-2026-doorbooster"]   # 没名字的才会并进去
    assert [c["event_id"] for c in cands][2] == "bestbuy-US-early_bf-2026"


def test_costco_member_deals_holiday_savings_and_black_friday(cfg, make_candidate):
    def co(**kw):
        return make_candidate(brand_id="costco", region="US", **kw)
    members = co(event_type="other", url="https://yahoo/1", event_name="members-only deals", start="2026-10-19",
                 end="2026-11-15", quote="The latest members-only deals are valid from October 19, 2026, through "
                                         "November 15, 2026")
    holiday = co(event_type="other", url="https://marca/1", event_name="Holiday Savings", start="2026-10-30",
                 end="2026-11-30", quote="Costco's separate Holiday Savings event begins October 30 and continues "
                                         "through November 30")
    holiday_as_bf = co(event_type="black_friday", url="https://yahoo/2", event_name="Holiday Savings",
                       start="2026-10-30", end="2026-11-30",
                       quote="the Holiday Savings deals are valid between October 30 and November 30")
    bf = co(event_type="black_friday", url="https://tu/1", start="2026-11-16", end="2026-11-30",
            quote="followed by the official Black Friday sale from Nov. 16-30")
    dropped = {}
    cands = finalize_candidates([members, holiday, holiday_as_bf, bf], cfg.brands, dropped)
    assert cands[2]["event_type"] == "other" and cands[2]["ai_event_type"] == "black_friday"
    assert sum(v for k, v in dropped.items() if k.startswith("类型改成 other")) == 1
    by = {e["id"]: e for e in merge_candidates([], cands, T1)}
    assert set(by) == {"costco-US-black_friday-2026", "costco-US-other-2026-holidaysaving",
                       "costco-US-other-2026-memberonly"}
    assert (by["costco-US-black_friday-2026"]["start"], by["costco-US-black_friday-2026"]["end"]) == \
        ("2026-11-16", "2026-11-30")
    assert len(by["costco-US-black_friday-2026"]["sources"]) == 1
    assert (by["costco-US-other-2026-holidaysaving"]["start"], by["costco-US-other-2026-holidaysaving"]["end"]) == \
        ("2026-10-30", "2026-11-30")
    assert len(by["costco-US-other-2026-holidaysaving"]["sources"]) == 2
    assert (by["costco-US-other-2026-memberonly"]["start"], by["costco-US-other-2026-memberonly"]["end"]) == \
        ("2026-10-19", "2026-11-15")


def test_other_without_name_or_start_dropped(cfg, make_candidate):
    dropped, rejected = {}, []
    c = make_candidate(event_type="other", url="https://x/1")
    assert finalize_candidates([c], cfg.brands, dropped, rejected) == []
    assert dropped == {"other 没有名字也没有开始日期": 1} and rejected[0]["dropped_reason"]
    # 不经过 finalize 直接进合并，也会被跳过
    assert merge_candidates([], [make_candidate(event_type="other", url="https://x/2")], T1) == []


def test_same_url_different_quotes_never_spliced(make_candidate):
    url = "https://www.macrumors.com/x"
    a = make_candidate(url=url, quote="Black Friday sale announced", status="rumored")              # 没日期
    b = make_candidate(url=url, start="2026-11-19", end="2026-11-28",
                       quote="Black Friday sale will start on Thursday, November 19 and run through Saturday, November 28")
    e = merge_candidates([], [a, b], T1)[0]
    s = e["sources"][0]
    assert len(e["sources"]) == 1
    assert (s["start"], s["end"], s["quote"]) == ("2026-11-19", "2026-11-28", b["quote"])   # 整组换成 b
    c = make_candidate(url=url, start="2026-12-18", quote="Black Friday sale announced")      # 引句和 a 一样但不同于现在
    e2 = merge_candidates([e], [c], T2)[0]
    assert e2["sources"][0]["quote"] == b["quote"] and e2["start"] == "2026-11-19"           # 排名不高于 b：不动
    assert all_source_dates_in_quotes([e, e2])


def test_source_dates_not_in_own_quote_are_cleared(make_candidate, caplog):
    bad = make_candidate(url="https://m/1", start="2026-12-18", end="2026-12-24",
                         quote='Best Buy will kick off its holiday sales with "DoorBOOsters" on the day before Halloween')
    e = merge_candidates([], [bad], T1)[0]
    assert e["start"] is None and e["sources"][0]["start"] is None and e["sources"][0]["end"] is None
    assert "来源日期不在它自己的引句里" in caplog.text


def test_candidates_record_final_event_and_kept(make_candidate):
    cands = [make_candidate(level="other", url=f"https://r/{i}", published=f"2026-10-1{i}T00:00:00+00:00")
             for i in range(9)]
    merge_candidates([], cands, T1)
    assert all(c["event_id"] == "nike-US-black_friday-2026" for c in cands)
    assert [c["kept"] for c in cands] == [False] + [True] * 8      # 最旧的那条最后被挤掉


# ---------- S3 v5 ----------
def test_override_dates_clear_date_conflict(make_candidate):
    a = make_candidate(url="https://a/1", start="2026-11-06", published="2026-10-27T00:00:00+00:00")
    b = make_candidate(url="https://b/1", start="2026-10-30", published="2026-10-30T00:00:00+00:00")
    ev = merge_candidates([], [a, b], T1)
    assert ev[0]["date_conflict"]
    out = apply_overrides(ev, {ev[0]["id"]: {"start": "2026-11-06"}})
    assert "date_conflict" not in out[0] and out[0]["start"] == "2026-11-06"
    assert apply_overrides(ev, {ev[0]["id"]: {"discount": "x"}})[0]["date_conflict"]   # 没改日期：保留


def test_unadopted_same_url_evidence_cannot_change_text_fields(make_candidate):
    url = "https://m/1"
    a = make_candidate(url=url, start="2026-11-19", discount="40% off", event_name="Black Friday Sale",
                       quote="Black Friday Sale starts 2026-11-19 with 40% off")
    b = make_candidate(url=url, discount="70% off", event_name="Other Name",
                       quote="something else entirely 70% off", status="rumored")       # 没日期、排名更低
    e = merge_candidates([], [a, b], T1)[0]
    assert (e["discount"], e["event_name"]) == ("40% off", "Black Friday Sale")
    assert e["sources"][0]["quote"] == a["quote"]


def test_exact_dates_fold_other_into_non_other_and_unnamed_into_named(cfg, make_candidate):
    bf = make_candidate(url="https://a/1", start="2026-11-20", end="2026-11-30",
                        quote="Black Friday sale from 2026-11-20 to 2026-11-30")      # 引句带类型关键词，类型校正不改它
    named_same = make_candidate(event_type="other", event_name="Big Event", url="https://b/1",
                                start="2026-11-20", end="2026-11-30")              # 起止相同 → 并进 black_friday
    named_x = make_candidate(event_type="other", event_name="Member Days", url="https://c/1",
                             start="2026-12-01", end="2026-12-05")
    unnamed_x = make_candidate(event_type="other", url="https://d/1", start="2026-12-01", end="2026-12-05")
    named_open = make_candidate(event_type="other", event_name="Open Ended", url="https://e/1",
                                start="2026-11-20")                                 # 没有结束日期：有名的不并
    cands = finalize_candidates([bf, named_same, named_x, unnamed_x, named_open], cfg.brands, {})
    ev = merge_candidates([], cands, T1)
    by = {e["id"]: e for e in ev}
    assert set(by) == {"nike-US-black_friday-2026", "nike-US-other-2026-memberday", "nike-US-other-2026-openended"}
    assert {s["url"] for s in by["nike-US-black_friday-2026"]["sources"]} == {"https://a/1", "https://b/1"}
    assert {s["url"] for s in by["nike-US-other-2026-memberday"]["sources"]} == {"https://c/1", "https://d/1"}
    assert [c["event_id"] for c in cands] == ["nike-US-black_friday-2026", "nike-US-black_friday-2026",
                                              "nike-US-other-2026-memberday", "nike-US-other-2026-memberday",
                                              "nike-US-other-2026-openended"]


def test_manual_source_wins_same_level_vote_and_is_never_evicted(make_candidate):
    ai = [make_candidate(level="official", url=f"https://o/{i}", start="2026-11-20", discount="10% off",
                         published="2026-10-30T00:00:00+00:00") for i in range(9)]
    man = make_candidate(level="official", url="https://www.nike.com/ca", start="2026-11-18", discount="20% off",
                         event_name="Member Access", published="2026-10-01T00:00:00+00:00")
    man["source"]["manual"] = True
    e = merge_candidates([], [man] + ai, T1)[0]
    assert "https://www.nike.com/ca" in {s["url"] for s in e["sources"]} and len(e["sources"]) == 8
    assert e["start"] == "2026-11-18" and "date_conflict" not in e      # manual 胜出，不算分歧
    assert e["discount"] == "20% off" and e["event_name"] == "Member Access"


def test_real_overrides_file_well_formed(cfg):
    from datetime import date as _d
    from dealradar.merge import OVERRIDE_FIELDS
    for eid, ov in cfg.overrides.items():
        assert eid.split("-")[0] in cfg.brands, eid
        for k, v in ov.items():
            assert k in OVERRIDE_FIELDS or k == "delete", (eid, k)
            if k in ("start", "end") and v is not None:
                _d.fromisoformat(str(v))
    assert cfg.overrides["bestbuy-US-early_bf-2026"]["end"] == "2026-11-19"
    assert cfg.overrides["bestbuy-US-other-2026-doorbooster"]["end"] == "2026-11-01"
