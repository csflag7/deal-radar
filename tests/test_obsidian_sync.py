"""Obsidian 同步：events.json 字段、品牌笔记的程序区 / 用户区规则。"""
from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest

from dealradar import obsidian_sync as osync
from dealradar.config import APP_ROOT, Paths

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)


def make_event(eid="nike-US-black_friday-2026", **kw):
    brand, region, etype = eid.split("-")[:3]
    e = {
        "id": eid, "brand_id": brand, "region": region, "event_type": etype, "year": 2026,
        "event_name": None, "start": "2026-11-20", "end": "2026-11-30", "discount": "up to 50% off",
        "status": "rumored",
        "sources": [{"title": "Nike Black Friday sale", "url": "https://example.com/a", "domain": "example.com",
                     "name": "Example News", "level": "media", "status": "rumored",
                     "quote": "Nike Black Friday sale runs 2026-11-20 to 2026-11-30", "published": "2026-10-28"}],
    }
    e.update(kw)
    return e


@pytest.fixture
def env(tmp_path):
    """临时库 + 临时配置目录（复制真实配置，overrides.yaml 换成测试自己的）。
    返回的第一个值是工作台所在的子文件夹 <库>/黑五购物（默认位置，ADR-007）。"""
    vault = tmp_path / "vault"
    vault.mkdir()
    conf = tmp_path / "config"
    shutil.copytree(APP_ROOT / "config", conf)
    (conf / "overrides.yaml").write_text("events: {}\n", encoding="utf-8")
    sales = tmp_path / "sales.json"

    def run(events, updated="2026-11-02T00:40:25+00:00"):
        sales.write_text(json.dumps({"updated": updated, "events": events}), encoding="utf-8")
        return osync.sync(vault, sales, Paths(config_dir=conf), now=NOW)
    return vault / osync.DEFAULT_SUBDIR, conf, run


def note(vault: Path, name: str) -> Path:
    return vault / "品牌" / f"{name}.md"


def split_note(text: str) -> tuple:
    """(程序区之前, 程序区, 程序区之后)。"""
    i, j = text.index(osync.START), text.index(osync.END) + len(osync.END)
    return text[:i], text[i:j], text[j:]


# ---------- events.json ----------
def test_events_json_fields_and_overrides(env):
    vault, conf, run = env
    (conf / "overrides.yaml").write_text(
        'events:\n  nike-US-black_friday-2026:\n    end: "2026-11-28"\n    status: confirmed\n', encoding="utf-8")
    run([make_event(), make_event("sephora-CA-other-2026-vibsale", event_name="VIB Sale", start=None, end=None),
         make_event("unknownbrand-US-black_friday-2026")])
    data = json.loads((vault / "_黑五" / "data" / "events.json").read_text(encoding="utf-8"))
    assert data["data_updated"] == "2026-11-02T00:40:25+00:00"
    assert data["generated_at"].startswith("2026-11-01T12:00")
    assert len(data["brands"]) == 47 and {c["id"] for c in data["categories"]} >= {"fashion", "tech"}
    ids = [e["id"] for e in data["events"]]
    assert ids == ["nike-US-black_friday-2026", "sephora-CA-other-2026-vibsale"]   # 品牌清单外的丢掉；有日期的在前
    nike = data["events"][0]
    assert nike["end"] == "2026-11-28" and nike["status"] == "confirmed"          # 先套人工纠错
    assert nike["brand"] == "Nike" and nike["type_zh"] == "黑五" and nike["category"] == "fashion"
    assert nike["shop_url"] == "https://www.nike.com"
    assert next(b for b in data["brands"] if b["id"] == "nike")["note"] == "黑五购物/品牌/Nike"   # 库里的完整路径
    assert nike["sources"] == [{"title": "Nike Black Friday sale", "url": "https://example.com/a",
                                "site": "Example News", "quote": "Nike Black Friday sale runs 2026-11-20 to 2026-11-30"}]
    assert "field_levels" not in nike and "year" not in nike                      # 只留页面要用的字段
    seph = data["events"][1]
    assert seph["name"] == "VIB Sale" and seph["type_zh"] == "促销活动"
    assert seph["shop_url"] == "https://www.sephora.com/ca/en"


def test_unknown_region_uses_us_shop(env):
    vault, _, run = env
    run([make_event("nike-unknown-black_friday-2026")])
    data = json.loads((vault / "_黑五" / "data" / "events.json").read_text(encoding="utf-8"))
    assert data["events"][0]["shop_url"] == "https://www.nike.com"


# ---------- 品牌笔记：新建 ----------
def test_creates_all_brand_notes(env):
    vault, _, run = env
    st = run([make_event()])
    assert len(st["created"]) == 47 and st["events"] == 1
    text = note(vault, "Nike").read_text(encoding="utf-8")
    assert text.startswith("---\ncategory: \"服装鞋包\"\nregions: [US, CA]\n")
    assert 'shop_us: "https://www.nike.com"' in text and "tags: [品牌]" in text
    before, block, after = split_note(text)
    assert "11月20日 – 11月30日" in block and "up to 50\\% off" in block and "[Example News](https://example.com/a)" in block
    assert "%% brand_id: nike %%" in block
    assert "[[黑五购物/黑五首页|← 黑五首页]] · [[黑五购物/品牌|品牌列表]] · [[黑五购物/购物车|购物车]]" in block
    assert after.lstrip().startswith(osync.USER_HEADING)
    # 只有一个国家有店：regions 只写那一个，另一个链接留空
    sdm = note(vault, "Shoppers Drug Mart").read_text(encoding="utf-8")
    assert "regions: [CA]" in sdm and "shop_us:\n" in sdm
    assert "本季暂时还没有收集到" in sdm
    # 文件名里不能用的字符去掉；& ' 这些保留
    assert note(vault, "Abercrombie & Fitch").exists() and note(vault, "Arc'teryx").exists()


# ---------- 品牌笔记：重复运行 ----------
def test_rerun_keeps_user_area_byte_for_byte(env):
    vault, _, run = env
    run([make_event()])
    p = note(vault, "Nike")
    text = p.read_text(encoding="utf-8")
    before, _, after = split_note(text)
    # 用户改了 frontmatter、在程序区上面加了一行、在我的备注里写了东西（含像表格的竖线、像标记的注释）
    before = before.replace("tags: [品牌]", "tags: [品牌, 想买]\nsize: M")
    before += "我在程序区上面写的一行\n\n"
    after += "\n- 想买 Pegasus 41 | 42 码\n<!-- 我自己的注释 -->\n"
    p.write_text(before + split_note(text)[1] + after, encoding="utf-8")

    st = run([make_event(discount="up to 60% off", status="confirmed"),
              make_event("nike-CA-cyber_monday-2026", start="2026-11-30", end=None, discount="")])
    assert "Nike.md" in st["updated"]
    new = p.read_text(encoding="utf-8")
    b2, block2, a2 = split_note(new)
    assert b2 == before and a2 == after                       # 用户区一个字都没变
    assert "up to 60\\% off" in block2 and "已官宣" in block2 and "11月30日 起" in block2 and "🇨🇦" in block2
    assert "up to 50\\% off" not in block2

    # 数据没变再跑一次：不改写任何文件
    mtimes = {f.name: f.stat().st_mtime_ns for f in (vault / "品牌").iterdir()}
    st = run([make_event(discount="up to 60% off", status="confirmed"),
              make_event("nike-CA-cyber_monday-2026", start="2026-11-30", end=None, discount="")])
    assert len(st["unchanged"]) == 47 and not st["updated"] and not st["created"]
    assert p.read_text(encoding="utf-8") == new
    assert {f.name: f.stat().st_mtime_ns for f in (vault / "品牌").iterdir()} == mtimes


def test_never_deletes_notes(env):
    vault, _, run = env
    run([make_event()])
    extra = vault / "品牌" / "我自己加的品牌.md"
    extra.write_text("随便写写\n", encoding="utf-8")
    run([])
    assert extra.read_text(encoding="utf-8") == "随便写写\n"
    assert "本季暂时还没有收集到" in note(vault, "Nike").read_text(encoding="utf-8")


# ---------- 品牌笔记：标记缺失 ----------
def test_both_markers_missing_inserts_block_after_frontmatter(env):
    vault, _, run = env
    run([])
    p = note(vault, "Nike")
    user = "---\ncategory: 服装鞋包\nmine: 1\n---\n## 我的备注\n\n去年黑五买了 Pegasus。\n"
    p.write_text(user, encoding="utf-8")
    st = run([make_event()])
    assert "Nike.md" in st["inserted"]
    text = p.read_text(encoding="utf-8")
    assert text.startswith("---\ncategory: 服装鞋包\nmine: 1\n---\n\n" + osync.START)
    assert text.endswith("## 我的备注\n\n去年黑五买了 Pegasus。\n")        # 原来的正文原样留在下面
    assert "11月20日 – 11月30日" in text
    # 插过之后就是正常的程序区：再跑一次，用户区不变、文件不变
    st = run([make_event()])
    assert "Nike.md" in st["unchanged"] and p.read_text(encoding="utf-8") == text


def test_markers_missing_without_frontmatter(env):
    vault, _, run = env
    run([])
    p = note(vault, "Nike")
    p.write_text("只有我的字\n", encoding="utf-8")
    run([make_event()])
    text = p.read_text(encoding="utf-8")
    assert text.startswith(osync.START) and text.endswith("\n\n只有我的字\n")


@pytest.mark.parametrize("body", [
    "## 我的备注\n" + osync.START + "\n只有开头的标记\n",
    "## 我的备注\n只有结尾的标记\n" + osync.END + "\n",
    osync.END + "\n顺序颠倒\n" + osync.START + "\n",
    osync.START + "\na\n" + osync.END + "\n" + osync.START + "\nb\n" + osync.END + "\n",
])
def test_broken_markers_skip_file(env, body):
    vault, _, run = env
    run([])
    p = note(vault, "Nike")
    p.write_text(body, encoding="utf-8")
    st = run([make_event()])
    assert "Nike.md" in st["skipped"]
    assert p.read_text(encoding="utf-8") == body               # 整篇不动


# ---------- 小工具 ----------
def test_update_note_text_empty_frontmatter():
    new, how = osync.update_note_text("---\n---\n正文\n", osync.START + "\nx\n" + osync.END)
    assert how == "inserted" and new == "---\n---\n\n" + osync.START + "\nx\n" + osync.END + "\n\n正文\n"


def test_table_cells_are_escaped():
    b = {"name": "X", "shop": {"US": "https://x.com"}}
    r = {"id": "x-US-other-2026-a", "brand_id": "x", "brand": "X", "region": "US", "event_type": "other",
         "type_zh": "促销活动", "name": "A | B <!-- dealradar:end -->", "start": None, "end": None,
         "discount": "50% | 60%", "status": "rumored",
         "sources": [{"title": "t", "url": "javascript:alert(1)", "site": "[evil]", "quote": ""}]}
    block = osync.render_block(b, "家居综合", [r], None)
    assert block.count(osync.END) == 1 and block.endswith(osync.END)
    assert "50\\% \\| 60\\%" in block and "A \\| B &lt;\\!-- dealradar:end --&gt;" in block
    assert "javascript:" not in block and "\\(evil\\)" in block


def test_note_name():
    assert osync.note_name("Arc'teryx") == "Arc'teryx"
    assert osync.note_name('A/B: "C"?') == "A B C"
    assert osync.note_name("") == "未命名品牌"


def test_cli_reports_missing_vault(tmp_path, capsys):
    assert osync.main(["--vault", str(tmp_path / "nope"), "--sales", str(tmp_path / "s.json")]) == 2
    assert "找不到库的文件夹" in capsys.readouterr().out


def test_markdown_specials_are_escaped():
    """数据里的字不能变成图片、双链、链接（也不能变成公式、注释、高亮）。"""
    b = {"name": "X", "shop": {"US": "https://x.com"}}
    nasty = "![img](https://x.com/a.png) [[秘密笔记]] $x$ %%c%% ==h== ~~s~~ #tag ^ref"
    r = {"id": "x-US-other-2026-a", "brand_id": "x", "brand": "X", "region": "US", "event_type": "other",
         "type_zh": "促销活动", "name": nasty, "start": "2026-11-20", "end": None, "discount": nasty,
         "status": "rumored",
         "sources": [{"title": "t", "url": "https://example.com/a", "site": "![s](t) [[u]]", "quote": ""}]}
    block = osync.render_block(b, "家居综合", [r], None, "x")
    row = next(l for l in block.splitlines() if l.startswith("| 11月20日"))
    cells = [c.strip() for c in row.strip("|").split(" | ")]
    assert len(cells) == 6
    for c in cells[:5]:
        for seq in ("![", "[[", "](", "$x$", "%%", "==", "~~"):
            assert seq not in c, (seq, c)
    src = cells[5]
    assert src.count("](") == 1 and src.endswith("](https://example.com/a)")   # 只有程序自己生成的那一个链接
    assert "![" not in src and "[[" not in src
    assert "\\$x\\$" in cells[2] and "\\#tag" in cells[2]


def test_finds_note_by_brand_id_after_rename(env):
    vault, _, run = env
    run([make_event()])
    old = note(vault, "Nike")
    renamed = note(vault, "耐克 跑鞋")
    old.rename(renamed)
    st = run([make_event(discount="up to 70% off")])
    assert not old.exists() and "耐克 跑鞋.md" in st["updated"]       # 按 brand_id 找到改了名的笔记，不另建
    assert "up to 70\\% off" in renamed.read_text(encoding="utf-8")
    data = json.loads((vault / "_黑五" / "data" / "events.json").read_text(encoding="utf-8"))
    assert next(b for b in data["brands"] if b["id"] == "nike")["note"] == "黑五购物/品牌/耐克 跑鞋"


def test_note_name_collision_warns(env, caplog):
    vault, conf, run = env
    with (conf / "brands.yaml").open("a", encoding="utf-8") as f:
        f.write('  nike2:        { name: "Nike?", category: fashion, shop: { US: "https://nike2.example" }, '
                'domains: [nike2.example] }\n')
    with caplog.at_level("WARNING"):
        st = run([make_event(), make_event("nike2-US-black_friday-2026", discount="nike2 only")])
    assert any("撞名" in r.getMessage() for r in caplog.records)
    assert "Nike.md" in st["created"] and "Nike (nike2).md" in st["created"]
    assert "%% brand_id: nike %%" in note(vault, "Nike").read_text(encoding="utf-8")
    two = note(vault, "Nike (nike2)").read_text(encoding="utf-8")
    assert "%% brand_id: nike2 %%" in two and "nike2 only" in two
    # 再跑一次还是各找各的
    st = run([make_event(), make_event("nike2-US-black_friday-2026", discount="nike2 only")])
    assert not st["created"] and len(st["unchanged"]) == 48


def test_vault_root_mode(tmp_path):
    """subdir 为空：工作台就在库的根目录，导航双链不带前缀。"""
    sales = tmp_path / "sales.json"
    sales.write_text(json.dumps({"events": [make_event()]}), encoding="utf-8")
    vault = tmp_path / "v"
    vault.mkdir()
    osync.sync(vault, sales, now=NOW, subdir="")
    text = (vault / "品牌" / "Nike.md").read_text(encoding="utf-8")
    assert "[[黑五首页|← 黑五首页]]" in text and (vault / "_黑五" / "data" / "events.json").exists()


def test_sync_never_touches_cart_json(env):
    """购物车数据（cart.json，用户在页面上填的）同步程序一个字节都不碰；以后的比价程序读它。"""
    vault, _, run = env
    data = vault / "_黑五" / "data"
    data.mkdir(parents=True)
    cart = data / "cart.json"
    original = '{"items": [{"id": "c1", "name": "耳机", "brand_id": "bose", "region": "US"}]}\n'
    cart.write_text(original, encoding="utf-8")
    before = cart.stat().st_mtime_ns
    run([make_event()])
    run([make_event(discount="up to 60% off")])
    assert cart.read_text(encoding="utf-8") == original and cart.stat().st_mtime_ns == before
    assert sorted(p.name for p in data.iterdir()) == ["cart.json", "events.json"]
