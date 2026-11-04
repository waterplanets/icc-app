"""虚拟盘执行层：A 股费用模型 + 买卖成交 + 组合估值。

费用口径与 invest-council 技能里的 PaperBroker 一致：
佣金 0.025%（最低 5 元）、卖出印花税 0.1%、过户费 0.001%、滑点 5bp、整手 100 股。

成交日期取账户的时间指针 asof：回放模式下补录的成交都记在 asof 当天，
避免出现「用未来价格交易」的漏洞。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional

import db

FEE = {
    "commission_rate": 0.00025,
    "commission_min": 5.0,
    "stamp_tax_sell": 0.001,
    "transfer_fee": 0.00001,
    "lot_size": 100,
    "slippage_bps": 5.0,
    "max_participation": 0.02,   # 单笔最多吃当日成交量的比例（流动性约束）
}

LIMIT_PCT = {"main": 0.10, "gem": 0.20, "bj": 0.30, "st": 0.05}


def price_limit_pct(symbol: str, name: str = "") -> float:
    """涨跌幅限制：主板 10%、创业板/科创板 20%、北交所 30%、ST 5%。"""
    code, mkt = symbol.split(".")
    if "ST" in (name or "").upper():
        return LIMIT_PCT["st"]
    if mkt == "BJ":
        return LIMIT_PCT["bj"]
    if code.startswith(("688", "300", "301")):
        return LIMIT_PCT["gem"]
    return LIMIT_PCT["main"]


def limit_prices(prev_close: float, pct: float) -> tuple:
    return (round(prev_close * (1 + pct), 2), round(prev_close * (1 - pct), 2))


def market_ref(conn: sqlite3.Connection, account: Dict[str, Any],
               symbol: str) -> Optional[tuple]:
    """取 (昨收, 参考价, 当日成交量/手)。

    实时模式用快照的昨收与现价，成交量取最近一个完整交易日（盘中累计量太小）；
    回放模式用 asof 当天的日线与前一天收盘。
    """
    if account["live"]:
        q = db.get_quote(conn, symbol)
        if q and q["price"] and q["prev_close"]:
            bar = conn.execute(
                "SELECT volume FROM bars WHERE symbol=? AND date<=? ORDER BY date DESC LIMIT 1",
                (symbol, account["asof"])).fetchone()
            vol = float(bar[0]) if bar and bar[0] else float(q["volume"] or 0)
            return float(q["prev_close"]), float(q["price"]), vol
    rows = conn.execute(
        "SELECT close, volume FROM bars WHERE symbol=? AND date<=? ORDER BY date DESC LIMIT 2",
        (symbol, account["asof"])).fetchall()
    if len(rows) < 2:
        return None
    return float(rows[1]["close"]), float(rows[0]["close"]), float(rows[0]["volume"] or 0)


def volume_cap(volume_hands: Optional[float]) -> int:
    """单笔可成交上限：当日成交量 × 参与率，按整手向下取整。"""
    if not volume_hands or volume_hands <= 0:
        return 0
    shares = volume_hands * 100 * FEE["max_participation"]
    return int(shares // FEE["lot_size"]) * FEE["lot_size"]


def fill_price(price: float, side: str) -> float:
    s = FEE["slippage_bps"] / 10000.0
    return round(price * (1 + s) if side == "buy" else price * (1 - s), 3)


def fees_of(amount: float, side: str) -> float:
    fee = max(FEE["commission_min"], amount * FEE["commission_rate"]) + amount * FEE["transfer_fee"]
    if side == "sell":
        fee += amount * FEE["stamp_tax_sell"]
    return round(fee, 2)


def trade_price(conn: sqlite3.Connection, account: Dict[str, Any],
                symbol: str) -> Optional[float]:
    """当前时间里该标的可用的成交参考价：实时模式用快照，回放模式用 asof 收盘。"""
    if account["live"]:
        q = db.get_quote(conn, symbol)
        if q and q["price"]:
            return float(q["price"])
    info = db.close_on(conn, symbol, account["asof"])
    return info["close"] if info else None


def buy(conn: sqlite3.Connection, account_id: int, symbol: str, name: str, qty: int,
        price: float, note: str = "") -> Dict[str, Any]:
    acc = db.account(conn, account_id)
    lot = FEE["lot_size"]
    qty = int(qty // lot) * lot
    if qty <= 0:
        return {"status": "rejected", "reason": "不足一手（100 股）"}
    limited = False
    ref = market_ref(conn, acc, symbol)
    if ref:
        prev_close, _, vol = ref
        pct = price_limit_pct(symbol, name)
        up, _down = limit_prices(prev_close, pct)
        if price >= up - 0.005:
            return {"status": "rejected", "reason": f"涨停封板（涨停价 {up}），买不进"}
        cap = volume_cap(vol)
        if vol <= 0:
            return {"status": "rejected", "reason": "当日成交量为 0（停牌或数据缺失）"}
        if cap <= 0:
            return {"status": "rejected", "reason": "当日成交量过小，不足一手"}
        if qty > cap:
            qty, limited = cap, True
    px = fill_price(price, "buy")
    amount = qty * px
    fee = fees_of(amount, "buy")
    if amount + fee > acc["cash"] + 1e-6:
        qty = int((acc["cash"] / (px * 1.002)) // lot) * lot
        if qty <= 0:
            return {"status": "rejected", "reason": "可用资金不足"}
        amount = qty * px
        fee = fees_of(amount, "buy")

    db.set_cash(conn, account_id, acc["cash"] - amount - fee)
    pos = db.get_position(conn, account_id, symbol)
    if pos:
        total = pos["avg_cost"] * pos["qty"] + amount
        new_qty = pos["qty"] + qty
        db.save_position(conn, account_id, symbol, name or pos["name"], new_qty, total / new_qty)
    else:
        db.save_position(conn, account_id, symbol, name or symbol, qty, px)
    rec = {
        "account_id": account_id, "trade_date": acc["asof"],
        "ts": f"{acc['asof']} {datetime.now().strftime('%H:%M:%S')}",
        "symbol": symbol, "name": name, "side": "buy", "qty": qty, "price": px,
        "amount": round(amount, 2), "fee": fee, "pnl": None, "note": note,
        "limited": limited,
    }
    db.add_trade(conn, **rec)
    record_equity_at_cursor(conn, account_id, "买入 " + symbol)
    return {"status": "filled", "reason": "", **rec}


def sell(conn: sqlite3.Connection, account_id: int, symbol: str, qty: Optional[int],
         price: float, note: str = "") -> Dict[str, Any]:
    acc = db.account(conn, account_id)
    pos = db.get_position(conn, account_id, symbol)
    if not pos:
        return {"status": "rejected", "reason": "无持仓"}
    qty = pos["qty"] if qty is None else min(int(qty), pos["qty"])
    if qty <= 0:
        return {"status": "rejected", "reason": "卖出数量为 0"}
    limited = False
    ref = market_ref(conn, acc, symbol)
    if ref:
        prev_close, _, vol = ref
        pct = price_limit_pct(symbol, pos["name"])
        _up, down = limit_prices(prev_close, pct)
        if price <= down + 0.005:
            return {"status": "rejected", "reason": f"跌停封板（跌停价 {down}），卖不掉"}
        cap = volume_cap(vol)
        if vol <= 0:
            return {"status": "rejected", "reason": "当日成交量为 0（停牌）"}
        if cap <= 0:
            return {"status": "rejected", "reason": "当日成交量过小，不足一手"}
        if qty > cap:
            qty, limited = cap, True
    px = fill_price(price, "sell")
    amount = qty * px
    fee = fees_of(amount, "sell")
    pnl = round((px - pos["avg_cost"]) * qty - fee, 2)
    db.set_cash(conn, account_id, acc["cash"] + amount - fee)
    db.save_position(conn, account_id, symbol, pos["name"], pos["qty"] - qty, pos["avg_cost"])
    rec = {
        "account_id": account_id, "trade_date": acc["asof"],
        "ts": f"{acc['asof']} {datetime.now().strftime('%H:%M:%S')}",
        "symbol": symbol, "name": pos["name"], "side": "sell", "qty": qty, "price": px,
        "amount": round(amount, 2), "fee": fee, "pnl": pnl, "note": note,
        "limited": limited,
    }
    db.add_trade(conn, **rec)
    record_equity_at_cursor(conn, account_id, "卖出 " + symbol)
    return {"status": "filled", "reason": "", **rec}


def portfolio(conn: sqlite3.Connection, account_id: int) -> Dict[str, Any]:
    """按时间指针估值：实时用快照，回放用 asof 收盘，绝不使用 asof 之后的行情。"""
    acc = db.account(conn, account_id)
    items: List[Dict[str, Any]] = []
    mv = 0.0
    for p in db.positions(conn, account_id):
        px = trade_price(conn, acc, p["symbol"])
        price = px if px is not None else p["avg_cost"]
        value = price * p["qty"]
        cost = p["avg_cost"] * p["qty"]
        mv += value
        items.append({
            "symbol": p["symbol"], "name": p["name"] or p["symbol"], "qty": p["qty"],
            "avg_cost": round(p["avg_cost"], 3), "price": round(price, 3),
            "value": round(value, 2), "cost": round(cost, 2),
            "pnl": round(value - cost, 2),
            "pnl_pct": round((value / cost - 1) * 100, 2) if cost else 0.0,
            "weight": 0.0,
        })
    equity = acc["cash"] + mv
    for it in items:
        it["weight"] = round(it["value"] / equity * 100, 2) if equity else 0.0
    return {
        "account_id": account_id, "name": acc["name"], "asof": acc["asof"], "live": acc["live"],
        "cash": round(acc["cash"], 2),
        "initial_cash": round(acc["initial_cash"], 2),
        "market_value": round(mv, 2),
        "equity": round(equity, 2),
        "total_pnl": round(equity - acc["initial_cash"], 2),
        "total_pnl_pct": round((equity / acc["initial_cash"] - 1) * 100, 2) if acc["initial_cash"] else 0.0,
        "cash_ratio": round(acc["cash"] / equity * 100, 2) if equity else 0.0,
        "positions": sorted(items, key=lambda x: -x["value"]),
    }


def record_equity_at_cursor(conn: sqlite3.Connection, account_id: int, note: str = "") -> None:
    acc = db.account(conn, account_id)
    pf = portfolio(conn, account_id)
    db.record_equity(conn, account_id, acc["asof"], pf["cash"], pf["market_value"], note)
