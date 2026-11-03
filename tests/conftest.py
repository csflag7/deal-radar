from __future__ import annotations

import sys
from urllib.parse import urlparse
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[1]
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

from dealradar.config import Paths, load_config  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture(scope="session")
def cfg():
    return load_config(Paths())


@pytest.fixture
def make_item():
    def _make(**kw):
        item = {
            "source": "gnews", "feed": "gnews-US-nike", "region_hint": "US", "brand_hints": ["nike"],
            "title": "Nike Black Friday sale starts Nov. 20", "summary": "", "link": "https://news.google.com/rss/articles/X",
            "published": "2026-10-28T14:00:00+00:00", "guid": "X", "source_name": "CNBC",
            "source_url": "https://www.cnbc.com",
        }
        item.update(kw)
        return item
    return _make


@pytest.fixture
def make_candidate():
    """status.judge 输出的候选事件。status 不给时按来源级别定（合并测试只关心合并规则）。
    quote 不给时按日期和折扣自动生成（日期写成 ISO，能通过“日期必须在引句里”的核对）。"""
    def _make(level="other", url="https://example.com/a", title=None, published="2026-10-28T00:00:00+00:00", **kw):
        c = {
            "brand_id": "nike", "region": "US", "event_type": "black_friday", "year": 2026,
            "event_name": None, "start": None, "end": None, "discount": None,
            "status": "confirmed" if level in ("official", "media") else "rumored",
        }
        c.update(kw)
        if "quote" not in c:
            c["quote"] = " ".join(["sale"] + [f"from {c['start']}"] * bool(c["start"]) +
                                  [f"to {c['end']}"] * bool(c["end"]) + [c["discount"]] * bool(c["discount"]))
        c["source"] = {"title": title or f"标题 {url}", "url": url, "domain": urlparse(url).hostname,
                       "name": "x", "level": level, "status": c["status"],
                       "start": c["start"], "end": c["end"], "discount": c["discount"],
                       "quote": c["quote"], "published": published}
        return c
    return _make


@pytest.fixture(autouse=True)
def no_real_push(monkeypatch):
    """测试里跑 dealradar.run 时不真的推送：把 run 里的 push_run 换成记录调用的假函数。"""
    calls = []
    monkeypatch.setattr("dealradar.run.push_run", lambda *a, **k: calls.append((a, k)) or {"digest": False,
                                                                                            "alert": False})
    return calls


@pytest.fixture(autouse=True)
def no_real_official(monkeypatch):
    """测试里不能真的去官网：不传假 seed 就找页面、不传假 crawler 就启动浏览器，一律报错。"""
    import dealradar.official as off

    def no_browser():
        raise AssertionError("测试里不能启动真的浏览器")
    orig = off.discover_async

    async def guarded(brands, settings, seed=None, transport=None):
        if seed is None:
            raise AssertionError("测试里不能联网找官网页面")
        return await orig(brands, settings, seed, transport)
    monkeypatch.setattr(off, "default_crawler_factory", no_browser)
    monkeypatch.setattr(off, "discover_async", guarded)


def pytest_configure(config):
    # 只在 Mac 上用的模块（Mac 本地任务、Mac 日历）的测试打这个标记；云端（GitHub Actions）用 -m "not mac" 跳过
    config.addinivalue_line("markers", "mac: 只在 Mac 上用的测试（mac_run、calendar_sync、launchd），云端跳过")
