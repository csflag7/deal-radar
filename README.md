# Deal Radar · 品牌打折日历

每天自动收集关注品牌（美国 / 加拿大）的黑五、网一、节礼日等打折消息，整理成网页日历，有变化时推送到手机。

- 信息源：Google News RSS、Bing News RSS（能拿到原文链接，挑最相关的抓正文段落）、Slickdeals / RedFlagDeals / Dealnews 的 RSS
- AI（DeepSeek）只负责“读”：品牌、地区、活动类型、日期、折扣、原文引句；状态（已官宣 / 传闻）和日期都由代码按来源和引句核对
- 推送：ntfy.sh，每天最多 1 条摘要 + 1 条告警；首次运行只建库不推送
- 网页：`web/index.html`（月 / 周 / 列表视图），数据在 `web/data/data.js`

## 目录结构

```
app/
  dealradar/          程序：sources 抓取、filter 预筛、articles 抓正文、extract AI 提取、status 核对与状态、
                      merge 合并、manual 官网所见、build_web 网页数据、notify 推送、probe 探针、run 每日流程
  config/             brands.yaml 品牌清单、settings.yaml 运行参数、llm.yaml AI 设置、trusted_media.yaml 权威媒体、
                      overrides.yaml 人工纠错（最优先）、manual_events.yaml 官网所见（人工记录）
  data/               sales.json 事件库、seen.json 已处理条目、source_health.json 信息源健康、changes.json 本次变化
                      （都提交进仓库，下次运行接着用；data/debug/ 是调试存档，不提交）
  web/                静态网页（GitHub Pages 部署这个目录）
  tests/              离线测试（pytest）
  .github/workflows/  daily.yml 每日运行；probe.yml 手动探针
```

仓库是公开的：抓到的新闻正文只在内存里用，不写文件；sales.json 里每条来源只存标题、链接和不超过 200 字的引句。

## 本地运行

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest -q                                  # 测试（离线）
.venv/bin/python -m dealradar.run --dry-run                    # 完整跑一遍，但不写文件、不推送
.venv/bin/python -m dealradar.run --no-push                    # 正常写 data/ 和 web/data/data.js，不推送
.venv/bin/python -m dealradar.notify --preview --data-dir data # 预览将要发的推送（不发送）
.venv/bin/python -m dealradar.probe                            # 探针：各信息源能不能访问
```

本地的 Key 和主题名写在 `app/.env`（不会提交）：

```
DEEPSEEK_API_KEY=<你的 DeepSeek API key>
NTFY_TOPIC=你的私有主题名
```

## GitHub 上需要的两个 Secrets

| 名称 | 用途 |
|---|---|
| `DEEPSEEK_API_KEY` | AI 提取（DeepSeek） |
| `NTFY_TOPIC` | ntfy 推送主题名（当作密码保管，别人知道就能看到推送） |

开通 GitHub Pages（Source 选 GitHub Actions）后，把网页地址填进 `config/settings.yaml` 的 `site_url`，推送末尾会附上链接。
