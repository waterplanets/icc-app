"""同步调度：日线增量入库 + 实时快照 + 收盘净值快照。

同步范围：自选股 ∪ 持仓 ∪ 沪深300（基准），不做全市场扫描。
日线增量以库内 last_bar_date 为起点，重复拉取最后一天用于覆盖当日修正。
"""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from typing import Any, Dict, List

import broker
import db
import feed


def sync_bars(conn: sqlite3.Connection, full: bool = False) -> Dict[str, Any]:
    """全量或增量同步日线。首次入库回看 3 年。"""
    symbols = db.all_symbols(conn)
    ok, failed, new_rows = 0, [], 0
    for sym in symbols:
        last = None if full else db.last_bar_date(conn, sym)
        start = last or (date.today() - timedelta(days=365 * 3)).isoformat()
        try:
            data = feed.daily_bars(sym, start=start)
        except Exception as e:      # noqa: BLE001 - 单只失败不影响其它
            failed.append({"symbol": sym, "error": str(e)})
            continue
        n = db.upsert_bars(conn, sym, data["rows"])
        new_rows += n
        ok += 1
        if data["source"] != "eastmoney":
            failed.append({"symbol": sym, "error": "已回退腾讯源"})
    return {"ok": ok, "total": len(symbols), "rows": new_rows, "notes": failed}


def sync_quotes(conn: sqlite3.Connection) -> Dict[str, Any]:
    """刷新自选股/持仓/基准的实时快照，并落一条当日净值。"""
    symbols = db.all_symbols(conn)
    try:
        items = feed.quotes(symbols)
    except Exception as e:          # noqa: BLE001
        return {"ok": 0, "total": len(symbols), "error": str(e)}
    n = db.upsert_quotes(conn, items)
    for q in items:
        if q.get("name"):
            conn.execute(
                "UPDATE instruments SET name=? WHERE symbol=? AND (name='' OR name=symbol)",
                (q["name"], q["symbol"]))
    conn.commit()
    # 只给「时间指针=今天」的账户落净值；回放账户的净值由成交驱动
    for acc in db.accounts(conn):
        if acc["asof"] >= db.today_str():
            broker.record_equity_at_cursor(conn, acc["id"], "行情快照")
    bad = [q["symbol"] for q in items if q.get("error") or q.get("price") is None]
    return {"ok": n, "total": len(symbols), "bad": bad}


def sync_all(conn: sqlite3.Connection) -> Dict[str, Any]:
    bars = sync_bars(conn)
    quotes = sync_quotes(conn)
    return {"bars": bars, "quotes": quotes}


def sync_symbol(conn: sqlite3.Connection, symbol: str, years: int = 3) -> Dict[str, Any]:
    """只补一只标的：历史日线 + 实时快照，供对话里按需扩充。"""
    start = (date.today() - timedelta(days=365 * max(1, int(years)))).isoformat()
    data = feed.daily_bars(symbol, start=start)
    rows = db.upsert_bars(conn, symbol, data["rows"])
    quotes = 0
    try:
        items = feed.quotes([symbol])
        quotes = db.upsert_quotes(conn, items)
        for q in items:
            if q.get("name"):
                conn.execute(
                    "UPDATE instruments SET name=? WHERE symbol=? AND (name='' OR name=symbol)",
                    (q["name"], symbol))
        conn.commit()
    except Exception:  # noqa: BLE001 - 快照失败不影响历史日线
        pass
    return {"symbol": symbol, "bars": rows, "quotes": quotes, "source": data.get("source")}


def intraday_snapshot(conn: sqlite3.Connection) -> Dict[str, Any]:
    """盘中轻量快照：只更新报价，不拉日线。"""
    return sync_quotes(conn)
