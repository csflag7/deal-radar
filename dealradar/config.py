"""路径、配置文件读取、.env 加载、时间工具。"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

APP_ROOT = Path(__file__).resolve().parents[1]

EVENT_TYPES = ["early_bf", "black_friday", "cyber_monday", "cyber_week", "boxing_day", "other"]
REGIONS = ["US", "CA", "unknown"]


def key_dates(year: int) -> dict:
    """几个节日在某一年的日期（AI 提示词和日期核对都用）。"""
    nov1 = date(year, 11, 1)
    thanksgiving = nov1 + timedelta(days=(3 - nov1.weekday()) % 7 + 21)
    return {
        "US Thanksgiving": thanksgiving,
        "Black Friday": thanksgiving + timedelta(days=1),
        "Cyber Monday": thanksgiving + timedelta(days=4),
        "Boxing Day": date(year, 12, 26),
    }


@dataclass
class Paths:
    """数据文件分两处（§0.1 修订：每个文件只有一个写入方）：
    - data/news/：云端（GitHub Actions，--skip-official）写、提交进仓库的新闻部分；
    - data/local/：Mac 本地写、不进仓库的部分（官网监控、合并结果、日历 / 邮件状态）。"""
    config_dir: Path = APP_ROOT / "config"
    data_dir: Path = APP_ROOT / "data"
    web_out: Path = APP_ROOT / "web" / "data" / "data.js"     # 网页已停用（ADR-005）：run.py 默认不写
    env_file: Path = APP_ROOT / ".env"

    @property
    def news_dir(self) -> Path:
        return self.data_dir / "news"

    @property
    def local_dir(self) -> Path:
        return self.data_dir / "local"

    # ---- 云端写（data/news/）----
    @property
    def seen(self) -> Path:
        return self.news_dir / "seen.json"

    @property
    def sales(self) -> Path:
        return self.news_dir / "sales.json"

    @property
    def health(self) -> Path:
        return self.news_dir / "source_health.json"

    @property
    def changes(self) -> Path:
        return self.news_dir / "changes.json"

    # ---- Mac 写（data/local/）----
    @property
    def official_base(self) -> Path:
        return self.local_dir / "official_base.json"

    @property
    def official_state(self) -> Path:
        return self.local_dir / "official_state.json"

    @property
    def manual_review(self) -> Path:
        return self.local_dir / "manual_review.json"

    @property
    def final(self) -> Path:
        return self.local_dir / "final.json"

    @property
    def news_cache(self) -> Path:
        return self.local_dir / "news_cache.json"

    @property
    def status(self) -> Path:
        return self.local_dir / "status.json"

    @property
    def calendar_map(self) -> Path:
        return self.local_dir / "calendar_map.json"

    @property
    def digest_sent(self) -> Path:
        return self.local_dir / "digest_sent.json"

    @property
    def lock(self) -> Path:
        return self.local_dir / "mac_run.lock"


def load_yaml(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, obj: Any) -> None:
    """先写临时文件再改名，避免写到一半中断留下坏文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


@dataclass
class Config:
    settings: dict
    llm: dict             # config/llm.yaml：AI 提供方、模型、每批条数
    categories: list
    brands: dict          # brand_id -> {name, aliases, category, shop, domains}
    trusted_domains: list
    overrides: dict       # event_id -> {字段: 值} 或 {delete: true}
    manual_events: list = None   # config/manual_events.yaml 的 events（官网所见，人工记录）


def load_config(paths: Paths) -> Config:
    b = load_yaml(paths.config_dir / "brands.yaml")
    trusted = load_yaml(paths.config_dir / "trusted_media.yaml").get("domains") or []
    ov_path = paths.config_dir / "overrides.yaml"
    overrides = (load_yaml(ov_path).get("events") or {}) if ov_path.exists() else {}
    mn_path = paths.config_dir / "manual_events.yaml"
    manual = (load_yaml(mn_path).get("events") or []) if mn_path.exists() else []
    return Config(
        settings=load_yaml(paths.config_dir / "settings.yaml"),
        llm=load_yaml(paths.config_dir / "llm.yaml"),
        categories=b.get("categories") or [],
        brands=b.get("brands") or {},
        trusted_domains=[d.lower() for d in trusted],
        overrides=overrides,
        manual_events=manual,
    )


def load_env(path: Path) -> None:
    """读取 .env（KEY=VALUE 每行一个）。环境变量里已有的不覆盖，空值忽略。"""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        value = value.strip().strip('"').strip("'")
        if key and value and not os.environ.get(key):
            os.environ[key] = value


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def local_date(dt: datetime, tz_name: str) -> str:
    """把时间换成某个时区的日期字符串 YYYY-MM-DD。"""
    try:
        from zoneinfo import ZoneInfo
        return dt.astimezone(ZoneInfo(tz_name)).date().isoformat()
    except Exception:
        return dt.astimezone(timezone.utc).date().isoformat()
