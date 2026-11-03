from __future__ import annotations

from dealradar import build_web
from dealradar.merge import merge_candidates

SALE_KEYS = {"id", "brand", "region", "event_type", "event", "event_name", "start", "end", "discount", "status",
             "sources", "updated", "date_conflict"}


def test_data_js_parses_and_has_fields(cfg, make_candidate, tmp_path):
    events = merge_candidates([], [
        make_candidate(level="media", url="https://m", title="Nike BF \"quoted\" </script>", start="2026-11-20",
                       discount="50% off"),
        make_candidate(region="CA", event_type="boxing_day", url="https://r"),
        make_candidate(brand_id="lululemon", event_name="We Made Too Much", url="https://l"),
        make_candidate(brand_id="zara", url="https://z"),  # 不在品牌清单里：不输出
    ], "2026-11-01T12:00:00+00:00")
    data = build_web.build_data(events, cfg.categories, cfg.brands, "2026-11-01T12:00:00+00:00")
    out = tmp_path / "data.js"
    build_web.write(out, data)
    text = out.read_text(encoding="utf-8")
    assert text.startswith("//") and "window.DEAL_DATA = " in text and text.rstrip().endswith(";")
    d = build_web.parse(text)
    assert d == data

    assert d["meta"] == {"generated_at": "2026-11-01T12:00:00+00:00", "sample": False}
    assert [c["id"] for c in d["categories"]] == ["fashion", "beauty", "tech", "home"]
    assert len(d["brands"]) == len(cfg.brands)
    for b in d["brands"]:
        assert set(b) == {"id", "name", "category", "shop"} and b["shop"]
    assert len(d["sales"]) == 3
    by = {s["id"]: s for s in d["sales"]}
    for s in d["sales"]:
        assert set(s) == SALE_KEYS
        assert s["status"] in ("confirmed", "rumored")
        assert s["brand"] in cfg.brands
        assert all(set(x) == {"title", "url", "level", "status"} for x in s["sources"])
        assert s["updated"] == "2026-11-01"
    us = by["nike-US-black_friday-2026"]
    assert us["event"] == "黑五" and us["status"] == "confirmed" and us["start"] == "2026-11-20"
    assert us["sources"] == [{"title": "Nike BF \"quoted\" </script>", "url": "https://m",
                              "level": "media", "status": "confirmed"}]
    assert by["nike-CA-boxing_day-2026"]["event"] == "节礼日"
    assert by["nike-CA-boxing_day-2026"]["status"] == "rumored" and by["nike-CA-boxing_day-2026"]["start"] is None
    # 非 other：标题用类型中文名，AI 给的活动名只放在 event_name（详情里显示）
    assert by["lululemon-US-black_friday-2026"]["event"] == "黑五"
    assert by["lululemon-US-black_friday-2026"]["event_name"] == "We Made Too Much"


def test_display_names(cfg):
    from dealradar.build_web import display_name
    assert [display_name({"event_type": t, "event_name": "Best Buy Week 1 Black Friday Doorbusters"})
            for t in ("early_bf", "black_friday", "cyber_monday", "cyber_week", "boxing_day")] == \
        ["提前黑五", "黑五", "网一", "网一周", "节礼日"]
    assert display_name({"event_type": "other", "event_name": "DoorBOOsters"}) == "DoorBOOsters"
    assert display_name({"event_type": "other", "event_name": None}) == "促销活动"
