#!/usr/bin/env bash
# 公开仓库提交前的敏感词扫描（§0.1 修订）：在 app/ 下找邮箱、用户名、API key、浏览器工具名这类不该公开的字。
# 被 .gitignore 忽略的路径不扫（它们不会进仓库）；这个脚本自己也不扫（里面写着要找的词）。
#
# 用法（在任何目录）：bash scripts/precommit_scan.sh
# 结果：没有命中 → 打印“没有命中”、退出码 0；有命中 → 列出 文件:行号:内容、退出码 1。
#
# 文件清单：app/ 已经是 git 仓库时用 git ls-files（已跟踪 + 未跟踪但没被忽略的）；
# 还不是仓库时，按 .gitignore 自己排除（只支持本项目用到的写法：目录 xx/、路径 a/b、文件名通配 *.log）。
set -u
cd "$(dirname "$0")/.." || exit 2
SELF="scripts/precommit_scan.sh"

# 不分大小写：邮箱域名、用户名（含拼音名的一部分）
PAT_I='outlook\.com|caishuqi|shuqi'
# 分大小写、按整词：API key 前缀、浏览器工具名（"ego" 只算独立的词，category 之类不算）
PAT_W='sk-[A-Za-z0-9_-]*|ego'

list_files() {
  if git rev-parse --is-inside-work-tree >/dev/null 2>&1 && [ "$(git rev-parse --show-toplevel)" = "$(pwd -P)" ]; then
    git ls-files -co --exclude-standard
    return
  fi
  local prune=() fexcl=() line pat
  while IFS= read -r line || [ -n "$line" ]; do
    pat="${line%%#*}"
    pat="$(printf '%s' "$pat" | sed -e 's/[[:space:]]*$//' -e 's/^[[:space:]]*//')"
    [ -z "$pat" ] && continue
    case "$pat" in
      '!'*) echo "提醒：.gitignore 里的反向规则 $pat 不支持，忽略" >&2; continue ;;
    esac
    pat="${pat#/}"
    if [ "${pat%/}" != "$pat" ]; then             # 目录
      pat="${pat%/}"
      case "$pat" in
        */*) prune+=( -o -path "./$pat" ) ;;
        *)   prune+=( -o -name "$pat" ) ;;
      esac
    else                                          # 文件
      case "$pat" in
        */*) fexcl+=( -o -path "./$pat" ) ;;
        *)   fexcl+=( -o -name "$pat" ) ;;
      esac
    fi
  done < .gitignore
  # 第一个 -o 前面放一个永远不成立的条件
  find . \( -path ./.git ${prune[@]+"${prune[@]}"} \) -prune -o -type f \
    ! \( -name '.__never__' ${fexcl[@]+"${fexcl[@]}"} \) -print | sed 's#^\./##'
}

hits=0
tmp="$(mktemp)"
list_files | grep -v -x -F "$SELF" | sort > "$tmp"
while IFS= read -r f; do
  [ -f "$f" ] || continue
  out="$( { grep -nIiE "$PAT_I" -- "$f"; grep -nIwE "$PAT_W" -- "$f"; } 2>/dev/null | sort -t: -k1,1n -u )"
  if [ -n "$out" ]; then
    printf '%s\n' "$out" | sed "s#^#$f:#" | cut -c1-240
    hits=$((hits + $(printf '%s\n' "$out" | wc -l)))
  fi
done < "$tmp"
n=$(wc -l < "$tmp" | tr -d ' ')
rm -f "$tmp"

if [ "$hits" -gt 0 ]; then
  echo "共 $hits 处命中（扫了 $n 个文件）：请改掉，或者确认这个文件该加进 .gitignore" >&2
  exit 1
fi
echo "没有命中（扫了 $n 个文件）"
exit 0
