"""用本机 Chrome（无头模式）打开链接，读取页面标题，用来人工核实被脚本拦截的购物链接。
用法：python scripts/check_titles.py URL [URL ...]
"""
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


def title(url: str) -> str:
    try:
        out = subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--virtual-time-budget=8000", "--dump-dom", url],
                             capture_output=True, text=True, timeout=45).stdout
    except subprocess.TimeoutExpired:
        return "（超时）"
    m = re.search(r"<title[^>]*>([^<]*)", out)
    return m.group(1).strip()[:80] if m else f"（无标题，页面 {len(out)} 字节）"


if __name__ == "__main__":
    urls = sys.argv[1:]
    with ThreadPoolExecutor(6) as ex:
        for u, t in zip(urls, ex.map(title, urls)):
            print(f"{u} | {t}")
