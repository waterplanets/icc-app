"""LLM 辩论：多头 / 空头 / 主席，三轮调用，产出可留档的决议。

和 5.3 的规则投委会并行存在，区别只有一个：
- 规则投委会：8 位人格各按 YAML 权重打分，输入输出都确定；
- 这里：把同一份证据交给 LLM，多头和空头各自找论据，主席裁决。

提示词默认内置在 `DEFAULT_CONFIG`，`config/debate_prompt.yaml` 存在时按字段覆盖默认值；
改文件即换风格，不用改代码，缺文件也能直接跑。
API Key 依次从环境变量 DEEPSEEK_API_KEY、~/.dsh/.credentials.yaml 读取。
"""
from __future__ import annotations

import copy
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


DEFAULT_CONFIG: Dict[str, Any] = {
    "pipeline": "full",
    "model": "deepseek-chat",
    "temperature": 0.6,
    "max_tokens": 800,
    "max_debate_rounds": 1,
    "risk_rounds": 1,
    "max_news": 8,
    "max_chars_per_news": 110,
    "language": "中文",
    "roles": {
        "news_analyst": (
            "你是新闻分析员。依据给定新闻（含本地 SVM 情感标签与置信度），判断这些新闻\n"
            "对标的的短期影响，并指出哪些标签可能错了。不要复述标题，不要调用任何外部工具。\n"
            "输出 150 字以内，分「支持看多 / 支持看空 / 存疑」三类。"
        ),
        "tech_analyst": (
            "你是技术分析员。依据动量、波动率、RSI、均线趋势、距高点与最大回撤，\n"
            "描述当前所处的位置（趋势中/震荡/超买超卖），并说明这些指标在什么情况下会失效。\n"
            "输出 150 字以内，不预测具体价格，不调用外部工具。"
        ),
        "bull": (
            "你是多头研究员，为 {name}（{symbol}）建立有证据的看多论证。\n"
            "重点：\n"
            "- 成长与需求：行业机会、需求端变化、可持续性\n"
            "- 竞争优势：产品、品牌、渠道、定价权\n"
            "- 积极指标：财务健康、行业趋势、近期正面新闻\n"
            "- 反驳空头：逐条回应空头上一条发言，用具体证据说明为什么多头逻辑更站得住\n"
            "- 对话感：直接与空头辩论，不要只罗列数据\n"
            "可用材料：证据、两位分析师报告、辩论历史、空头上一条发言。\n"
            "只引用证据里的事实；论据不足时直说，不要编。输出 200 字以内。"
        ),
        "bear": (
            "你是空头研究员，为 {name}（{symbol}）建立反对买入的论证。\n"
            "重点：\n"
            "- 风险与挑战：需求饱和、财务不稳、宏观压力\n"
            "- 竞争劣势：份额下滑、创新放缓、对手威胁\n"
            "- 负面指标：财务数据、趋势走弱、近期负面新闻\n"
            "- 反驳多头：逐条回应多头上一条发言，指出过度乐观的假设\n"
            "- 对话感：直接与多头辩论，不要只罗列事实\n"
            "可用材料：证据、两位分析师报告、辩论历史、多头上一条发言。\n"
            "只引用证据里的事实；找不到像样的空头理由时直说。输出 200 字以内。"
        ),
        "research_manager": (
            "你是研究主管兼辩论主持人。你的任务不是复述双方，而是判断哪一方的论据更强，\n"
            "给交易员一个明确的投资计划。不要调用外部工具。\n\n"
            "评级必须从五档中选一个：\n"
            "- Buy：强烈认同多头，建议建仓或加仓\n"
            "- Overweight：偏乐观，建议逐步增加敞口\n"
            "- Hold：维持现有仓位，不动\n"
            "- Underweight：偏谨慎，建议减仓\n"
            "- Sell：强烈认同空头，建议清仓或回避\n\n"
            "辩论一定存在分歧，判断哪边更强就是你的工作，分歧本身不构成 Hold 的理由。\n"
            "请按论据强弱表态，按赢的幅度定仓位；只有在权衡后证据仍然均衡、或证据太薄时\n"
            "才选 Hold。不要为了显得果断而硬造方向，也不要为了安全而默认 Hold。\n"
            "评判只看论据本身，与谁先发言无关。\n\n"
            "输出严格 JSON：\n"
            '{"rating":"Buy|Overweight|Hold|Underweight|Sell","confidence":0.0,\n'
            ' "rationale":"不超过90字，哪几条论据决定了结论",\n'
            ' "strategic_actions":"不超过60字，给交易员的具体步骤"}'
        ),
        "trader": (
            "你是交易员。把投资计划变成可执行方案，必须给出仓位与失效条件，不要调用外部工具。\n"
            "仓位以总资产百分比计，0-10，且不得超过评级对应的上限\n"
            "（Buy 10、Overweight 6、Hold 2、Underweight 0、Sell 0）。\n"
            "不得预测价格。\n"
            "输出严格 JSON：\n"
            '{"rating":"Buy|Overweight|Hold|Underweight|Sell","position_pct":0,\n'
            ' "trigger":"不超过40字，什么条件下执行",\n'
            ' "invalid_if":"不超过40字，什么情况下方案失效",\n'
            ' "reason":"不超过60字"}'
        ),
        "risk_aggressive": (
            "你是激进型风控。目标是别错过机会：强调高回报与竞争优势，评估交易员方案是否过于保守、\n"
            "仓位可以加到多少、最坏情况亏多少。必须逐条回应保守型和中立型上一条发言中\n"
            "最有力的点，指出它们的谨慎在哪里会错过机会、哪个假设过于保守。\n"
            "不要调用外部工具。输出严格 JSON：\n"
            '{"stance":"加仓|维持|反对","points":["不超过45字","..."],"confidence":0.0}'
        ),
        "risk_neutral": (
            "你是中立型风控。目标是平衡：同时评估上行与下行，指出激进型过于乐观之处、\n"
            "保守型过于保守之处，只保留有量化依据的部分，给出可调整的中间方案。\n"
            "必须逐条回应激进型与保守型上一条发言。不要调用外部工具。输出严格 JSON：\n"
            '{"stance":"加仓|维持|反对","points":["不超过45字","..."],"confidence":0.0}'
        ),
        "risk_conservative": (
            "你是保守型风控。目标是保住本金：评估最大回撤、流动性、证据缺口，\n"
            "指出激进型与中立型忽略了哪些威胁，并给出你愿意接受的仓位上限。\n"
            "必须逐条回应激进型与中立型上一条发言。不要调用外部工具。输出严格 JSON：\n"
            '{"stance":"加仓|维持|反对","points":["不超过45字","..."],"confidence":0.0}'
        ),
        "portfolio_manager": (
            "你是组合经理，有最终决定权。综合研究主管的计划、交易员的方案与三位风控的辩论，\n"
            "给出决议。不要调用外部工具。\n\n"
            "评级必须从五档中选一个：\n"
            "- Buy：强烈建仓或加仓\n"
            "- Overweight：偏乐观，逐步增加敞口\n"
            "- Hold：维持现状\n"
            "- Underweight：减仓，落袋部分利润\n"
            "- Sell：清仓或回避\n\n"
            "风控辩论一定存在分歧，判断哪边更强就是你的工作，分歧本身不构成 Hold 的理由。\n"
            "请押注更强的一方，按赢的幅度定仓位；只有在权衡后证据仍然均衡时才选 Hold。\n"
            "不要为了显得果断而硬造方向。评判只看论据本身，与发言顺序无关。\n\n"
            "输出严格 JSON：\n"
            '{"rating":"Buy|Overweight|Hold|Underweight|Sell",\n'
            ' "final_position_pct":0,"confidence":0.0,\n'
            ' "executive_summary":"不超过80字，结论与怎么执行",\n'
            ' "investment_thesis":"不超过80字，哪条证据决定了它，什么会推翻它",\n'
            ' "keep_dissent":"不超过50字，保留你不同意的一方中最值得听的那条",\n'
            ' "assumptions":["不超过30字","..."]}'
        )
    },
    "evidence_header": (
        "=== 标的 ===\n"
        "{name}（{symbol}），虚拟当前日期 {asof}\n"
        "{quote}\n\n"
        "=== 技术因子（截至该日）===\n"
        "{factors}\n\n"
        "=== 规则投委会的初步结论（8 位大师按规则打分，非 LLM）===\n"
        "{council}\n\n"
        "=== 最近新闻（标签由本地 SVM 模型给出，可能出错）===\n"
        "{news}\n\n"
        "=== 本标的的历史决议（供对照，避免自相矛盾）===\n"
        "{memory}\n\n"
        "=== 约束 ===\n"
        "{limits}"
    ),
    "limits": (
        "1. 只能使用上面的信息，不得引用未给出的数据；\n"
        "2. 不得给出价格预测或收益承诺；\n"
        "3. 证据不足时，允许结论为「Hold」，并说明缺什么；\n"
        "4. 不要调用任何外部工具，不要假设自己能查行情或财报。"
    )
}


def _merge_config(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if key == "roles" and isinstance(value, dict):
            merged.setdefault("roles", {}).update(value)
        elif value is not None:
            merged[key] = value
    return merged


def load_config(path: Path = CONFIG_PATH) -> Dict[str, Any]:
    """优先读 YAML；文件缺失时用内置默认值，保证新克隆的仓库也能跑。"""
    p = Path(path)
    if not p.exists():
        return copy.deepcopy(DEFAULT_CONFIG)
    text = p.read_text(encoding="utf-8")
    try:
        import yaml
        cfg = yaml.safe_load(text) or {}
    except Exception:
        # 没有 pyyaml 时退回极简解析：只支持本文件用到的两层结构
        cfg = _mini_yaml(text)
    return _merge_config(DEFAULT_CONFIG, cfg)


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
