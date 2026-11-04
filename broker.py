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
}


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
