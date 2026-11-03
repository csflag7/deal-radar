"""每日流程：抓取 → 去重预筛 → 抓正文（只在内存里）→ AI 提取 → 状态判定 → 合并。

用法（在 app 目录下）：
  .venv/bin/python -m dealradar.run --skip-official --no-push   云端（GitHub Actions）：只抓新闻，只写 data/news/
  .venv312/bin/python -m dealradar.run --official [--discover]  Mac：只跑官网监控（S15，需要 Python 3.12 + crawl4ai），
                                                                只写 data/local/（Mac 上一般由 dealradar.mac_run 调用）
  .venv/bin/python -m dealradar.run --dry-run           只跑流程，不写任何文件，也不推送
  .venv/bin/python -m dealradar.run --no-ai --dry-run   只抓取和预筛，打印各源条数
  .venv/bin/python -m dealradar.run --fixture tests/fixtures/run --dry-run   用本地样本代替联网和 AI
  .venv/bin/python -m dealradar.run --web-out web/data/data.js   另外生成旧网页数据（网页已停用，默认不写）
不加 --official / --skip-official：新闻和官网都跑，各写各的位置（fixture 模式下默认不跑官网）。

文件分工（§0.1 修订：每个文件只有一个写入方）：
- 新闻部分 → data/news/{sales,seen,source_health,changes}.json（云端写、提交进仓库）；
- 官网部分 → data/local/{official_base,official_state,manual_review}.json（Mac 写、不进仓库）。
这里只存 AI 合并结果（不含人工记录和人工纠错）；人工记录、人工纠错和两部分的合并由 dealradar.combine 每次从头算，
结果写在 data/local/final.json。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from collections import Counter
from datetime import date
from pathlib import Path

from . import build_web
from .articles import fetch_articles
from .config import Paths, load_config, load_env, load_json, local_date, now_utc, save_json
from .extract import ExtractResult, FakeExtractor, LLMExtractor, run_extraction
from .filter import BrandMatcher, apply_cap, mark_seen, prefilter, prune_seen
from .llm_client import ChatClient
from .merge import merge_candidates, merge_run
from .notify import push_run
from .sources import build_feeds, fetch_all, load_fixture, update_health
from .status import finalize_candidates, judge

log = logging.getLogger("dealradar")

# 官网监控用的浏览器和 robots 检查；None = 用 official.py 的默认（Crawl4AI + 自己的 robots 检查）。测试里替换成假的。
OFFICIAL_CRAWLER_FACTORY = None
OFFICIAL_ROBOTS_OK = None


def setup_logging() -> None:
    root = logging.getLogger()
    if not any(getattr(h, "_dealradar", False) for h in root.handlers):
        h = logging.StreamHandler(sys.stdout)
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S"))
        h._dealradar = True
        root.addHandler(h)
    root.setLevel(logging.INFO)
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def summarize_fetch(results: list) -> list:
    lines = []
    for source, name in (("gnews", "Google News"), ("bing", "Bing News")):
        g = [r for r in results if r.feed.source == source]
        if not g:
            continue
        for region in ("US", "CA"):
            rs = [r for r in g if r.feed.region == region]
            ok = [r for r in rs if r.ok]
            lines.append(f"{name} {region}：{len(ok)}/{len(rs)} 个查询成功，共 {sum(len(r.items) for r in ok)} 条")
    for r in results:
        if r.feed.source not in ("gnews", "bing"):
            lines.append(f"{r.feed.name}：{r.describe()}")
    return lines


def summarize_events(events: list) -> list:
    n = len(events)
    with_start = sum(1 for e in events if e.get("start"))
    with_both = sum(1 for e in events if e.get("start") and e.get("end"))
    region = Counter(e["region"] for e in events)
    status = Counter(e.get("status") for e in events)
    return [
        f"事件 {n} 个；有开始日期 {with_start}，有起止日期 {with_both}",
        "地区：" + "、".join(f"{k} {region.get(k, 0)}" for k in ("US", "CA", "unknown")),
        "状态：" + "、".join(f"{k} {status.get(k, 0)}" for k in ("confirmed", "rumored")),
    ]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Deal Radar 抓取 + AI 提取 + 合并（新闻写 data/news/，官网写 data/local/）")
    ap.add_argument("--dry-run", action="store_true", help="只跑流程，不写 seen.json、sales.json 等任何文件，也不推送")
    ap.add_argument("--no-push", action="store_true", help="照常写文件，但不发 ntfy 推送")
    ap.add_argument("--no-ai", action="store_true", help="跳过 AI，只跑抓取和预筛")
    ap.add_argument("--fixture", type=Path, help="从本地 fixture 目录读输入、用假的提取结果，代替联网和 AI")
    ap.add_argument("--data-dir", type=Path, default=Paths.data_dir)
    ap.add_argument("--web-out", type=Path, default=None, help="另外生成旧网页数据 data.js（网页已停用，默认不写）")
    ap.add_argument("--config-dir", type=Path, default=Paths.config_dir)
    ap.add_argument("--official", action="store_true", help="只跑官网监控（不抓新闻）")
    ap.add_argument("--skip-official", action="store_true", help="不跑官网监控（GitHub Actions 用）")
    ap.add_argument("--discover", action="store_true", help="官网监控前先重新找页面（sitemap + 首页）")
    ap.add_argument("--official-pages", type=Path, default=None,
                    help="官网页面清单，默认 <config-dir>/official_pages.yaml")
    args = ap.parse_args(argv)

    setup_logging()
    paths = Paths(config_dir=args.config_dir, data_dir=args.data_dir, web_out=args.web_out or Paths.web_out)
    cfg = load_config(paths)
    s = cfg.settings
    now = now_utc()
    now_iso = now.isoformat(timespec="seconds")
    today = local_date(now, s.get("timezone", "UTC"))
    if args.dry_run:
        log.info("dry-run：只跑流程，不写任何文件")

    t0 = time.monotonic()
    # ① 抓取
    feeds = [] if args.official else build_feeds(cfg.brands, s.get("deal_feeds") or [], year=int(s["event_year"]),
                                                 bing=bool(s.get("bing_news", False)))
    if args.official:
        log.info("--official：只跑官网监控，不抓新闻")
    if args.fixture:
        log.info("从 fixture 读输入：%s", args.fixture)
        results = load_fixture(args.fixture, feeds)
    else:
        log.info("开始抓取 %d 个信息源（同一网站每次间隔 %s 秒）", len(feeds), s.get("request_delay_seconds"))
        results = fetch_all(feeds, s)
    failed = [r for r in results if not r.ok]
    items = [it for r in results if r.ok for it in r.items]
    health = update_health(load_json(paths.health, {}), results, now_iso)

    # ②③ 去重 + 预筛（先不套条数上限）
    seen = load_json(paths.seen, {})
    candidates_all, fst = prefilter(items, seen, BrandMatcher(cfg.brands),
                                    date.fromisoformat(str(s["cutoff_date"])), max_items=None)
    cap = int(s.get("max_items_per_run", 300))

    log.info("==== 抓取结果 ====")
    for line in summarize_fetch(results):
        log.info("  %s", line)
    report_failed(failed)
    log.info("（抓取 + 预筛用时 %.0f 秒）", time.monotonic() - t0)

    # ③½ 抓正文：在全部候选里挑最相关的去抓；截取的段落只放在内存里的条目上（item["excerpt"]），不写文件
    if args.no_ai:
        pass
    elif args.fixture:
        log.info("fixture 模式：不抓正文")
    else:
        t1 = time.monotonic()
        log.info("==== 正文 ====")
        try:
            ast = fetch_articles(candidates_all, cfg.brands, s)
            log.info("  %s（用时 %.0f 秒）", ast.describe(), time.monotonic() - t1)
        except Exception as e:   # 抓正文出任何问题都不影响后面：只用标题和摘要
            log.warning("  抓正文出错，本次只用标题和摘要：%s: %s", type(e).__name__, e)

    # ③¾ 条数上限：抓到正文的优先入选，剩下的名额按新的优先给只有标题的条目
    kept = apply_cap(candidates_all, cap, fst)
    log.info("==== 预筛 ====")
    log.info("  %s", fst.describe())
    if fst.non_na_reasons:
        log.info("  非北美的原因：%s", fst.non_na_reasons)
    log.info("  保留的按来源：%s", fst.kept_by_source or "无")

    if args.no_ai:
        if not args.dry_run and not args.official:      # 信息源健康属于新闻部分（data/news/）
            save_json(paths.health, health)
        log.info("--no-ai：跳过 AI 提取，到此结束")
        return 1 if results and len(failed) == len(results) else 0

    # ④ AI 提取
    t2 = time.monotonic()
    client = None
    if args.fixture:
        extractor = FakeExtractor(load_json(args.fixture / "extractions.json", {}))
    else:
        load_env(paths.env_file)
        key_env = cfg.llm.get("api_key_env", "DEEPSEEK_API_KEY")
        if not os.environ.get(key_env):
            log.error("没有找到 %s（环境变量或 app/.env），无法调用 AI。可以先用 --no-ai。", key_env)
            return 2
        client = ChatClient(cfg.llm, os.environ[key_env])
        log.info("AI：%s / %s", cfg.llm.get("provider"), cfg.llm.get("model"))
        extractor = LLMExtractor(cfg.brands, int(s["event_year"]), today, client)
    off_run = off_state = None
    off_res = ExtractResult()
    do_official = args.official or (not args.skip_official and not args.fixture)
    try:
        res = run_extraction(kept, extractor, set(cfg.brands), int(s["event_year"]),
                             int(cfg.llm.get("batch_size", 15)), cfg.llm.get("excerpt_batch_size"),
                             int(cfg.llm.get("concurrency", 1)))
        # ④½ 官网监控：抓官网 → 页面内容变了才交给 AI（S15）
        if do_official:
            log.info("==== 官网监控 ====")
            try:
                from . import official
                off_run, off_state = official.run_official(
                    cfg.brands, s, args.official_pages or (paths.config_dir / "official_pages.yaml"),
                    paths.official_state, paths.manual_review, now, discover=args.discover,
                    crawler_factory=OFFICIAL_CRAWLER_FACTORY, robots_ok=OFFICIAL_ROBOTS_OK,
                    write=not args.dry_run)
                log.info("  %s", off_run.describe())
                if off_run.items:
                    off_res = run_extraction(off_run.items, extractor, set(cfg.brands), int(s["event_year"]),
                                             int(cfg.llm.get("batch_size", 15)), cfg.llm.get("excerpt_batch_size"),
                                             int(cfg.llm.get("concurrency", 1)))
                    log.info("  官网页面交给 AI：%d 页 → %d 个事件", len(off_run.items), len(off_res.pairs))
            except Exception as e:   # 官网监控出任何问题都不影响新闻那部分
                log.warning("  官网监控出错，本次跳过：%s: %s", type(e).__name__, e)
                off_run = None
    finally:
        if client is not None:
            log.info("AI 用量：输入 %(prompt_tokens)s，输出 %(completion_tokens)s tokens", client.usage)
            client.close()
    for item, ev in off_res.pairs:          # 官网页面：地区直接用页面所属的地区
        ev["region"] = item["official_region"]
    for k, v in off_res.dropped.items():
        res.dropped[k] = res.dropped.get(k, 0) + v
    res.errors += [f"官网 {e}" for e in off_res.errors]
    canadian = [d.lower() for d in s.get("canadian_media_domains") or []]
    rejected: list = []
    # 类型校正 + other 的促销身份。新闻和官网分开判定、分开合并（两部分写在不同的文件里）
    candidates = finalize_candidates(judge(res.pairs, cfg.brands, cfg.trusted_domains, res.dropped, canadian),
                                     cfg.brands, res.dropped, rejected)
    off_candidates = finalize_candidates(judge(off_res.pairs, cfg.brands, cfg.trusted_domains, res.dropped, canadian),
                                         cfg.brands, res.dropped, rejected)
    log.info("==== AI 提取 ====")
    log.info("  %d 批，成功处理 %d 条（其中带正文 %d 条），得到 %d 个事件，引句核对后剩 %d 个（用时 %.0f 秒）",
             res.batches, len(res.processed), sum(1 for it in res.processed if it.get("excerpt")),
             len(res.pairs), len(candidates) + len(off_candidates), time.monotonic() - t2)
    if res.dropped:
        log.info("  丢弃 / 置空原因：%s", res.dropped)

    do_news = not args.official
    # ⑤ 合并 + 人工纠错 + 变更清单（新闻部分；data/news/sales.json 只存 AI 合并结果，人工纠错只用来算变更清单）
    sales_doc = load_json(paths.sales, None)
    first_run = sales_doc is None
    base = (sales_doc or {}).get("events", [])
    changes: list = []
    final: list = []
    if do_news:
        new_base, final, changes = merge_run(base, candidates, cfg.overrides, now_iso,
                                             int(s.get("max_sources_per_event", 8)))
        log.info("==== 合并（新闻）====")
        log.info("  事件 %d → %d 条（套用人工纠错后 %d 条）；变更 %d 条%s", len(base), len(new_base), len(final),
                 len(changes), "（首次运行，S4 不推送）" if first_run else "")
        for c in changes:
            log.info("  [%s] %s %s~%s %s", c["kind"], c["id"], c.get("start"), c.get("end"), c.get("discount") or "")
        log.info("==== 事件统计（新闻，套用人工纠错后）====")
        for line in summarize_events(final):
            log.info("  %s", line)
    # ⑤½ 官网部分：并进 data/local/official_base.json（只有这次真的跑了官网监控才动它）
    off_base = (load_json(paths.official_base, None) or {}).get("events", [])
    new_off = None
    if off_run is not None:
        new_off = merge_candidates(off_base, off_candidates, now_iso, int(s.get("max_sources_per_event", 8)))
        log.info("==== 合并（官网）====")
        log.info("  官网事件 %d → %d 条（本次官网候选 %d 个）", len(off_base), len(new_off), len(off_candidates))
        for line in summarize_events(new_off):
            log.info("  %s", line)
    log.info("（总用时 %.0f 秒）", time.monotonic() - t0)

    debug_dir = (paths.local_dir if args.official else paths.data_dir) / "debug"
    if args.dry_run:
        log.info("dry-run：不写 data/news/、data/local/ 里的任何文件")
    else:
        if do_news:
            mark_seen(seen, res.processed, today)
            save_json(paths.seen, prune_seen(seen, date.fromisoformat(today), int(s.get("seen_keep_days", 90))))
            save_json(paths.sales, {"updated": now_iso, "events": new_base})
            save_json(paths.changes, {
                "generated_at": now_iso,
                "first_run": first_run,
                "changes": changes,
                "failed_sources": [{"id": r.feed.id, "error": r.error} for r in failed],
                "ai_errors": res.errors,
                "ai_aborted": res.aborted,
                "ai_batches": res.batches,
                "ai_processed": len(res.processed),
            })
            save_json(paths.health, health)
            log.info("新闻部分已写入 %s", paths.news_dir)
        if args.web_out and do_news:      # 旧网页（已停用）：只有明确给了 --web-out 才写
            build_web.write(paths.web_out, build_web.build_data(final, cfg.categories, cfg.brands, now_iso))
            log.info("网页数据已写入 %s", paths.web_out)
        if off_run is not None:
            from . import official
            save_json(paths.official_base, {"updated": now_iso, "events": new_off})
            official.finish_official(off_state, off_run, off_res.processed, now_iso, paths.official_state,
                                     paths.manual_review)
            log.info("官网部分已写入 %s", paths.local_dir)
        if s.get("debug_candidates", True):
            n = write_candidate_archive(debug_dir, today, now_iso, candidates + off_candidates + rejected,
                                        int(s.get("debug_keep_days", 7)))
            log.info("候选存档：%d 条 → %s", n, debug_dir)

    # ⑥ 推送（只有新闻部分）：最多 1 条摘要 + 最多 1 条告警；首次运行不发摘要；--dry-run / --no-push 不推送
    if args.dry_run or args.no_push or not do_news:
        log.info("不推送（%s）", "--dry-run" if args.dry_run else ("--no-push" if args.no_push else "--official"))
    else:
        load_env(paths.env_file)
        try:
            push_run(s, changes, final, cfg.brands, health, first_run, date.fromisoformat(today),
                     res.aborted, res.errors, res.batches, len(res.processed))
        except Exception as e:   # 推送出问题不影响数据
            log.warning("推送出错：%s: %s", type(e).__name__, e)

    if res.errors:
        log.warning("AI 提取有 %d 批失败：%s", len(res.errors), res.errors)
    if res.aborted:
        return 2
    return 1 if results and len(failed) == len(results) else 0


ARCHIVE_FIELDS = ("brand_id", "region", "event_type", "ai_event_type", "event_name", "year", "start", "end",
                  "discount", "status", "sale_key", "sale_key_kind", "ai_start", "ai_end", "quote",
                  "event_id", "kept", "dropped_reason")


def write_candidate_archive(debug_dir: Path, today: str, now_iso: str, candidates: list, keep_days: int = 7) -> int:
    """调试用：把本次的候选（结构化字段、AI 原始日期、引句、链接、最后归到的事件 id）追加到
    data/debug/candidates-YYYY-MM-DD.jsonl。不存标题、摘要、正文。超过 keep_days 天的存档删掉。"""
    debug_dir.mkdir(parents=True, exist_ok=True)
    path = debug_dir / f"candidates-{today}.jsonl"
    with path.open("a", encoding="utf-8") as f:
        for c in candidates:
            src = c.get("source") or {}
            row = {"run_at": now_iso, **{k: c.get(k) for k in ARCHIVE_FIELDS},
                   "level": src.get("level"), "url": src.get("url"), "published": src.get("published"),
                   "quote_in": src.get("quote_in")}
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    limit = date.fromisoformat(today).toordinal() - keep_days
    for old in debug_dir.glob("candidates-*.jsonl"):
        try:
            if date.fromisoformat(old.stem[len("candidates-"):]).toordinal() < limit:
                old.unlink()
        except ValueError:
            continue
    return len(candidates)


def report_failed(failed: list) -> None:
    if not failed:
        log.info("所有信息源都抓取成功")
        return
    log.warning("==== 失败的信息源（%d 个，S4 告警用）====", len(failed))
    for r in failed:
        log.warning("  %s：%s", r.feed.id, r.error)


if __name__ == "__main__":
    sys.exit(main())
