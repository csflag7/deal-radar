"""购物车提醒的路径和设置（S13 / S14，ADR-008）。

文件分工（§0.1 修订）：
- cart.json            只由用户写（Obsidian 页面）；这里**只读**
- cart_state.json      只由程序写：识别出的型号和候选、商品页、搜索链接、最新价格、建议
- prices.json          只由程序写：每个商品页的价格历史和读取状态
- notified.json        只由程序写：发过的提醒（去重），**只在真的发出邮件后才写**
- cart_pending.json    只由程序写：本次生成的汇总里有哪些提醒（发送成功后据此写 notified.json）
- mail_preview/        预览模式写的邮件（HTML + 文本）
都在 Obsidian 的 邮件工作台/黑五购物/_黑五/data/ 下面，不进仓库。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..config import APP_ROOT, load_json, load_yaml, save_json

CART_YAML = APP_ROOT / "config" / "cart.yaml"
BRANDS_YAML = APP_ROOT / "config" / "brands.yaml"
LLM_YAML = APP_ROOT / "config" / "llm.yaml"
DEFAULT_DATA_DIR = APP_ROOT.parents[1] / "邮件工作台" / "黑五购物" / "_黑五" / "data"
MAIL_CONFIG = Path.home() / "Library" / "Application Support" / "DealRadar" / "mail.json"
ENV_DATA_DIR = "DEALRADAR_CART_DATA_DIR"


def load_cart_config(path: Path = CART_YAML) -> dict:
    return load_yaml(path) if path.exists() else {}


def load_brands(path: Path = BRANDS_YAML) -> dict:
    return (load_yaml(path).get("brands") or {}) if path.exists() else {}


def resolve_data_dir(settings: Optional[dict] = None, cart_cfg: Optional[dict] = None,
                     override: Optional[Path] = None) -> Path:
    """优先级：参数 > settings['cart_data_dir'] > 环境变量 DEALRADAR_CART_DATA_DIR > cart.yaml 的 data_dir > 默认。"""
    for v in (override, (settings or {}).get("cart_data_dir"), os.environ.get(ENV_DATA_DIR),
              (cart_cfg or {}).get("data_dir")):
        if v:
            return Path(v).expanduser()
    return DEFAULT_DATA_DIR


@dataclass
class Files:
    dir: Path

    @property
    def cart(self) -> Path:
        return self.dir / "cart.json"

    @property
    def state(self) -> Path:
        return self.dir / "cart_state.json"

    @property
    def prices(self) -> Path:
        return self.dir / "prices.json"

    @property
    def notified(self) -> Path:
        return self.dir / "notified.json"

    @property
    def pending(self) -> Path:
        return self.dir / "cart_pending.json"

    @property
    def preview_dir(self) -> Path:
        return self.dir / "mail_preview"


def read_json(path: Path, default):
    """读程序自己写的 JSON；坏了就当没有（下次整份重写）。"""
    try:
        return load_json(path, default)
    except (ValueError, OSError):
        return default


def write_json(path: Path, obj) -> None:
    """原子写：先写临时文件再改名（config.save_json）。"""
    save_json(path, obj)
