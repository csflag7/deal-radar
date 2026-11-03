"""把活动数据同步进 Obsidian「黑五购物工作台」库（只写库里的文件，不碰 Mac 日历）。

用法（在 app 目录下）：
  .venv312/bin/python -m dealradar.obsidian_sync                       读 data/local/final.json；默认库：邮件工作台，子文件夹 黑五购物/
  .venv312/bin/python -m dealradar.obsidian_sync --vault <库路径> --subdir <子文件夹> --final <final.json>
  .venv312/bin/python -m dealradar.obsidian_sync --sales <sales.json>  （旧用法）读没套过人工纠错的 sales.json
Mac 上一般由 dealradar.mac_run 调用（combine 之后）。

工作台放在库的子文件夹里（ADR-007，默认 邮件工作台/黑五购物/，下面记作 <黑五>）。做两件事：
1. 写 <黑五>/_黑五/data/events.json：final.json 已经套过 config/overrides.yaml（AI 盖不过人工纠错），
   旧用法的 sales.json 这里先套一次；只留页面要用的字段（品牌、地区、类型中文名、名称、起止日期、折扣、状态、来源、官网链接）。
   另带 meta：Mac 本地运行状态（data/local/status.json，例如上次成功的时间 last_success），首页显示。
2. 给每个品牌写 <黑五>/品牌/<品牌名>.md：
   - 先按程序区里的 %% brand_id: xx %% 找笔记（用户改了文件名也找得到），找不到再按文件名找。
     两个品牌的文件名撞了：打警告，后一个用「品牌名 (id).md」。
   - 新建时：frontmatter（category、regions、shop_us、shop_ca、tags）+ 程序区 + 「## 我的备注」。
     frontmatter 只在新建时写一次。
   - 已存在时：只替换 <!-- dealradar:start --> 到 <!-- dealradar:end --> 之间（含两个标记）的内容，
     标记之外（frontmatter、我的备注……）一个字都不动。内容没变就不写文件。
   - 两个标记都找不到：把程序区插在 frontmatter 后面，原来的内容原样留在下面。
   - 只有一个标记、标记重复、或顺序颠倒：不知道用户区从哪开始，整篇跳过不写，打出警告。
   - 不删除任何笔记（品牌从 brands.yaml 里拿掉了，笔记也留着）。
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

from .build_web import EVENT_NAMES_ZH, display_name
from .config import APP_ROOT, Paths, load_config, load_json
from .merge import apply_overrides

log = logging.getLogger(__name__)

START = "<!-- dealradar:start -->"
END = "<!-- dealradar:end -->"
USER_HEADING = "## 我的备注"
DEFAULT_VAULT = APP_ROOT.parents[1] / "邮件工作台"     # ~/claude_workplatform/邮件工作台
DEFAULT_SUBDIR = "黑五购物"
DATA_REL = Path("_黑五") / "data" / "events.json"     # 相对 <黑五>
NOTES_DIR = "品牌"
HOME_NOTE = "黑五首页"
ID_LINE = "%% brand_id: {} %%"
_ID_RE = re.compile(r"%% brand_id: ([A-Za-z0-9_-]+) %%")
REGION_FLAG = {"US": "🇺🇸", "CA": "🇨🇦", "unknown": "❔"}
REGION_ZH = {"US": "美国", "CA": "加拿大", "unknown": "地区未知"}
STATUS_ZH = {"confirmed": "已官宣", "rumored": "传闻"}
MAX_NOTE_SOURCES = 3            # 品牌笔记活动表里每个活动最多列几个来源链接
_BAD_NAME = re.compile(r'[\\/:*?"<>|#^\[\]]')


# ---------- 文件名、文本转义 ----------
def note_name(brand_name: str) -> str:
    """品牌显示名 → 笔记文件名（不含 .md）。去掉 Obsidian 文件名和链接里不能用的字符。"""
    s = _BAD_NAME.sub(" ", brand_name or "").strip()
    return re.sub(r"\s+", " ", s) or "未命名品牌"


def _cell(text) -> str:
    """Markdown 表格单元格：去换行；转义 Markdown 特殊字符（防止数据里的字被当成链接、图片、双链、
    公式、注释、高亮）；尖括号换成实体（防止被当成 HTML，也防止撞上标记）；最后转义竖线。"""
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    s = re.sub(r"([\\`*_\[\]()!#$%=~^])", r"\\\1", s)
    return s.replace("<", "&lt;").replace(">", "&gt;").replace("|", "\\|")


def _link_text(text) -> str:
    return _cell(text).replace("[", "(").replace("]", ")")


def _link_url(url: str) -> Optional[str]:
    u = str(url or "").strip()
    if not re.match(r"^https?://", u, re.I):
        return None
    u = u.replace("|", "%7C").replace(" ", "%20").replace("<", "%3C").replace(">", "%3E")
    return f"<{u}>" if ("(" in u or ")" in u) else u


def _md_link(text, url) -> str:
    u = _link_url(url)
    t = _link_text(text) or "链接"
    return f"[{t}]({u})" if u else t


def _yaml_str(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)


# ---------- 日期 ----------
def _d(s: Optional[str]) -> Optional[date]:
    try:
        return date.fromisoformat(str(s)[:10]) if s else None
    except ValueError:
        return None


def _md(d: date) -> str:
    return f"{d.month}月{d.day}日"


def date_text(start: Optional[str], end: Optional[str]) -> str:
    a, b = _d(start), _d(end)
    if not a:
        return "日期未定"
    if not b:
        return f"{_md(a)} 起"
    if a == b:
        return _md(a)
    return f"{_md(a)} – {_md(b)}"


# ---------- events.json ----------
def shop_url(brand: dict, region: str) -> Optional[str]:
    shop = brand.get("shop") or {}
    return shop.get(region) or shop.get("US") or shop.get("CA")


def event_record(e: dict, brand: dict) -> dict:
    """页面要用的字段。"""
    region = e.get("region") or "unknown"
    return {
        "id": e["id"],
        "brand_id": e["brand_id"],
        "brand": brand["name"],
        "category": brand.get("category"),
        "region": region,
        "event_type": e.get("event_type"),
        "type_zh": EVENT_NAMES_ZH.get(e.get("event_type"), e.get("event_type") or ""),
        "name": display_name(e),
        "event_name": e.get("event_name") or None,
        "start": e.get("start"),
        "end": e.get("end"),
        "discount": e.get("discount") or "",
        "status": "confirmed" if e.get("status") == "confirmed" else "rumored",
        "sources": [{"title": s.get("title") or s.get("url") or "",
                     "url": s.get("url") or "",
                     "site": s.get("name") or s.get("domain") or "",
                     "quote": s.get("quote") or ""}
                    for s in e.get("sources") or []],
        "shop_url": shop_url(brand, region),
    }


def _sort_key(r: dict) -> tuple:
    return (r.get("start") or "9999", r.get("end") or "9999", r["brand"].lower(), r["region"], r["id"])


def _vault_rel(prefix: str, rel: str) -> str:
    return f"{prefix}/{rel}" if prefix else rel


def build_payload(final_events: list, categories: list, brands: dict, data_updated: Optional[str],
                  generated_at: str, notes: Optional[dict] = None, meta: Optional[dict] = None) -> dict:
    """notes：brand_id → 笔记在库里的路径（不含 .md），首页和品牌列表靠它找笔记；不给就按文件名推。"""
    notes = notes or {}
    events = [event_record(e, brands[e["brand_id"]]) for e in final_events if e.get("brand_id") in brands]
    events.sort(key=_sort_key)
    return {
        "generated_at": generated_at,          # 这次同步的时间
        "data_updated": data_updated,          # 活动数据（final.json）最后一次算出来的时间
        "meta": dict(meta or {}),              # Mac 本地运行状态（status.json），例如 last_success
        "categories": [{"id": c["id"], "name": c["name"], "color": c.get("color")} for c in categories],
        "brands": [{"id": bid, "name": b["name"], "aliases": list(b.get("aliases") or []),
                    "category": b.get("category"), "shop": dict(b.get("shop") or {}),
                    "note": notes.get(bid) or f"{NOTES_DIR}/{note_name(b['name'])}"}
                   for bid, b in brands.items()],
        "events": events,
    }


# ---------- 品牌笔记 ----------
def render_block(brand: dict, cat_name: str, events: list, data_updated: Optional[str],
                 brand_id: str = "", prefix: str = DEFAULT_SUBDIR) -> str:
    """程序区（含首尾两个标记）。只依赖数据本身，数据不变时输出逐字相同，所以重复同步不会改文件。
    prefix：工作台在库里的子文件夹，导航双链用完整路径（库里还有别的「工作台」）。"""
    shop = brand.get("shop") or {}
    nav = (f"[[{_vault_rel(prefix, HOME_NOTE)}|← 黑五首页]] · [[{_vault_rel(prefix, '品牌')}|品牌列表]] · "
           f"[[{_vault_rel(prefix, '购物车')}|购物车]]")
    links = [_md_link(f"{REGION_FLAG[r]} {REGION_ZH[r]}官网", shop[r]) for r in ("US", "CA") if shop.get(r)]
    lines = [
        START,
        *([ID_LINE.format(brand_id)] if brand_id else []),
        "> [!info] 这一块由同步程序自动更新，每次同步整块重写。自己的笔记请写在下面的「我的备注」里。",
        "",
        nav,
        "",
        f"**类别**：{_cell(cat_name)}　**官网**：{' · '.join(links) if links else '（暂无）'}",
        "",
        "### 本季活动",
        "",
    ]
    if events:
        upd = (data_updated or "")[:10]
        if upd:
            lines += [f"数据更新于 {upd}，共 {len(events)} 条。", ""]
        lines += ["| 日期 | 地区 | 类型 | 折扣 | 状态 | 来源 |", "| --- | --- | --- | --- | --- | --- |"]
        for r in sorted(events, key=_sort_key):
            kind = r["type_zh"] if r["event_type"] != "other" else (r["name"] or r["type_zh"])
            srcs = [_md_link(s["site"] or s["title"], s["url"]) for s in r["sources"][:MAX_NOTE_SOURCES]]
            more = len(r["sources"]) - MAX_NOTE_SOURCES
            if more > 0:
                srcs.append(f"等 {len(r['sources'])} 条")
            lines.append("| " + " | ".join([
                date_text(r["start"], r["end"]),
                f"{REGION_FLAG.get(r['region'], '❔')} {REGION_ZH.get(r['region'], r['region'])}",
                _cell(kind),
                _cell(r["discount"]) or "—",
                STATUS_ZH[r["status"]],
                " · ".join(srcs) or "—",
            ]) + " |")
    else:
        lines.append("本季暂时还没有收集到这个品牌的活动消息。")
    lines.append(END)
    return "\n".join(lines)


def render_frontmatter(brand: dict, cat_name: str) -> str:
    """只在新建笔记时写一次。之后 frontmatter 属于用户区，同步再也不改它
    （brands.yaml 改了官网链接：程序区和 events.json 会跟着变，品牌列表页优先读 events.json）。"""
    shop = brand.get("shop") or {}
    regions = [r for r in ("US", "CA") if shop.get(r)]
    return "\n".join([
        "---",
        f"category: {_yaml_str(cat_name)}",
        f"regions: [{', '.join(regions)}]",
        f"shop_us: {_yaml_str(shop['US']) if shop.get('US') else ''}".rstrip(),
        f"shop_ca: {_yaml_str(shop['CA']) if shop.get('CA') else ''}".rstrip(),
        "tags: [品牌]",
        "---",
    ])


def render_new_note(brand: dict, cat_name: str, block: str) -> str:
    return "\n".join([
        render_frontmatter(brand, cat_name),
        "",
        block,
        "",
        USER_HEADING,
        "",
        "（这里是你自己的地方：想买什么、尺码、往年经验……同步程序不会改动这一节。）",
        "",
    ])


def _frontmatter_end(text: str) -> int:
    """frontmatter 结束的位置（第二个 --- 那一行之后）；没有 frontmatter 返回 0。"""
    if not text.startswith("---"):
        return 0
    m = re.match(r"---[ \t]*\r?\n(?:.*?\r?\n)?---[ \t]*(?:\r?\n|$)", text, re.S)
    return m.end() if m else 0


def update_note_text(old: str, block: str) -> tuple:
    """已存在的笔记 → (新全文 或 None 表示跳过, 情况说明)。
    情况：replaced 正常替换（全文可能和原来一样）/ inserted 标记都不在，插到 frontmatter 后 /
    broken 只有一个标记、标记重复或顺序颠倒，跳过不写。"""
    ns, ne = old.count(START), old.count(END)
    if ns == 1 and ne == 1:
        i, j = old.index(START), old.index(END)
        if i < j:
            return old[:i] + block + old[j + len(END):], "replaced"
        return None, "broken"
    if ns == 0 and ne == 0:
        k = _frontmatter_end(old)
        head, rest = old[:k], old[k:]            # rest（原来的正文）原样接在程序区后面
        if head and not head.endswith("\n"):
            head += "\n"
        text = head + ("\n" if head else "") + block + "\n"
        if rest:
            text += ("" if rest.startswith("\n") else "\n") + rest
        return text, "inserted"
    return None, "broken"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _brand_id_in(text: str) -> Optional[str]:
    """笔记程序区里记的 brand_id（标记不完整时在全文里找）。"""
    if text.count(START) == 1 and text.count(END) == 1 and text.index(START) < text.index(END):
        text = text[text.index(START):text.index(END)]
    m = _ID_RE.search(text)
    return m.group(1) if m else None


def locate_notes(notes_dir: Path, brands: dict) -> dict:
    """brand_id → 笔记路径。先按程序区里的 brand_id 找（用户改了文件名也认得），再按文件名；
    文件名撞了（两个品牌名去掉特殊字符后一样，或者那个文件记着别的品牌）打警告，用「品牌名 (id).md」。"""
    found: dict = {}
    claimed: dict = {}                       # 路径 → 记在里面的 brand_id
    for p in sorted(notes_dir.glob("*.md")) if notes_dir.is_dir() else []:
        try:
            bid = _brand_id_in(p.read_text(encoding="utf-8"))
        except OSError:
            continue
        if not bid:
            continue
        claimed[p] = bid
        if bid not in brands:
            continue
        if bid in found:
            log.warning("两篇笔记都记着 brand_id %s：用 %s，忽略 %s", bid, found[bid].name, p.name)
            continue
        found[bid] = p
    taken = {p: bid for bid, p in found.items()}
    out: dict = {}
    for bid, b in brands.items():
        if bid in found:
            out[bid] = found[bid]
            continue
        p = notes_dir / f"{note_name(b['name'])}.md"
        owner = taken.get(p) or claimed.get(p)
        if owner and owner != bid:
            alt = notes_dir / f"{note_name(b['name'])} ({bid}).md"
            log.warning("品牌笔记撞名：%s 已经属于 %s，%s 改用 %s", p.name, owner, bid, alt.name)
            p = alt
        taken[p] = bid
        out[bid] = p
    return out


def sync_notes(base: Path, payload: dict, brands: dict, categories: list, paths_by_id: dict,
               prefix: str = DEFAULT_SUBDIR) -> dict:
    cat_name = {c["id"]: c["name"] for c in categories}
    by_brand: dict = {}
    for r in payload["events"]:
        by_brand.setdefault(r["brand_id"], []).append(r)
    stats = {"created": [], "updated": [], "unchanged": [], "inserted": [], "skipped": []}
    for bid, b in brands.items():
        cname = cat_name.get(b.get("category"), b.get("category") or "")
        block = render_block(b, cname, by_brand.get(bid, []), payload.get("data_updated"), bid, prefix)
        path = paths_by_id[bid]
        if not path.exists():
            _write(path, render_new_note(b, cname, block))
            stats["created"].append(path.name)
            continue
        old = path.read_text(encoding="utf-8")
        new, how = update_note_text(old, block)
        if new is None:
            log.warning("品牌笔记的标记不完整（只有一个、重复或顺序颠倒），这篇跳过不写：%s", path)
            stats["skipped"].append(path.name)
            continue
        if new == old:
            stats["unchanged"].append(path.name)
            continue
        _write(path, new)
        if how == "inserted":
            log.warning("品牌笔记里找不到程序区标记，已把程序区插在 frontmatter 后面，原内容保留在下面：%s", path)
            stats["inserted"].append(path.name)
        else:
            stats["updated"].append(path.name)
    return stats


def sync(vault: Path, sales_path: Path, paths: Optional[Paths] = None, now: Optional[datetime] = None,
         subdir: str = DEFAULT_SUBDIR, overrides_applied: bool = False, meta: Optional[dict] = None) -> dict:
    """vault：Obsidian 库；subdir：工作台在库里的子文件夹（空字符串表示库的根目录）。
    sales_path：final.json（overrides_applied=True，已经套过人工纠错）或旧的 sales.json（这里再套一次）。
    meta：写进 events.json 的 meta（Mac 本地运行状态）。"""
    cfg = load_config(paths or Paths())
    raw = json.loads(sales_path.read_text(encoding="utf-8"))
    events = raw.get("events", []) if isinstance(raw, dict) else raw
    data_updated = (raw.get("updated") or raw.get("generated_at")) if isinstance(raw, dict) else None
    final = events if overrides_applied else apply_overrides(events, cfg.overrides)
    now = now or datetime.now(timezone.utc)
    prefix = subdir.strip("/")
    base = vault / prefix if prefix else vault
    paths_by_id = locate_notes(base / NOTES_DIR, cfg.brands)
    notes = {bid: p.relative_to(vault).with_suffix("").as_posix() for bid, p in paths_by_id.items()}
    payload = build_payload(final, cfg.categories, cfg.brands, data_updated, now.isoformat(timespec="seconds"),
                            notes, meta)
    out = base / DATA_REL
    _write(out, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    stats = sync_notes(base, payload, cfg.brands, cfg.categories, paths_by_id, prefix)
    stats["events"] = len(payload["events"])
    stats["events_path"] = str(out)
    return stats


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="把活动数据同步进 Obsidian 黑五购物工作台（events.json + 品牌笔记）")
    ap.add_argument("--vault", default=DEFAULT_VAULT, type=Path, help=f"Obsidian 库的文件夹（默认 {DEFAULT_VAULT}）")
    ap.add_argument("--subdir", default=DEFAULT_SUBDIR, help=f"工作台在库里的子文件夹（默认 {DEFAULT_SUBDIR}）")
    ap.add_argument("--final", default=None, type=Path, help="final.json 路径（默认 app/data/local/final.json）")
    ap.add_argument("--sales", default=None, type=Path, help="（旧用法）读没套过人工纠错的 sales.json，这里先套一次")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stdout)
    vault = args.vault.expanduser().resolve()
    if not vault.is_dir():
        print(f"找不到库的文件夹：{vault}")
        return 2
    paths = Paths()
    sales = (args.sales or args.final or paths.final).expanduser()
    if not sales.exists():
        print(f"找不到活动数据：{sales}（先运行 python -m dealradar.combine）")
        return 2
    meta = load_json(paths.status, {}) or {}
    st = sync(vault, sales, paths, subdir=args.subdir, overrides_applied=args.sales is None, meta=meta)
    print(f"events.json：{st['events']} 条活动 → {st['events_path']}")
    print(f"品牌笔记：新建 {len(st['created'])}，更新 {len(st['updated'])}，没变 {len(st['unchanged'])}，"
          f"补插程序区 {len(st['inserted'])}，跳过 {len(st['skipped'])}")
    for name in st["skipped"]:
        print(f"  跳过（标记不完整，请手动修好或删掉剩下的那个标记）：{name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
