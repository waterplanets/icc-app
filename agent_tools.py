"""Agent 插件页的后端：安装自检 + 用法提示词。

只做三件事：
1. 读 ~/.dsh/profiles/desktop 的 package.json / node_modules，判断插件是否装上、是否进了 bundles；
2. 给出可直接复制的提示词；
3. 跑一次 `ml/cli.py predict`，验证 Python 侧的 JSON 契约还通。
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

BASE_DIR = Path(__file__).resolve().parent
REPO = BASE_DIR.parent
DSH_PROFILE = Path.home() / ".dsh" / "profiles" / "desktop"
PYTHON = Path(r"D:\myProgram\IDE\compilers\Python\Python3\python.exe")


def _checks() -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    pkg_path = DSH_PROFILE / "package.json"

    pkg: Dict[str, Any] = {}
    if pkg_path.exists():
        try:
            pkg = json.loads(pkg_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pkg = {}
    deps = pkg.get("dependencies") or {}
    bundles = ((pkg.get("dsh") or {}).get("profile") or {}).get("bundles") or []

    out.append({
        "name": "插件目录",
        "ok": (REPO / "dsh-plugin" / "lib" / "index.js").exists(),
        "detail": str(REPO / "dsh-plugin"),
    })
    out.append({
        "name": "写入 profile 依赖",
        "ok": "dsh-icc-tools" in deps,
        "detail": deps.get("dsh-icc-tools") or "package.json 里没有这一项",
    })
    out.append({
        "name": "进入 profile bundles",
        "ok": "dsh-icc-tools" in bundles,
        "detail": "bundles 决定启动时是否加载",
    })
    out.append({
        "name": "node_modules 链接",
        "ok": (DSH_PROFILE / "node_modules" / "dsh-icc-tools").exists(),
        "detail": "pnpm link 到工作区的软链",
    })
    out.append({
        "name": "Python 解释器",
        "ok": PYTHON.exists(),
        "detail": str(PYTHON),
    })
    return out


def status() -> Dict[str, Any]:
    checks = _checks()
    return {"checks": checks, "ready": all(c["ok"] for c in checks),
            "profile": str(DSH_PROFILE)}


def prompts(asof: str) -> List[Dict[str, str]]:
    return [
        {
            "title": "给新闻打分",
            "text": "用 news_sentiment 给下面三条新闻打情感标签，只返回标签、分数和置信度：\n"
                    "1. 宁德时代三季度净利同比增长，拟回购 400 亿元。\n"
                    "2. 某公司因信息披露违规被立案调查，股价跌停。\n"
                    "3. 央行宣布降准 0.5 个百分点，释放长期资金约 1 万亿元。",
        },
        {
            "title": "跑一次个股辩论",
            "text": f"用 stock_debate 对 600519.SH 跑一次辩论，asof 用 {asof}，"
                    "然后告诉我五档评级、建议仓位，以及风控三方各自的立场。",
        },
        {
            "title": "把情绪和决议串起来",
            "text": f"先用 news_sentiment 打分这几条新闻，再用 stock_debate 对 000333.SZ 跑一次"
                    f"（asof {asof}），最后说明新闻情绪有没有被组合经理采纳。",
        },
    ]


def selftest() -> Dict[str, Any]:
    """验证插件 spawn 的那条 Python 通道仍然可用。"""
    if not PYTHON.exists():
        return {"ok": False, "error": f"找不到解释器 {PYTHON}"}
    samples = [
        {"title": "宁德时代三季报净利大增，拟 400 亿回购", "content": "公司称产能利用率维持高位。"},
        {"title": "某公司因信息披露违规被立案调查，股价跌停", "content": "交易所下发关注函。"},
    ]
    payload = "\n".join(json.dumps(s, ensure_ascii=False) for s in samples) + "\n"
    t0 = time.time()
    try:
        r = subprocess.run([str(PYTHON), str(REPO / "ml" / "cli.py"), "predict"],
                           input=payload.encode("utf-8"), cwd=str(REPO),
                           capture_output=True, timeout=120)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "打分超时（120 秒）"}
    ms = int((time.time() - t0) * 1000)
    if r.returncode != 0:
        err = r.stderr.decode("utf-8", "replace").strip().splitlines()[-3:]
        return {"ok": False, "error": "；".join(err), "ms": ms}
    items = []
    for line in r.stdout.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            items.append(json.loads(line))
        except json.JSONDecodeError:
            return {"ok": False, "error": f"输出不是 JSON：{line[:80]}", "ms": ms}
    return {"ok": len(items) == len(samples), "items": items, "ms": ms}
