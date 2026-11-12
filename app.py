"""ICC 本地工作台：FastAPI + Jinja2 服务端渲染，零前端 JavaScript。

核心机制：
- 多账户：顶部可切换账户，每个账户独立现金/持仓/成交/净值。
- 时间指针（asof）：每个账户有自己的「虚拟当前日期」。asof=今天为实时模式（用快照价），
  否则为回放模式（只读 asof 及之前的日线，成交也记在 asof 当天）。

启动：
    python app.py                 # http://127.0.0.1:8770
    python app.py --sync          # 只跑一次全量同步后退出
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from typing import Any, Dict, Iterator, List, Optional

import uvicorn
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import broker
import council
import db
import debate
import feed
import newsfeed
import sync
import agent_tools

BASE_DIR = db.BASE_DIR
app = FastAPI(title="ICC 本地工作台", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


# --------------------------------------------------------------------------- #
# 模板过滤器
# --------------------------------------------------------------------------- #
def _money(v: Optional[float], digits: int = 2) -> str:
    if v is None:
        return "—"
    return f"{v:,.{digits}f}"


def _pct(v: Optional[float]) -> str:
    if v is None:
        return "—"
    return f"{v:+.2f}%"


def _cls(v: Optional[float]) -> str:
    if v is None:
        return "flat"
    return "up" if v > 0 else ("down" if v < 0 else "flat")


templates.env.filters["money"] = _money
templates.env.filters["pct"] = _pct
templates.env.filters["sign_cls"] = _cls


# --------------------------------------------------------------------------- #
# 请求上下文
# --------------------------------------------------------------------------- #
def get_conn() -> Iterator[sqlite3.Connection]:
    conn = db.connect()
    try:
        yield conn
    finally:
        conn.close()


def get_ctx(request: Request, conn: sqlite3.Connection = Depends(get_conn)) -> Dict[str, Any]:
    aid: Optional[int] = None
    raw = request.cookies.get("icc_acc")
    if raw:
        try:
            aid = int(raw)
        except ValueError:
            aid = None
    acc = db.account(conn, aid)
    return {"accounts": db.accounts(conn), "acc": acc, "asof": acc["asof"], "live": acc["live"]}


def render(request: Request, name: str, ctx: Dict[str, Any],
           **extra: Any) -> HTMLResponse:
    data: Dict[str, Any] = dict(ctx)
    data.update(extra)
    return templates.TemplateResponse(request, name, data)


def safe_next(value: str) -> str:
    return value if value.startswith("/") else "/"


def index_strip(conn: sqlite3.Connection, asof: str, live: bool) -> List[Dict[str, Any]]:
    out = []
    for key in ("sh", "sz", "cyb", "hs300", "kc50"):
        preset = feed.INDEX_PRESETS[key]
        sym = preset["symbol"]
        q = db.get_quote(conn, sym)
        info = db.close_on(conn, sym, asof)
        close = info["close"] if info else None
        out.append({
            "key": key, "symbol": sym, "name": preset["name"],
            "price": float(q["price"]) if live and q and q["price"] else close,
            "pct": float(q["pct"]) if live and q and q["pct"] is not None else (info["pct"] if info else None),
            "date": info["date"] if info else None,
        })
    return out


def chart_payload(conn: sqlite3.Connection, account_id: int, asof: str) -> Dict[str, Any]:
    """净值曲线 + 沪深300 归一化对比（服务端计算，SVG 渲染）。"""
    curve = db.equity_curve(conn, account_id, until=asof, limit=400)
    empty = {"points": None, "bench": None, "labels": [], "grid": [], "curve": curve,
             "eq_ret": 0.0, "bench_ret": None, "width": 920, "height": 300,
             "left": 56, "right": 16, "top": 18, "bottom": 32}
    if len(curve) < 2:
        return empty

    d0 = curve[0]["date"]
    eq0 = curve[0]["equity"] or 1.0
    eq_vals = [(c["date"], (c["equity"] / eq0 - 1) * 100) for c in curve]

    bench_rows = conn.execute(
        "SELECT date, close FROM bars WHERE symbol=? AND date>=? AND date<=? ORDER BY date",
        (db.BENCHMARK, d0, asof)).fetchall()
    bench_vals: List[tuple] = []
    if bench_rows:
        b0 = bench_rows[0]["close"] or 1.0
        by_date = {r["date"]: (r["close"] / b0 - 1) * 100 for r in bench_rows}
        bench_vals = [(c["date"], by_date.get(c["date"])) for c in curve]

    all_vals = [v for _, v in eq_vals] + [v for _, v in bench_vals if v is not None]
    lo, hi = min(all_vals), max(all_vals)
    pad = max((hi - lo) * 0.08, 0.5)
    lo, hi = lo - pad, hi + pad

    W, H, L, R, T, B = 920, 300, 56, 16, 18, 32
    dates = [d for d, _ in eq_vals]

    def x(i: int, n: int) -> float:
        return L + (W - L - R) * (i / max(n - 1, 1))

    def y(v: float) -> float:
        return T + (H - T - B) * (1 - (v - lo) / (hi - lo))

    def poly(vals: List[Optional[float]]) -> str:
        parts = []
        for i, v in enumerate(vals):
            if v is None:
                continue
            parts.append(f"{x(i, len(vals)):.1f},{y(v):.1f}")
        return " ".join(parts)

    grid = [{"y": round(y(lo + (hi - lo) * k / 4), 1),
             "label": f"{lo + (hi - lo) * k / 4:+.1f}%"} for k in range(5)]

    valid_bench = [v for _, v in bench_vals if v is not None]
    return {
        "points": poly([v for _, v in eq_vals]),
        "bench": poly([v for _, v in bench_vals]) if bench_vals else None,
        "grid": grid,
        "labels": [dates[0], dates[len(dates) // 2], dates[-1]],
        "width": W, "height": H, "left": L, "right": R, "top": T, "bottom": B,
        "curve": curve, "eq_ret": round(eq_vals[-1][1], 2),
        "bench_ret": round(valid_bench[-1], 2) if valid_bench else None,
    }


def kline_payload(conn: sqlite3.Connection, symbol: str, asof: str,
                  n: int = 60) -> Dict[str, Any]:
    rows = [dict(r) for r in db.bars_until(conn, symbol, asof, n)]
    name = conn.execute("SELECT name FROM instruments WHERE symbol=?", (symbol,)).fetchone()
    payload: Dict[str, Any] = {"symbol": symbol, "name": (name[0] if name else symbol),
                               "asof": asof, "bars": [], "candles": [], "grid": [],
                               "labels": [], "rows": rows}
    if not rows:
        return payload

    W, H, L, R, T, B = 920, 340, 62, 14, 16, 34
    lows = min(float(r["low"]) for r in rows)
    highs = max(float(r["high"]) for r in rows)
    pad = (highs - lows) * 0.06 or 1.0
    lo, hi = lows - pad, highs + pad

    def y(v: float) -> float:
        return T + (H - T - B) * (1 - (v - lo) / (hi - lo))

    step = (W - L - R) / len(rows)
    body_w = max(step * 0.62, 1.2)
    candles = []
    for i, r in enumerate(rows):
        cx = L + step * (i + 0.5)
        o, c = float(r["open"]), float(r["close"])
        candles.append({
            "x": round(cx, 1), "w": round(body_w, 1),
            "wick_top": round(y(float(r["high"])), 1),
            "wick_bottom": round(y(float(r["low"])), 1),
            "body_top": round(y(max(o, c)), 1),
            "body_height": max(round(abs(y(o) - y(c)), 1), 1.2),
            "color": "#d33f3f" if c >= o else "#17945a",
        })

    grid = [{"y": round(y(lo + (hi - lo) * k / 4), 1),
             "label": f"{lo + (hi - lo) * k / 4:.1f}"} for k in range(5)]

    payload.update({
        "candles": candles, "grid": grid, "width": W, "height": H, "left": L, "right": R,
        "top": T, "bottom": B,
        "labels": [rows[0]["date"], rows[len(rows) // 2]["date"], rows[-1]["date"]],
    })
    return payload


# --------------------------------------------------------------------------- #
# 账户与时间
# --------------------------------------------------------------------------- #
@app.post("/accounts/switch")
def switch_account(account_id: str = Form(...), next: str = Form("/")) -> RedirectResponse:
    resp = RedirectResponse(safe_next(next), status_code=303)
    resp.set_cookie("icc_acc", str(int(account_id)), max_age=3600 * 24 * 365, httponly=True)
    return resp


@app.post("/accounts/create")
def create_account(name: str = Form(...), cash: str = Form("1000000"),
                   next: str = Form("/")) -> RedirectResponse:
    conn = db.connect()
    try:
        try:
            amount = max(float(cash), 0.0)
        except ValueError:
            amount = db.INITIAL_CASH
        clean = name.strip() or "新账户"
        try:
            aid = db.create_account(conn, clean, amount)
        except sqlite3.IntegrityError:
            return RedirectResponse("/?msg=账户名已存在", status_code=303)
        resp = RedirectResponse(safe_next(next), status_code=303)
        resp.set_cookie("icc_acc", str(aid), max_age=3600 * 24 * 365, httponly=True)
        return resp
    finally:
        conn.close()


@app.post("/asof")
def move_asof(action: str = Form("set"), date: str = Form(""),
              next: str = Form("/"), conn: sqlite3.Connection = Depends(get_conn),
              ctx: Dict[str, Any] = Depends(get_ctx)) -> RedirectResponse:
    acc = ctx["acc"]
    if action == "today":
        target = db.today_str()
    elif action == "next":
        target = db.next_trading_day(conn, acc["asof"]) or acc["asof"]
    else:
        target = date or acc["asof"]
    db.set_asof(conn, acc["id"], target)
    return RedirectResponse(safe_next(next), status_code=303)


@app.post("/account/reset")
def reset_action(cash: str = Form("1000000"),
                 conn: sqlite3.Connection = Depends(get_conn),
                 ctx: Dict[str, Any] = Depends(get_ctx)) -> RedirectResponse:
    try:
        amount = float(cash) if cash.strip() else db.INITIAL_CASH
    except ValueError:
        amount = db.INITIAL_CASH
    db.reset_account(conn, ctx["acc"]["id"], max(amount, 0.0))
    return RedirectResponse("/positions?msg=账户已重置：持仓/成交/净值清空，时间指针不变", status_code=303)


# --------------------------------------------------------------------------- #
# 页面
# --------------------------------------------------------------------------- #
@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, msg: str = "", conn: sqlite3.Connection = Depends(get_conn),
              ctx: Dict[str, Any] = Depends(get_ctx)) -> HTMLResponse:
    acc = ctx["acc"]
    pf = broker.portfolio(conn, acc["id"])
    rows = db.watchlist_with_quotes(conn, ctx["asof"], ctx["live"])
    last_sync = conn.execute("SELECT MAX(updated_at) FROM quotes").fetchone()[0]
    return render(request, "dashboard.html", ctx, pf=pf, rows=rows,
                  indices=index_strip(conn, ctx["asof"], ctx["live"]),
                  last_sync=last_sync,
                  trades=db.trades(conn, acc["id"], 8, until=ctx["asof"]), page="home", msg=msg)


@app.get("/watchlist", response_class=HTMLResponse)
def watchlist_page(request: Request, msg: str = "",
                   conn: sqlite3.Connection = Depends(get_conn),
                   ctx: Dict[str, Any] = Depends(get_ctx)) -> HTMLResponse:
    rows = db.watchlist_with_quotes(conn, ctx["asof"], ctx["live"])
    groups = sorted({r["group_name"] for r in rows}) or ["自选"]
    return render(request, "watchlist.html", ctx, rows=rows, groups=groups, msg=msg, page="watch")


@app.post("/watchlist/add")
def watchlist_add(symbol: str = Form(...), name: str = Form(""),
                  group_name: str = Form("自选")) -> RedirectResponse:
    conn = db.connect()
    try:
        try:
            sym = feed.normalize_symbol(symbol)
        except feed.FeedError as e:
            return RedirectResponse(f"/watchlist?msg=代码无法识别：{e}", status_code=303)
        fetched: Dict[str, Any] = {}
        try:
            fetched = feed.quotes([sym])[0]
        except Exception:
            pass
        real_name = name.strip() or fetched.get("name") or sym
        db.add_instrument(conn, sym, real_name, group_name.strip() or "自选")
        try:
            data = feed.daily_bars(sym, start="2023-01-01")
            db.upsert_bars(conn, sym, data["rows"])
        except Exception:
            pass
        if fetched.get("price") is not None:
            db.upsert_quotes(conn, [fetched])
        return RedirectResponse(f"/watchlist?msg=已添加 {sym} {real_name}", status_code=303)
    finally:
        conn.close()


@app.post("/watchlist/delete")
def watchlist_delete(symbol: str = Form(...)) -> RedirectResponse:
    conn = db.connect()
    try:
        db.remove_instrument(conn, symbol)
        return RedirectResponse(f"/watchlist?msg=已移除 {symbol}（持仓与日线保留）", status_code=303)
    finally:
        conn.close()


@app.post("/watchlist/group")
def watchlist_group(symbol: str = Form(...), group_name: str = Form(...)) -> RedirectResponse:
    conn = db.connect()
    try:
        db.update_group(conn, symbol, group_name.strip() or "自选")
        return RedirectResponse("/watchlist?msg=分组已更新", status_code=303)
    finally:
        conn.close()


@app.get("/quotes", response_class=HTMLResponse)
def quotes_page(request: Request, msg: str = "",
                conn: sqlite3.Connection = Depends(get_conn),
                ctx: Dict[str, Any] = Depends(get_ctx)) -> HTMLResponse:
    rows = db.watchlist_with_quotes(conn, ctx["asof"], ctx["live"])
    stats = db.bar_stats(conn)
    last_sync = conn.execute("SELECT MAX(updated_at) FROM quotes").fetchone()[0]
    return render(request, "quotes.html", ctx, rows=rows, stats=stats, msg=msg,
                  last_sync=last_sync, page="quotes")


@app.post("/sync")
def sync_all_action() -> RedirectResponse:
    conn = db.connect()
    try:
        res = sync.sync_all(conn)
        bq = res["quotes"]
        msg = (f"同步完成：日线 {res['bars']['ok']}/{res['bars']['total']} 只"
               f"（{res['bars']['rows']} 行），快照 {bq.get('ok', 0)}/{bq.get('total', 0)} 只")
        return RedirectResponse(f"/quotes?msg={msg}", status_code=303)
    finally:
        conn.close()


@app.post("/snapshot")
def snapshot_action() -> RedirectResponse:
    conn = db.connect()
    try:
        res = sync.sync_quotes(conn)
        msg = f"快照已刷新 {res.get('ok', 0)}/{res.get('total', 0)} 只"
        if res.get("error"):
            msg += f"（失败：{res['error']}）"
        return RedirectResponse(f"/quotes?msg={msg}", status_code=303)
    finally:
        conn.close()


@app.get("/positions", response_class=HTMLResponse)
def positions_page(request: Request, msg: str = "",
                   conn: sqlite3.Connection = Depends(get_conn),
                   ctx: Dict[str, Any] = Depends(get_ctx)) -> HTMLResponse:
    pf = broker.portfolio(conn, ctx["acc"]["id"])
    return render(request, "positions.html", ctx, pf=pf, fee=broker.FEE,
                  msg=msg, page="pos")


@app.get("/trade", response_class=HTMLResponse)
def trade_page(request: Request, msg: str = "",
               conn: sqlite3.Connection = Depends(get_conn),
               ctx: Dict[str, Any] = Depends(get_ctx)) -> HTMLResponse:
    pf = broker.portfolio(conn, ctx["acc"]["id"])
    rows = db.watchlist_with_quotes(conn, ctx["asof"], ctx["live"])
    return render(request, "trade.html", ctx, pf=pf, rows=rows, msg=msg,
                  trades=db.trades(conn, ctx["acc"]["id"], 20, until=ctx["asof"]),
                  fee=broker.FEE, page="trade")


@app.post("/trade")
def trade_action(symbol: str = Form(...), side: str = Form(...), qty: str = Form(""),
                 price: str = Form(""), note: str = Form(""),
                 conn: sqlite3.Connection = Depends(get_conn),
                 ctx: Dict[str, Any] = Depends(get_ctx)) -> RedirectResponse:
    acc = ctx["acc"]
    try:
        sym = feed.normalize_symbol(symbol)
    except feed.FeedError as e:
        return RedirectResponse(f"/trade?msg=代码无法识别：{e}", status_code=303)
    row = conn.execute("SELECT name FROM instruments WHERE symbol=?", (sym,)).fetchone()
    name = row[0] if row else sym
    try:
        px: Optional[float] = float(price) if price.strip() else None
    except ValueError:
        px = None
    if px is None:
        px = broker.trade_price(conn, acc, sym)
    if px is None:
        return RedirectResponse(
            f"/trade?msg={sym} 在 {acc['asof']} 没有可用价格，请手填成交价", status_code=303)
    try:
        qty_n = int(float(qty)) if qty.strip() else 0
    except ValueError:
        qty_n = 0
    if qty_n <= 0:
        return RedirectResponse("/trade?msg=数量需为正整数", status_code=303)
    res = broker.buy(conn, acc["id"], sym, name, qty_n, px, note) if side == "buy" \
        else broker.sell(conn, acc["id"], sym, qty_n, px, note)
    if res["status"] == "filled":
        msg = (f"{'买入' if res['side'] == 'buy' else '卖出'} {sym} {res['qty']} 股"
               f" @ {res['price']}（成交日 {res['trade_date']}），费用 {res['fee']} 元")
        if res.get("pnl") is not None:
            msg += f"，本次盈亏 {res['pnl']} 元"
        if res.get("limited"):
            msg += "（受当日成交量限制，仅部分成交）"
        return RedirectResponse(f"/positions?msg={msg}", status_code=303)
    return RedirectResponse(f"/trade?msg=委托被拒：{res['reason']}", status_code=303)


@app.get("/trades", response_class=HTMLResponse)
def trades_page(request: Request, conn: sqlite3.Connection = Depends(get_conn),
                ctx: Dict[str, Any] = Depends(get_ctx)) -> HTMLResponse:
    return render(request, "trades.html", ctx,
                  trades=db.trades(conn, ctx["acc"]["id"], 500, until=ctx["asof"]), page="trades")


@app.get("/equity", response_class=HTMLResponse)
def equity_page(request: Request, conn: sqlite3.Connection = Depends(get_conn),
                ctx: Dict[str, Any] = Depends(get_ctx)) -> HTMLResponse:
    pf = broker.portfolio(conn, ctx["acc"]["id"])
    data = chart_payload(conn, ctx["acc"]["id"], ctx["asof"])
    return render(request, "equity.html", ctx, pf=pf, data=data, page="equity")


@app.get("/kline", response_class=HTMLResponse)
def kline_page(request: Request, symbol: str = "600519.SH", n: int = 60,
               conn: sqlite3.Connection = Depends(get_conn),
               ctx: Dict[str, Any] = Depends(get_ctx)) -> HTMLResponse:
    try:
        sym = feed.normalize_symbol(symbol)
    except feed.FeedError:
        sym = "600519.SH"
    data = kline_payload(conn, sym, ctx["asof"], max(min(n, 250), 10))
    return render(request, "kline.html", ctx, data=data, page="kline")


# --------------------------------------------------------------------------- #
# 新闻情绪（ml/ 训练的 SVM 模型推理）
# --------------------------------------------------------------------------- #
@app.get("/news", response_class=HTMLResponse)
def news_page(request: Request, symbol: str = "", msg: str = "",
              conn: sqlite3.Connection = Depends(get_conn),
              ctx: Dict[str, Any] = Depends(get_ctx)) -> HTMLResponse:
    sym = symbol.strip() or None
    rows = newsfeed.news_list(conn, ctx["asof"], symbol=sym, limit=120)
    sig = newsfeed.signals(conn, ctx["asof"])
    daily = newsfeed.daily_rows(conn, ctx["asof"], symbol=sym, days=12)
    return render(request, "news.html", ctx, rows=rows, signals=sig, daily=daily,
                  symbol=sym, stats=newsfeed.stats(conn), msg=msg, page="news")


@app.post("/news/sync")
def news_sync(per_symbol: str = Form("8"),
              conn: sqlite3.Connection = Depends(get_conn),
              ctx: Dict[str, Any] = Depends(get_ctx)) -> RedirectResponse:
    try:
        n = max(min(int(per_symbol), 20), 1)
    except ValueError:
        n = 8
    targets = [(r["symbol"], r["name"]) for r in conn.execute(
        "SELECT symbol, name FROM instruments WHERE is_watch=1").fetchall()]
    try:
        res = newsfeed.sync(conn, targets, per_symbol=n)
    except Exception as e:
        return RedirectResponse(f"/news?msg=抓取失败：{type(e).__name__}: {e}", status_code=303)
    tail = "" if res["model"] else "（未找到 ml/model/model.joblib，仅抓取未打分）"
    msg = (f"抓取 {res['symbols']} 个检索词，去重后 {res['kept']} 条，"
           f"已打分 {res['scored']} 条{tail}")
    if res["errors"]:
        msg += f"；失败 {len(res['errors'])} 项：{res['errors'][0]}"
    return RedirectResponse(f"/news?msg={msg}", status_code=303)


@app.get("/api/news")
def api_news(symbol: str = "", limit: int = 50,
             conn: sqlite3.Connection = Depends(get_conn),
             ctx: Dict[str, Any] = Depends(get_ctx)) -> Dict[str, Any]:
    rows = newsfeed.news_list(conn, ctx["asof"], symbol=symbol.strip() or None,
                              limit=max(min(limit, 300), 1))
    return {"asof": ctx["asof"], "count": len(rows), "items": rows}


@app.get("/api/sentiment")
def api_sentiment(symbol: str = "", days: int = 15,
                  conn: sqlite3.Connection = Depends(get_conn),
                  ctx: Dict[str, Any] = Depends(get_ctx)) -> Dict[str, Any]:
    rows = newsfeed.daily_rows(conn, ctx["asof"], symbol=symbol.strip() or None,
                               days=max(min(days, 60), 1))
    return {"asof": ctx["asof"], "count": len(rows), "items": rows}


@app.get("/api/signal")
def api_signal(conn: sqlite3.Connection = Depends(get_conn),
               ctx: Dict[str, Any] = Depends(get_ctx)) -> Dict[str, Any]:
    sig = newsfeed.signals(conn, ctx["asof"])
    return {"asof": ctx["asof"], "count": len(sig),
            "review_threshold": newsfeed.CONF_REVIEW, "items": sig}


# --------------------------------------------------------------------------- #
# 投委会：8 位大师人格 + 多轮审议（复用 invest-councilskill 引擎）
# --------------------------------------------------------------------------- #
@app.get("/council", response_class=HTMLResponse)
def council_page(request: Request, msg: str = "", run: int = 0,
                 conn: sqlite3.Connection = Depends(get_conn),
                 ctx: Dict[str, Any] = Depends(get_ctx)) -> HTMLResponse:
    data: Dict[str, Any] = {"ok": False, "items": [], "asof": ctx["asof"]}
    if run:
        row = conn.execute("SELECT payload FROM council_runs WHERE id=?", (run,)).fetchone()
        if row:
            data = json.loads(row[0])
    else:
        last = conn.execute("SELECT payload FROM council_runs ORDER BY id DESC LIMIT 1").fetchone()
        if last:
            data = json.loads(last[0])
    return render(request, "council.html", ctx, data=data, msg=msg,
                  history=council.history(conn), page="council")


@app.post("/council/run")
def council_run(conn: sqlite3.Connection = Depends(get_conn),
                ctx: Dict[str, Any] = Depends(get_ctx)) -> RedirectResponse:
    try:
        res = council.run(conn, ctx["asof"])
    except Exception as e:
        return RedirectResponse(f"/council?msg=审议失败：{type(e).__name__}: {e}", status_code=303)
    if not res.get("ok"):
        return RedirectResponse(f"/council?msg={res.get('error', '未能生成决议')}", status_code=303)
    return RedirectResponse(
        f"/council?msg=已审议 {len(res['items'])} 个标的（虚拟日期 {ctx['asof']}）", status_code=303)


@app.get("/api/council")
def api_council(conn: sqlite3.Connection = Depends(get_conn),
                ctx: Dict[str, Any] = Depends(get_ctx)) -> Dict[str, Any]:
    res = council.run(conn, ctx["asof"], record=False)
    return res


# --------------------------------------------------------------------------- #
# LLM 辩论：多头 / 空头 / 主席，提示词在 config/debate_prompt.yaml
# --------------------------------------------------------------------------- #
@app.get("/debate", response_class=HTMLResponse)
def debate_page(request: Request, symbol: str = "", msg: str = "",
                conn: sqlite3.Connection = Depends(get_conn),
                ctx: Dict[str, Any] = Depends(get_ctx)) -> HTMLResponse:
    sym = symbol.strip()
    data = debate.latest(conn, sym or None)
    symbols = [dict(r) for r in conn.execute(
        "SELECT symbol, name FROM instruments WHERE is_watch=1 ORDER BY group_name, symbol").fetchall()]
    return render(request, "debate.html", ctx, data=data, symbols=symbols,
                  symbol=sym, msg=msg, history=debate.history(conn, sym or None),
                  page="debate")


@app.post("/debate/run")
def debate_run(symbol: str = Form(...),
               conn: sqlite3.Connection = Depends(get_conn),
               ctx: Dict[str, Any] = Depends(get_ctx)) -> RedirectResponse:
    sym = symbol.strip()
    try:
        c = council.run(conn, ctx["asof"], symbols=[sym], record=False)
        res = debate.run(conn, sym, ctx["asof"], council_payload=c)
    except Exception as e:
        return RedirectResponse(f"/debate?symbol={sym}&msg=辩论失败：{type(e).__name__}: {e}",
                                status_code=303)
    return RedirectResponse(
        f"/debate?symbol={sym}&msg=已生成决议：{res['verdict']}（仓位上限 {res['position_pct']}%）",
        status_code=303)


@app.get("/api/debate")
def api_debate(symbol: str = "",
               conn: sqlite3.Connection = Depends(get_conn),
               ctx: Dict[str, Any] = Depends(get_ctx)) -> Dict[str, Any]:
    sym = symbol.strip()
    if not sym:
        raise HTTPException(status_code=400, detail="需要 symbol 参数")
    try:
        c = council.run(conn, ctx["asof"], symbols=[sym], record=False)
        return debate.run(conn, sym, ctx["asof"], council_payload=c)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"{type(e).__name__}: {e}")


# --------------------------------------------------------------------------- #
# Agent 插件：安装自检与用法引导
# --------------------------------------------------------------------------- #
@app.get("/agent", response_class=HTMLResponse)
def agent_page(request: Request, msg: str = "",
               ctx: Dict[str, Any] = Depends(get_ctx)) -> HTMLResponse:
    return render(request, "agent.html", ctx, st=agent_tools.status(),
                  prompts=agent_tools.prompts(ctx["asof"]), test=None, msg=msg, page="agent")


@app.get("/agent/selftest", response_class=HTMLResponse)
def agent_selftest(request: Request,
                   ctx: Dict[str, Any] = Depends(get_ctx)) -> HTMLResponse:
    return render(request, "agent.html", ctx, st=agent_tools.status(),
                  prompts=agent_tools.prompts(ctx["asof"]), test=agent_tools.selftest(),
                  msg="", page="agent")


@app.get("/healthz")
def healthz() -> Dict[str, str]:
    return {"status": "ok"}


# --------------------------------------------------------------------------- #
# 定时任务
# --------------------------------------------------------------------------- #
def job_daily_sync() -> None:
    conn = db.connect()
    try:
        sync.sync_all(conn)
    finally:
        conn.close()


def job_snapshot() -> None:
    conn = db.connect()
    try:
        sync.sync_quotes(conn)
    finally:
        conn.close()


def job_news_sync() -> None:
    conn = db.connect()
    try:
        targets = [(r["symbol"], r["name"]) for r in conn.execute(
            "SELECT symbol, name FROM instruments WHERE is_watch=1").fetchall()]
        newsfeed.sync(conn, targets, per_symbol=8)
    except Exception:
        pass  # 新闻抓取失败不影响行情与虚拟盘
    finally:
        conn.close()


def start_scheduler() -> BackgroundScheduler:
    sched = BackgroundScheduler(timezone="Asia/Shanghai")
    sched.add_job(job_daily_sync, CronTrigger(day_of_week="mon-fri", hour=15, minute=35),
                  id="eod", replace_existing=True)
    sched.add_job(job_snapshot,
                  CronTrigger(day_of_week="mon-fri", hour="9-11,13-14", minute="*/5"),
                  id="snap", replace_existing=True)
    sched.add_job(job_snapshot, CronTrigger(day_of_week="mon-fri", hour=15, minute=0),
                  id="snap_close", replace_existing=True)
    sched.add_job(job_news_sync, CronTrigger(day_of_week="mon-fri", hour=16, minute=5),
                  id="news", replace_existing=True)
    sched.start()
    return sched


def main() -> None:
    parser = argparse.ArgumentParser(description="ICC 本地工作台")
    parser.add_argument("--version", action="version", version=f"icc-app {db.VERSION}")
    parser.add_argument("--sync", action="store_true", help="只执行一次同步后退出")
    parser.add_argument("--port", type=int, default=8770)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--no-scheduler", action="store_true", help="不启动定时任务")
    args = parser.parse_args()

    db.init_db()
    if args.sync:
        conn = db.connect()
        try:
            res = sync.sync_all(conn)
            print(f"日线 {res['bars']['ok']}/{res['bars']['total']} 只，{res['bars']['rows']} 行")
            print(f"快照 {res['quotes'].get('ok', 0)}/{res['quotes'].get('total', 0)} 只")
            if res["bars"]["notes"]:
                print("提示:", res["bars"]["notes"])
        finally:
            conn.close()
        return

    if not args.no_scheduler:
        start_scheduler()
    print(f"ICC 工作台已启动： http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    sys.exit(main())
