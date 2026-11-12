"""LLM 辩论：多头 / 空头 / 主席，三轮调用，产出可留档的决议。

和 5.3 的规则投委会并行存在，区别只有一个：
- 规则投委会：8 位人格各按 YAML 权重打分，输入输出都确定；
- 这里：把同一份证据交给 LLM，多头和空头各自找论据，主席裁决。

提示词全部来自 `config/debate_prompt.yaml`，改文件即换风格，不用改代码。
API Key 依次从环境变量 DEEPSEEK_API_KEY、~/.dsh/.credentials.yaml 读取。
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR.parent / "config" / "debate_prompt.yaml"
API_URL = "https://api.deepseek.com/chat/completions"


class DebateError(RuntimeError):
    pass


def load_config(path: Path = CONFIG_PATH) -> Dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    try:
        import yaml
        return yaml.safe_load(text)
    except Exception:
        # 没有 pyyaml 时退回极简解析：只支持本文件用到的两层结构
        return _mini_yaml(text)


def _mini_yaml(text: str) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    block: Optional[str] = None
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        line = raw.strip()
        if indent == 0 and line.endswith(":"):
            block = line[:-1]
            data[block] = {}
            continue
        if ":" in line and indent > 0 and not line.startswith("-"):
            k, v = line.split(":", 1)
            v = v.strip()
            if v == "|":
                block = k.strip()
                data[block] = []
                continue
            if block and isinstance(data.get(block), dict):
                try:
                    data[block][k.strip()] = int(v)
                except ValueError:
                    data[block][k.strip()] = float(v) if re.match(r"^[\d.]+$", v) else v
        elif block and isinstance(data.get(block), list):
            data[block].append(line)
    for k, v in list(data.items()):
        if isinstance(v, list):
            data[k] = "\n".join(v)
    return data


def api_key() -> str:
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if key:
        return key
    cred = Path.home() / ".dsh" / ".credentials.yaml"
    if cred.exists():
        m = re.search(r"DEEPSEEK_API_KEY:\s*(\S+)", cred.read_text(encoding="utf-8", errors="replace"))
        if m:
            return m.group(1).strip()
    raise DebateError("没有找到 DEEPSEEK_API_KEY（环境变量或 ~/.dsh/.credentials.yaml）")


def chat(cfg: Dict[str, Any], system: str, user: str, json_mode: bool = True) -> str:
    payload = {
        "model": cfg.get("model", "deepseek-chat"),
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "temperature": float(cfg.get("temperature", 0.6)),
        "max_tokens": int(cfg.get("max_tokens", 700)),
    }
    if json_mode:
        # DeepSeek 的 json_object 模式要求提示词里出现 "json"，自由发言的角色不能带
        payload["response_format"] = {"type": "json_object"}
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(API_URL, data=body, headers={
        "Authorization": f"Bearer {api_key()}", "Content-Type": "application/json"})
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
            return data["choices"][0]["message"]["content"]
        except Exception as e:
            if attempt:
                raise DebateError(f"LLM 调用失败：{type(e).__name__}: {e}") from e
            time.sleep(1.5)
    return ""


def _json(text: str) -> Dict[str, Any]:
    text = text.strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
    return {"error": "返回不是合法 JSON", "raw": text[:200]}


def _num(v: Any, lo: float, hi: float) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return lo
    return max(lo, min(hi, x))


def build_evidence(conn: sqlite3.Connection, symbol: str, asof: str,
                   cfg: Dict[str, Any], council_payload: Optional[Dict[str, Any]] = None) -> str:
    name = symbol
    row = conn.execute("SELECT name FROM instruments WHERE symbol=?", (symbol,)).fetchone()
    if row:
        name = row[0]

    bars = conn.execute(
        "SELECT date, close FROM bars WHERE symbol=? AND date<=? ORDER BY date DESC LIMIT 2",
        (symbol, asof)).fetchall()
    if bars:
        close = float(bars[0]["close"])
        prev = float(bars[1]["close"]) if len(bars) > 1 else close
        quote = f"最新收盘 {close:.2f}（{bars[0]['date']}），当日 {((close / prev - 1) * 100):+.2f}%"
    else:
        quote = "本地没有该标的的日线"

    factors = "缺"
    council_txt = "未运行规则投委会"
    if council_payload and council_payload.get("items"):
        it = council_payload["items"][0]
        f = it.get("features", {})

        def pct(k: str, scale: float = 100.0) -> str:
            v = f.get(k)
            return f"{v * scale:+.1f}%" if isinstance(v, (int, float)) else "—"

        factors = (f"20日动量 {pct('mom_20')}，60日动量 {pct('mom_60')}，"
                   f"年化波动 {pct('vol_20')}，RSI14 {f.get('rsi14', 0):.0f}，"
                   f"均线趋势 {pct('trend')}，距52周高点 {pct('dist_52w_high')}，"
                   f"最大回撤 {pct('max_dd_252')}")
        bulls = "、".join(f"{b['name']}{b['score']}" for b in it.get("bulls", [])) or "无"
        bears = "、".join(f"{b['name']}{b['score']}" for b in it.get("bears", [])) or "无"
        council_txt = (f"聚合分 {it['score']:+.1f}（{it['stance']}），分歧度 {it['dispersion']:.1f}，"
                       f"信心 {it['confidence']:.2f}\n多头：{bulls}\n空头：{bears}")

    limit = int(cfg.get("max_news", 8))
    chars = int(cfg.get("max_chars_per_news", 110))
    news_rows = conn.execute(
        "SELECT date, title, label, score, confidence FROM news "
        "WHERE symbol=? AND date<=? AND label IS NOT NULL ORDER BY date DESC, time DESC LIMIT ?",
        (symbol, asof, limit)).fetchall()
    if news_rows:
        news = "\n".join(
            f"[{r['date']}] {({'1': '正面', '-1': '负面'}.get(str(r['label']), '中性'))}"
            f"（{r['score']:+.2f}/{r['confidence']:.2f}）{r['title'][:chars]}"
            for r in news_rows)
    else:
        news = "该标的暂无已打分的新闻"

    tpl = cfg
    return (str(tpl.get("evidence_header", ""))
            .replace("{name}", str(name)).replace("{symbol}", symbol).replace("{asof}", asof)
            .replace("{quote}", quote).replace("{factors}", factors)
            .replace("{council}", council_txt).replace("{news}", news)
            .replace("{memory}", _memory_text(conn, symbol, asof))
            .replace("{limits}", str(tpl.get("limits", "")))).strip()


def _memory_text(conn: sqlite3.Connection, symbol: str, asof: str) -> str:
    """历史决议：模仿 TradingAgents 的决策记忆，避免同一标的反复改口。"""
    rows = conn.execute(
        "SELECT asof, payload FROM debate_runs WHERE symbol=? AND asof<=? "
        "ORDER BY id DESC LIMIT 2", (symbol, asof)).fetchall()
    if not rows:
        return "无历史决议"
    out = []
    for r in rows:
        try:
            d = json.loads(r[1])
            out.append(f"{r[0]}：{d.get('verdict')}，仓位 {d.get('position_pct')}%，"
                       f"理由 {str((d.get('judge') or {}).get('reason', ''))[:40]}")
        except json.JSONDecodeError:
            continue
    return "\n".join(out) or "无历史决议"


RATING_CAP = {"Buy": 10.0, "Overweight": 6.0, "Hold": 2.0, "Underweight": 0.0, "Sell": 0.0}
RATING_VERDICT = {"Buy": "买入", "Overweight": "买入", "Hold": "观察",
                  "Underweight": "回避", "Sell": "回避"}


def _rating(v: Any) -> str:
    r = str(v or "").strip().capitalize()
    return r if r in RATING_CAP else "Hold"


def run(conn: sqlite3.Connection, symbol: str, asof: str,
        council_payload: Optional[Dict[str, Any]] = None, record: bool = True) -> Dict[str, Any]:
    """按 TradingAgents 的团队分工跑完整流水线，全程留痕。"""
    cfg = load_config()
    evidence = build_evidence(conn, symbol, asof, cfg, council_payload)
    roles = cfg.get("roles") or {}
    name = symbol
    row = conn.execute("SELECT name FROM instruments WHERE symbol=?", (symbol,)).fetchone()
    if row:
        name = row[0]
    sub = {"name": name, "symbol": symbol, "asof": asof}
    stages: List[Dict[str, Any]] = []

    def ask(role: str, user: str, json_mode: bool = True) -> str:
        s = str(roles.get(role, ""))
        for k, v in sub.items():
            s = s.replace("{" + k + "}", str(v))
        return chat(cfg, s, user, json_mode=json_mode)

    def stage(step: str, role: str, text: str, rnd: int = 1) -> None:
        stages.append({"step": step, "role": role, "content": text, "round": rnd})

    # 1. 分析师团队
    news_view = ask("news_analyst", evidence, json_mode=False)
    tech_view = ask("tech_analyst", evidence, json_mode=False)
    stage("分析师", "news_analyst", news_view)
    stage("分析师", "tech_analyst", tech_view)
    analyst_brief = f"【新闻分析】\n{news_view}\n\n【技术分析】\n{tech_view}"

    # 2. 研究员辩论：交替发言，带历史
    rounds = max(int(cfg.get("max_debate_rounds", 1) or 1), 1)
    hist: List[str] = []
    bull_arg, bear_arg = "", ""
    for r in range(1, rounds + 1):
        history_txt = "\n".join(hist[-4:]) or "（辩论尚未开始）"
        bull_arg = ask("bull", f"{evidence}\n\n{analyst_brief}\n\n【辩论历史】\n{history_txt}\n\n"
                               f"【空头上一条】\n{bear_arg or '（空头尚未发言，先陈述你的最强论据）'}", json_mode=False)
        hist.append(f"多头：{bull_arg}")
        stage("多空辩论", "bull", bull_arg, r)
        history_txt = "\n".join(hist[-4:])
        bear_arg = ask("bear", f"{evidence}\n\n{analyst_brief}\n\n【辩论历史】\n{history_txt}\n\n"
                               f"【多头上一条】\n{bull_arg}", json_mode=False)
        hist.append(f"空头：{bear_arg}")
        stage("多空辩论", "bear", bear_arg, r)

    # 3. 研究主管：五档评级 + 战略动作
    plan = _json(ask("research_manager",
                     f"{evidence}\n\n{analyst_brief}\n\n【辩论历史】\n" + "\n".join(hist)))
    plan["rating"] = _rating(plan.get("rating"))
    stage("研究主管", "research_manager", json.dumps(plan, ensure_ascii=False))

    # 4. 交易员：方案与上限
    trade = _json(ask("trader", f"{evidence}\n\n{analyst_brief}\n\n"
                                f"【投资计划】\n{json.dumps(plan, ensure_ascii=False)}"))
    trade["rating"] = _rating(trade.get("rating") or plan["rating"])
    stage("交易员", "trader", json.dumps(trade, ensure_ascii=False))

    # 5. 风控三方辩论：逐条回应，轮数可配
    risk: Dict[str, Any] = {}
    risk_of = {"risk_aggressive": "激进", "risk_neutral": "中立", "risk_conservative": "保守"}
    risk_hist: List[str] = []
    r_rounds = max(int(cfg.get("risk_rounds", 1) or 1), 1)
    for r in range(1, r_rounds + 1):
        for role, label in risk_of.items():
            others = {k: v for k, v in risk.items() if k != label and v}
            other_txt = "\n".join(f"【{k}上一条】{json.dumps(v, ensure_ascii=False)}"
                                   for k, v in others.items()) or "（其他两方尚未发言，先陈述你的立场）"
            hist_txt = "\n".join(risk_hist[-6:]) or "（辩论尚未开始）"
            res = _json(ask(role, f"{evidence}\n\n【交易员方案】\n{json.dumps(trade, ensure_ascii=False)}\n\n"
                                  f"【风控辩论历史】\n{hist_txt}\n\n{other_txt}"))
            risk[label] = res
            risk_hist.append(f"{label}：{json.dumps(res, ensure_ascii=False)}")
            stage("风控", role, json.dumps(res, ensure_ascii=False), r)
    risk_text = "\n".join(f"【{k}风控】{json.dumps(v, ensure_ascii=False)}" for k, v in risk.items())

    # 6. 组合经理终裁
    pm = _json(ask("portfolio_manager",
                   f"{evidence}\n\n【研究主管计划】{json.dumps(plan, ensure_ascii=False)}\n"
                   f"【交易员方案】{json.dumps(trade, ensure_ascii=False)}\n{risk_text}"))
    stage("组合经理", "portfolio_manager", json.dumps(pm, ensure_ascii=False))
    rating = _rating(pm.get("rating") or trade["rating"])
    conf = _num(pm.get("confidence"), 0.0, 1.0)
    cap = RATING_CAP[rating]
    trade_pos = _num(trade.get("position_pct"), 0.0, 10.0)
    pm_pos = _num(pm.get("final_position_pct"), 0.0, 10.0)
    # 三方取低值，再受评级上限与信心折减约束
    pos = round(min(trade_pos or pm_pos, pm_pos or trade_pos, cap) * min(1.0, 0.4 + conf), 2)
    verdict = RATING_VERDICT[rating]
    decision = {"Buy": "批准", "Overweight": "批准", "Hold": "部分批准",
                "Underweight": "否决", "Sell": "否决"}[rating]

    payload = {
        "ok": True, "symbol": symbol, "name": name, "asof": asof,
        "model": cfg.get("model", "deepseek-chat"),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "pipeline": cfg.get("pipeline", "full"),
        "stages": stages,
        "plan": plan, "trade": trade, "risk": risk, "judge": pm,
        "bull": {"stance": "多头", "points": [bull_arg]},
        "bear": {"stance": "空头", "points": [bear_arg]},
        "rating": rating, "decision": decision, "verdict": verdict,
        "confidence": round(conf, 3), "position_pct": pos,
        "prompt_roles": list(roles.keys()),
        "evidence": evidence,
    }
    if record:
        conn.execute(
            "INSERT INTO debate_runs(symbol, asof, created_at, payload) VALUES(?,?,?,?)",
            (symbol, asof, payload["generated_at"], json.dumps(payload, ensure_ascii=False)))
        conn.commit()
        payload["run_id"] = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    return payload


def latest(conn: sqlite3.Connection, symbol: Optional[str] = None) -> Optional[Dict[str, Any]]:
    if symbol:
        row = conn.execute("SELECT payload FROM debate_runs WHERE symbol=? ORDER BY id DESC LIMIT 1",
                           (symbol,)).fetchone()
    else:
        row = conn.execute("SELECT payload FROM debate_runs ORDER BY id DESC LIMIT 1").fetchone()
    return json.loads(row[0]) if row else None


def history(conn: sqlite3.Connection, symbol: Optional[str] = None, limit: int = 10) -> List[Dict[str, Any]]:
    if symbol:
        rows = conn.execute("SELECT id, symbol, asof, created_at FROM debate_runs "
                            "WHERE symbol=? ORDER BY id DESC LIMIT ?", (symbol, limit)).fetchall()
    else:
        rows = conn.execute("SELECT id, symbol, asof, created_at FROM debate_runs "
                            "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]
