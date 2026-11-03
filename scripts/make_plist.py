"""按本机路径，从 deploy/com.dealradar.mac.plist.template 生成 deploy/com.dealradar.mac.plist（launchd 配置）。

只生成文件，不加载。生成的文件含用户目录，已在 .gitignore 里，不进仓库。
用法（在 app 目录下）：
  .venv312/bin/python scripts/make_plist.py                 生成 deploy/com.dealradar.mac.plist
  plutil -lint deploy/com.dealradar.mac.plist               检查格式
加载（用户确认后再做，这个脚本不做）：
  cp deploy/com.dealradar.mac.plist ~/Library/LaunchAgents/ && launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.dealradar.mac.plist
"""
from __future__ import annotations

import argparse
import plistlib
import sys
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
TEMPLATE = APP / "deploy" / "com.dealradar.mac.plist.template"
DEFAULT_OUT = APP / "deploy" / "com.dealradar.mac.plist"
DEFAULT_LOG_DIR = Path.home() / "Library" / "Logs" / "DealRadar"


def render(app_dir: Path, log_dir: Path, template: Path = TEMPLATE) -> str:
    text = template.read_text(encoding="utf-8")
    text = text.replace("__APP_DIR__", str(app_dir)).replace("__LOG_DIR__", str(log_dir))
    plistlib.loads(text.encode("utf-8"))        # 格式不对就在这里报错
    return text


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="生成 launchd 配置 deploy/com.dealradar.mac.plist（只生成，不加载）")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--app-dir", type=Path, default=APP)
    ap.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR)
    args = ap.parse_args(argv)
    app_dir, log_dir = args.app_dir.expanduser().resolve(), args.log_dir.expanduser()
    if not (app_dir / ".venv312" / "bin" / "python").exists():
        print(f"提醒：{app_dir}/.venv312/bin/python 不存在，launchd 会启动失败")
    log_dir.mkdir(parents=True, exist_ok=True)        # launchd 不会自己建日志目录
    args.out.write_text(render(app_dir, log_dir), encoding="utf-8")
    print(f"已生成 {args.out}（没有加载；加载方法见本脚本开头的说明）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
