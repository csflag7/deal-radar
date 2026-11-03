"""S6：部署相关文件的检查（workflow、requirements、.gitignore、要提交的数据文件里没有正文）。"""
from __future__ import annotations

import json
import shutil

import yaml

from conftest import APP, FIXTURES
from dealradar.run import main

WF = APP / ".github" / "workflows"


def test_daily_workflow():
    d = yaml.safe_load((WF / "daily.yml").read_text(encoding="utf-8"))
    on = d[True]                                  # YAML 1.1 把 on 读成 True
    assert on["schedule"] == [{"cron": "17 */6 * * *"}] and "workflow_dispatch" in on      # 每 6 小时（ADR-008）
    assert d["permissions"] == {"contents": "write"}
    assert d["concurrency"]["cancel-in-progress"] is False
    job = d["jobs"]["daily"]
    assert "environment" not in job and "env" not in job      # Key 只给 Daily run 那一步
    steps = job["steps"]
    runs = [s.get("run", "") for s in steps]
    uses = [s.get("uses", "") for s in steps]
    run_step = next(s for s in steps if s.get("run", "").startswith("python -m dealradar.run"))
    # S15：官网监控只在 Mac 本地跑，Actions 里加 --skip-official（只写 data/news/）
    assert run_step["run"].split("#")[0].strip() == "python -m dealradar.run --skip-official --no-push"
    assert run_step["env"] == {"DEEPSEEK_API_KEY": "${{ secrets.DEEPSEEK_API_KEY }}"}
    assert [s for s in steps if s.get("env")] == [run_step]
    test_step = next(s for s in steps if "pytest" in s.get("run", ""))
    assert test_step["run"].split("#")[0].strip() == 'pytest -q -m "not mac"'     # 只在 Mac 上用的测试跳过
    order = [i for i, s in enumerate(steps) if "pytest -q" in s.get("run", "")] + \
            [steps.index(run_step)] + \
            [i for i, s in enumerate(steps) if "git push" in s.get("run", "")]
    assert order == sorted(order) and len(order) == 3
    assert any("python-version" in (s.get("with") or {}) and s["with"]["python-version"] == "3.12" for s in steps)
    commit = next(r for r in runs if "git push" in r)
    assert 'github-actions[bot]' in commit and "data: news update" in commit
    adds = [line.strip() for line in commit.splitlines() if line.strip().startswith("git add")]
    assert adds == ["git add data/news/"]                     # 云端只提交自己写的 data/news/
    assert commit.index('git pull --rebase origin "$GITHUB_REF_NAME"') < commit.index("git push")
    # 网页部署、ntfy 推送和失败告警都已停用（ADR-005）
    assert not any("pages" in u for u in uses)
    assert not any(s.get("if") == "failure()" for s in steps)
    text = (WF / "daily.yml").read_text(encoding="utf-8")
    assert "NTFY" not in text and "secrets." in text and text.count("secrets.") == 1
    assert any(u.startswith("actions/checkout") for u in uses)


def test_probe_workflow_manual_only():
    d = yaml.safe_load((WF / "probe.yml").read_text(encoding="utf-8"))
    assert list(d[True]) == ["workflow_dispatch"]
    assert d["permissions"] == {"contents": "read"}
    runs = [s.get("run", "") for s in d["jobs"]["probe"]["steps"]]
    assert "python -m dealradar.probe" in runs and not any("git " in r or "dealradar.run" in r for r in runs)


def test_requirements_pinned_and_gitignore():
    req = [l for l in (APP / "requirements.txt").read_text(encoding="utf-8").splitlines()
           if l.strip() and not l.startswith("#")]
    common = [l for l in req if ";" not in l]
    assert {l.split("==")[0].lower() for l in common} == {"pyyaml", "httpx", "feedparser", "pytest"}
    assert all("==" in l for l in req)
    # 只在 Mac 上装的（EventKit），带平台条件，云端 pip 自动跳过
    mac_only = [l for l in req if ";" in l]
    assert [l.split("==")[0].lower() for l in mac_only] == ["pyobjc-framework-eventkit"]
    assert all(l.split(";", 1)[1].strip() == 'sys_platform == "darwin"' for l in mac_only)
    ignore = [l.strip() for l in (APP / ".gitignore").read_text(encoding="utf-8").splitlines()
              if l.strip() and not l.strip().startswith("#")]
    for p in (".venv*/", ".env*", "__pycache__/", ".pytest_cache/", "data/debug/", "reports/raw/", "data/local/",
              "logs/", "*.log", ".DS_Store", "data/sales.pre-split.json", "config/manual_events.yaml"):
        assert p in ignore, p
    assert "config/manual_events.example.yaml" not in ignore and "data/news/" not in ignore
    for f in (APP / "dealradar").glob("*.py"):
        assert "import anthropic" not in f.read_text(encoding="utf-8")


def test_committed_data_files_have_no_article_text(tmp_path):
    conf = tmp_path / "config"
    shutil.copytree(APP / "config", conf)
    data = tmp_path / "data"
    assert main(["--fixture", str(FIXTURES / "run"), "--config-dir", str(conf), "--data-dir", str(data),
                 "--no-push", "--skip-official"]) == 0
    assert not (data / "local").exists() and not (tmp_path / "w.js").exists()     # 云端只写 data/news/
    sales = json.loads((data / "news" / "sales.json").read_text(encoding="utf-8"))
    for e in sales["events"]:
        for s in e["sources"]:
            assert not ({"summary", "excerpt", "content", "html"} & set(s))
            assert len(s["quote"]) <= 200
    seen = json.loads((data / "news" / "seen.json").read_text(encoding="utf-8"))
    assert all(isinstance(v, str) and len(v) == 10 for v in seen.values())        # 只有 key → 日期
    health = json.loads((data / "news" / "source_health.json").read_text(encoding="utf-8"))
    allowed = {"last_success", "consecutive_failures", "last_attempt", "last_count", "last_error"}
    assert all(set(h) <= allowed for h in health.values())


def test_manual_events_example_has_no_real_records():
    text = (APP / "config" / "manual_events.example.yaml").read_text(encoding="utf-8")
    assert yaml.safe_load(text)["events"] == []
    assert "bestbuy" not in text.lower()          # 样例只演示格式，没有真实的浏览记录


def test_precommit_scan(tmp_path):
    """scripts/precommit_scan.sh：被 .gitignore 忽略的路径不扫；命中时退出码 1。"""
    import subprocess
    app = tmp_path / "app"
    (app / "scripts").mkdir(parents=True)
    shutil.copy(APP / "scripts" / "precommit_scan.sh", app / "scripts" / "precommit_scan.sh")
    (app / ".gitignore").write_text("data/local/\n*.log\nconfig/manual_events.yaml\n.venv*/\n", encoding="utf-8")
    (app / "data" / "local").mkdir(parents=True)
    (app / "config").mkdir()
    (app / ".venv312").mkdir()
    mail = "someone@" + "outlook" + ".com"           # 拼起来写，免得这个测试文件自己被扫到
    for p in ("data/local/x.json", "run.log", "config/manual_events.yaml", ".venv312/y.py"):
        (app / p).write_text(mail, encoding="utf-8")
    (app / "ok.py").write_text("category = 'task-force'  # 不算命中\n", encoding="utf-8")

    def scan():
        return subprocess.run(["bash", str(app / "scripts" / "precommit_scan.sh")], capture_output=True, text=True)
    r = scan()
    assert r.returncode == 0 and "没有命中" in r.stdout, r.stdout + r.stderr
    (app / "README.md").write_text("key: " + "sk" + "-abc123\nby " + "e" + "go browser\n", encoding="utf-8")
    r = scan()
    assert r.returncode == 1 and "README.md:1:" in r.stdout and "README.md:2:" in r.stdout
