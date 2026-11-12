"""新闻模块：抓取东财资讯 → 用本地 SVM 模型打情感标签 → 入库与聚合。

与 ml/ 的分工：
- ml/ 负责训练（LLM 教师标注 + TF-IDF/SVM），产物是 ml/model/model.joblib；
- 本模块只做推理与存储，模型缺失时降级为「只存新闻、不打分」，不阻塞应用启动。

打分走 ml/cli.py 的 score_texts()，与训练时的特征管线完全一致。
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

BASE_DIR = Path(__file__).resolve().parent
ML_DIR = BASE_DIR.parent / "ml"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

MACRO_KEYWORDS = ("A股 收盘", "央行 降准 降息")
CONF_REVIEW = 0.60          # 低于此置信度进人工复核队列
SIGNAL_MIN_N = 2            # 少于几条新闻不出信号


class NewsError(RuntimeError):
    pass


def _http(url: str, referer: str = "https://so.eastmoney.com/", timeout: float = 12.0) -> str:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Referer": referer,
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Encoding": "identity", "Connection": "close",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _strip(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s or "")
    s = s.replace("&nbsp;", " ").replace("&amp;", "&").replace("&quot;", '"').replace("&#39;", "'")
    return re.sub(r"\s+", " ", s).strip()


def fetch(keyword: str, limit: int = 10, sort: str = "time") -> List[Dict[str, Any]]:
    """东财资讯搜索：与 ml/collect_news.py 同一接口。"""
    param = {
        "uid": "", "keyword": keyword, "type": ["cmsArticleWebOld"],
        "client": "web", "clientType": "web", "clientVersion": "curr",
        "param": {"cmsArticleWebOld": {"searchScope": "default", "sort": sort,
                                       "pageIndex": 1, "pageSize": int(limit)}},
    }
    q = urllib.parse.quote(json.dumps(param, ensure_ascii=False, separators=(",", ":")))
    text = _http(f"https://search-api-web.eastmoney.com/search/jsonp?cb=x&param={q}")
    m = re.search(r"x\((.*)\)\s*$", text, re.S)
    if not m:
        raise NewsError("资讯接口返回格式异常")
    data = json.loads(m.group(1))
    out = []
    for it in ((data.get("result") or {}).get("cmsArticleWebOld")) or []:
        title = _strip(it.get("title"))
        if not title:
            continue
        out.append({
            "id": hashlib.md5((it.get("url") or title).encode("utf-8")).hexdigest()[:12],
            "date": (it.get("date") or "")[:10],
            "time": it.get("date") or "",
            "title": title,
            "content": _strip(it.get("content")),
            "media": it.get("mediaName") or "东方财富",
            "url": it.get("url") or "",
        })
    return out


def _scorer():
    """延迟导入 ml 里的打分函数；模型不存在时返回 None（应用仍能启动）。"""
    if str(ML_DIR) not in sys.path:
        sys.path.insert(0, str(ML_DIR))
    try:
        from cli import score_texts  # type: ignore
        return score_texts
    except Exception:
        return None


def sync(conn, symbols: List[Tuple[str, str]], per_symbol: int = 8,
         include_macro: bool = True) -> Dict[str, Any]:
    """抓取并入库。symbols: [(symbol, name)]。返回统计。"""
    score_texts = _scorer()
    targets: List[Tuple[str, str]] = [(s, n) for s, n in symbols]
    if include_macro:
        targets += [(None, kw) for kw in MACRO_KEYWORDS]

    fetched: List[Dict[str, Any]] = []
    errors: List[str] = []
    for sym, name in targets:
        try:
            rows = fetch(name, limit=per_symbol)
        except Exception as e:  # 单只失败不影响其它
            errors.append(f"{name}: {type(e).__name__}")
            continue
        for r in rows:
            # 相关性过滤：按个股名称检索时，结果里会混入无关新闻，要求正文或标题提到该名称
            if sym and name not in r["title"] and name not in r["content"]:
                continue
            r["symbol"] = sym
            fetched.append(r)

    # 去重（同一 url 可能被多个关键词命中），保留已有 symbol 的那条
    uniq: Dict[str, Dict[str, Any]] = {}
    for r in fetched:
        if r["id"] not in uniq or (uniq[r["id"]]["symbol"] is None and r["symbol"]):
            uniq[r["id"]] = r
    rows = list(uniq.values())

    scored = 0
    if score_texts and rows:
        try:
            res = score_texts([{"title": r["title"], "content": r["content"]} for r in rows])
            for r, s in zip(rows, res):
                r.update({"label": s["label"], "score": s["score"],
                          "confidence": s["confidence"]})
            scored = len(res)
        except Exception as e:
            errors.append(f"打分失败: {type(e).__name__}: {e}")

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for r in rows:
        conn.execute(
            """INSERT INTO news(id,date,time,symbol,title,content,media,url,label,score,confidence,fetched_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET symbol=COALESCE(excluded.symbol,news.symbol),
                   label=excluded.label, score=excluded.score, confidence=excluded.confidence""",
            (r["id"], r["date"], r.get("time"), r.get("symbol"), r["title"], r.get("content"),
             r.get("media"), r.get("url"), r.get("label"), r.get("score"), r.get("confidence"), now))
    conn.commit()

    pairs = {(r["date"], r["symbol"]) for r in rows if r.get("symbol") and r.get("label") is not None}
    for d, s in pairs:
        rebuild_daily(conn, d, s)

    return {
        "fetched": len(fetched), "kept": len(rows), "scored": scored,
        "symbols": len(targets), "errors": errors[:5],
        "model": bool(score_texts),
    }


def rebuild_daily(conn, date: str, symbol: str) -> None:
    rows = conn.execute(
        "SELECT label, score, confidence FROM news WHERE date=? AND symbol=? AND label IS NOT NULL",
        (date, symbol)).fetchall()
    if not rows:
        return
    labels = [int(r["label"]) for r in rows]
    scores = [float(r["score"]) if r["score"] is not None else 0.0 for r in rows]
    confs = [float(r["confidence"] or 0) for r in rows]
    # 置信度加权：低置信新闻对当日情绪的贡献按比例缩小
    w = [max(c, 0.05) for c in confs]
    avg = sum(s * wi for s, wi in zip(scores, w)) / sum(w)
    conn.execute(
        """INSERT INTO sentiment_daily(date,symbol,n,pos,neu,neg,avg_score,avg_conf)
           VALUES(?,?,?,?,?,?,?,?)
           ON CONFLICT(date,symbol) DO UPDATE SET n=excluded.n,pos=excluded.pos,neu=excluded.neu,
               neg=excluded.neg,avg_score=excluded.avg_score,avg_conf=excluded.avg_conf""",
        (date, symbol, len(labels), labels.count(1), labels.count(0), labels.count(-1),
         round(avg, 4), round(sum(confs) / len(confs), 4)))
    conn.commit()


def news_list(conn, asof: str, symbol: Optional[str] = None, limit: int = 80,
              only_scored: bool = False) -> List[Dict[str, Any]]:
    sql = "SELECT * FROM news WHERE date<=?"
    args: List[Any] = [asof]
    if symbol:
        sql += " AND symbol=?"
        args.append(symbol)
    if only_scored:
        sql += " AND label IS NOT NULL"
    sql += " ORDER BY date DESC, time DESC LIMIT ?"
    args.append(limit)
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def daily_rows(conn, asof: str, symbol: Optional[str] = None, days: int = 15) -> List[Dict[str, Any]]:
    sql = "SELECT * FROM sentiment_daily WHERE date<=?"
    args: List[Any] = [asof]
    if symbol:
        sql += " AND symbol=?"
        args.append(symbol)
    sql += " ORDER BY date DESC, symbol LIMIT ?"
    args.append(days * (1 if symbol else 30))
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def signals(conn, asof: str, lookback: int = 3) -> List[Dict[str, Any]]:
    """按标聚合最近若干天的新闻情绪，产出「倾向 + 是否需要人工复核」。"""
    rows = conn.execute(
        "SELECT symbol, date, n, avg_score, avg_conf FROM sentiment_daily "
        "WHERE date<=? ORDER BY date DESC", (asof,)).fetchall()
    names = {r["symbol"]: r["name"] for r in
             conn.execute("SELECT symbol, name FROM instruments").fetchall()}

    per: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        per.setdefault(r["symbol"], []).append(dict(r))

    out = []
    for sym, items in per.items():
        items = items[:lookback]
        n = sum(i["n"] for i in items)
        if n == 0:
            continue
        score = sum(i["avg_score"] * i["n"] for i in items) / n
        conf = sum(i["avg_conf"] * i["n"] for i in items) / n
        if n < SIGNAL_MIN_N:
            action, why = "观望", f"样本仅 {n} 条"
        elif conf < CONF_REVIEW:
            action, why = "需人工复核", f"平均置信 {conf:.2f}"
        elif score >= 0.15:
            action, why = "偏多", f"情绪 {score:+.2f}"
        elif score <= -0.15:
            action, why = "偏空", f"情绪 {score:+.2f}"
        else:
            action, why = "中性", f"情绪 {score:+.2f}"
        out.append({"symbol": sym, "name": names.get(sym, sym), "n": n,
                    "score": round(score, 4), "conf": round(conf, 4),
                    "action": action, "why": why,
                    "date": items[0]["date"] if items else None})
    out.sort(key=lambda x: x["score"])
    return out


def stats(conn) -> Dict[str, Any]:
    row = conn.execute(
        "SELECT COUNT(*) n, SUM(label IS NULL) unscored, MIN(date), MAX(date), MAX(fetched_at) FROM news"
    ).fetchone()
    return {"n": row[0] or 0, "unscored": row[1] or 0,
            "first": row[2], "last": row[3], "fetched_at": row[4]}
