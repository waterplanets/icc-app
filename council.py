"""投委会：把 8 位大师人格的规则评估 + 多轮审议聚合成本地决议。

复用 `.agents/skills/invest-councilskill` 的三段引擎：
  personas.evaluate_all  → 每位大师按自己的 YAML 权重给分
  council.deliberate     → 偏离共识过远的观点向共识修正一轮
  council.aggregate_symbol → 按「权重 × 信心」聚合，输出分歧度与多空名单

区别在于证据：技能原本吃的是行情 + 财务 + 新闻一整套特征，这里只喂本库能算出来的
因子（动量/波动/RSI/趋势/回撤/量比）加上 ml/ 模型给出的新闻情绪。缺的因子会让
覆盖度下降，个人分数按覆盖率向 0 收缩——这是引擎自带的行为，不是本模块补的。
"""
from __future__ import annotations

import json
import math
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

BASE_DIR = Path(__file__).resolve().parent
SKILL_SCRIPTS = BASE_DIR.parent / ".agents" / "skills" / "invest-councilskill" / "scripts"

POSITION_CAP = {          # 决议分数 → 建议仓位上限（占总资产）
    "strong_buy": 0.10,
    "buy": 0.06,
    "watch": 0.03,
    "neutral": 0.0,
    "reduce": 0.0,
    "avoid": 0.0,
    "short": 0.0,
}


def _engine():
    """延迟导入技能包；缺失时返回 None，页面降级为提示。"""
    if str(SKILL_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SKILL_SCRIPTS))
    try:
        from icc import config as C
        from icc import council, features, personas
        return {"config": C, "council": council, "features": features, "personas": personas}
    except Exception:
        return None


def _rsi(closes: List[float], n: int = 14) -> Optional[float]:
    if len(closes) < n + 1:
        return None
    gains, losses = 0.0, 0.0
    for i in range(-n, 0):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    if losses == 0:
        return 100.0
    rs = (gains / n) / (losses / n)
    return 100 - 100 / (1 + rs)


def _features(conn: sqlite3.Connection, symbol: str, asof: str,
              news: Dict[str, Any]) -> Dict[str, Any]:
    rows = db_bars(conn, symbol, asof, 260)
    if len(rows) < 30:
        return {}
    closes = [float(r["close"]) for r in rows if r["close"]]
    vols = [float(r["volume"] or 0) for r in rows]
    if len(closes) < 30:
        return {}

    def ret(k: int) -> Optional[float]:
        return closes[-1] / closes[-1 - k] - 1 if len(closes) > k and closes[-1 - k] else None

    rets = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes)) if closes[i - 1]]
    vol20 = (sum((r - sum(rets[-20:]) / 20) ** 2 for r in rets[-20:]) / 20) ** 0.5 * math.sqrt(252) \
        if len(rets) >= 20 else None

    peak = closes[-1]
    max_dd = 0.0
    for c in reversed(closes[-252:]):
        peak = max(peak, c)
        max_dd = min(max_dd, c / peak - 1)

    ma = lambda k: sum(closes[-k:]) / k if len(closes) >= k else None
    ma50, ma200 = ma(50), ma(200)
    avg_vol = sum(vols[-20:]) / 20 if len(vols) >= 20 else None

    raw = {
        "mom_20": ret(20), "mom_60": ret(60), "mom_120": ret(120) if len(closes) > 120 else None,
        "vol_20": vol20,
        "rsi14": _rsi(closes),
        "trend": (ma50 / ma200 - 1) if ma50 and ma200 and ma200 else None,
        "dist_52w_high": closes[-1] / max(closes[-250:]) - 1 if closes else None,
        "max_dd_252": max_dd,
        "volume_ratio": (vols[-1] / avg_vol) if avg_vol else None,
        "news_sent": news.get("score"),
        "news_intensity": news.get("intensity"),
    }
    return {k: v for k, v in raw.items() if isinstance(v, (int, float))}


def db_bars(conn: sqlite3.Connection, symbol: str, asof: str, n: int) -> List[Any]:
    return conn.execute(
        "SELECT date, close, volume FROM bars WHERE symbol=? AND date<=? "
        "ORDER BY date DESC LIMIT ?", (symbol, asof, n)).fetchall()[::-1]


def _news_map(conn: sqlite3.Connection, asof: str) -> Dict[str, Dict[str, Any]]:
    rows = conn.execute(
        "SELECT symbol, n, avg_score FROM sentiment_daily WHERE date<=? "
        "ORDER BY date DESC", (asof,)).fetchall()
    per: Dict[str, List[Any]] = {}
    for r in rows:
        per.setdefault(r["symbol"], []).append(r)
    out = {}
    for sym, items in per.items():
        items = items[:3]
        n = sum(i["n"] for i in items)
        if not n:
            continue
        out[sym] = {
            "score": sum(i["avg_score"] * i["n"] for i in items) / n,
            "count": n,
            "intensity": min(n / 10.0, 1.0),
        }
    return out


def run(conn: sqlite3.Connection, asof: str, symbols: Optional[List[str]] = None,
        record: bool = True) -> Dict[str, Any]:
    eng = _engine()
    if eng is None:
        return {"ok": False, "error": "投委会引擎不可用（缺少 invest-councilskill）", "items": []}

    personas_mod, features_mod, council_mod = eng["personas"], eng["features"], eng["council"]
    cfg = eng["config"].load_config()

    if symbols is None:
        symbols = [r["symbol"] for r in conn.execute(
            "SELECT symbol FROM instruments WHERE is_watch=1 ORDER BY symbol").fetchall()]
    names = {r["symbol"]: r["name"] for r in conn.execute(
        "SELECT symbol, name FROM instruments").fetchall()}
    sectors = {r["symbol"]: r["group_name"] for r in conn.execute(
        "SELECT symbol, group_name FROM instruments").fetchall()}
    news = _news_map(conn, asof)

    ps = personas_mod.load_personas(cfg)
    universe: Dict[str, Dict[str, Any]] = {}
    raw_feats: Dict[str, Dict[str, Any]] = {}
    for sym in symbols:
        nw = news.get(sym, {})
        feats = _features(conn, sym, asof, nw)
        if not feats:
            continue
        norm = {k: features_mod.normalize(k, v) for k, v in feats.items()}
        feats["_norm"] = {k: v for k, v in norm.items() if v is not None}
        raw_feats[sym] = feats
        universe[sym] = {"name": names.get(sym, sym), "sector": sectors.get(sym, "自选"),
                         "features": feats, "market": None}

    if not universe:
        return {"ok": False, "error": "没有可评估的标的（行情或因子不足）", "items": []}

    verdicts = personas_mod.evaluate_all(ps, universe)
    before = {v.symbol: [] for v in verdicts}
    for v in verdicts:
        before[v.symbol].append({
            "persona": v.persona_id, "name": v.name, "score": round(v.score, 1),
            "confidence": round(v.confidence, 2), "stance": v.stance,
        })

    rounds = int(cfg.get("council", {}).get("deliberation_rounds", 2) or 2)
    convergence = float(cfg.get("council", {}).get("convergence", 0.3) or 0.3)
    log = council_mod.deliberate(verdicts, rounds=rounds, convergence=convergence)

    by_sym: Dict[str, List[Any]] = {}
    for v in verdicts:
        by_sym.setdefault(v.symbol, []).append(v)

    items = []
    for sym, vs in by_sym.items():
        disp_raw = council_mod.dispersion_of(vs, cfg)
        agg = council_mod.aggregate_symbol(sym, vs, cfg, dispersion_raw=disp_raw)
        cap = POSITION_CAP.get(agg.get("stance_code", "neutral"), 0.0)
        # 分歧越大仓位越保守：分歧度是 0–100 量纲的加权标准差
        cap = round(cap * max(0.3, 1 - min(agg.get("dispersion", 0.0) / 60.0, 0.7)), 4)
        items.append({
            "symbol": sym, "name": names.get(sym, sym), "sector": sectors.get(sym, "自选"),
            "score": round(float(agg.get("score", 0.0)), 1),
            "confidence": round(float(agg.get("confidence", 0.0)), 3),
            "dispersion": round(float(agg.get("dispersion", 0.0)), 3),
            "stance": agg.get("stance"), "stance_code": agg.get("stance_code"),
            "dissent": bool(agg.get("dissent")),
            "bulls": agg.get("bulls", []), "bears": agg.get("bears", []),
            "ignored": agg.get("ignored", []),
            "suggest_weight": cap,
            "features": {k: v for k, v in raw_feats[sym].items() if k != "_norm"},
            "round1": before.get(sym, []),
            "round2": [{"persona": v.persona_id, "score": round(v.score, 1),
                        "confidence": round(v.confidence, 2)} for v in vs],
        })
    items.sort(key=lambda x: -x["score"])

    payload = {
        "ok": True, "asof": asof, "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "rounds": rounds, "convergence": convergence,
        "deliberation": log.get("log", [])[:12],
        "items": items,
    }
    if record:
        conn.execute(
            "INSERT INTO council_runs(asof, created_at, payload) VALUES(?,?,?)",
            (asof, payload["generated_at"], json.dumps(payload, ensure_ascii=False)))
        conn.commit()
    return payload


def history(conn: sqlite3.Connection, limit: int = 10) -> List[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, asof, created_at FROM council_runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]
