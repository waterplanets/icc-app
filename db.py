"""SQLite 存储层：账户、自选股、日线、快照、持仓、成交、净值。

两个关键概念：
1. 账户（accounts）：支持多个虚拟盘，互不影响；每个账户有自己的现金与时间指针。
2. 时间指针（asof）：账户的「虚拟当前日期」。所有展示与成交都只看 <= asof 的数据，
   asof 等于今天时视为实时模式（用快照价），否则为回放模式（用日线收盘价）。

数据库默认在 data/app.db（可用 ICC_APP_DATA 覆盖），与 invest-council 技能的
~/.icc/workspace 隔离。
"""
from __future__ import annotations

import os
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

VERSION = "0.1.0"
BASE_DIR = Path(__file__).resolve().parent
# 数据目录可用环境变量覆盖，避免个人数据混进仓库
DATA_DIR = Path(os.environ.get("ICC_APP_DATA") or (BASE_DIR / "data"))
DB_PATH = DATA_DIR / "app.db"
BENCHMARK = "000300.SH"
INITIAL_CASH = 1_000_000.0

SEED_WATCHLIST: List[tuple] = [
    ("600519.SH", "贵州茅台", "白酒"),
    ("000858.SZ", "五粮液", "白酒"),
    ("600036.SH", "招商银行", "金融"),
    ("601318.SH", "中国平安", "金融"),
    ("300750.SZ", "宁德时代", "新能源"),
    ("002594.SZ", "比亚迪", "新能源"),
    ("600276.SH", "恒瑞医药", "医药"),
    ("300760.SZ", "迈瑞医疗", "医药"),
    ("002415.SZ", "海康威视", "科技"),
    ("688981.SH", "中芯国际", "科技"),
    ("601012.SH", "隆基绿能", "新能源"),
    ("000333.SZ", "美的集团", "消费"),
    ("601899.SH", "紫金矿业", "资源"),
    ("600900.SH", "长江电力", "公用"),
    ("002230.SZ", "科大讯飞", "科技"),
    ("600030.SH", "中信证券", "金融"),
]


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def today_str() -> str:
    return date.today().strftime("%Y-%m-%d")


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=15, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts(
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL UNIQUE,
    cash         REAL NOT NULL,
    initial_cash REAL NOT NULL,
    asof         TEXT NOT NULL,          -- 虚拟当前日期
    created_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS instruments(
    symbol     TEXT PRIMARY KEY,
    name       TEXT NOT NULL DEFAULT '',
    group_name TEXT NOT NULL DEFAULT '自选',
    is_watch   INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bars(
    symbol TEXT NOT NULL, date TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL, volume REAL, amount REAL,
    PRIMARY KEY (symbol, date)
);
CREATE TABLE IF NOT EXISTS quotes(
    symbol TEXT PRIMARY KEY, name TEXT,
    price REAL, pct REAL, change REAL, open REAL, high REAL, low REAL, prev_close REAL,
    volume REAL, amount REAL, source TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS positions(
    account_id INTEGER NOT NULL,
    symbol     TEXT NOT NULL,
    name       TEXT,
    qty        INTEGER NOT NULL,
    avg_cost   REAL NOT NULL,
    opened_at  TEXT NOT NULL,
    PRIMARY KEY (account_id, symbol)
);
CREATE TABLE IF NOT EXISTS trades(
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    trade_date TEXT NOT NULL,
    ts         TEXT NOT NULL,
    symbol     TEXT NOT NULL,
    name       TEXT,
    side       TEXT NOT NULL,
    qty        INTEGER NOT NULL,
    price      REAL NOT NULL,
    amount     REAL NOT NULL,
    fee        REAL NOT NULL,
    pnl        REAL,
    note       TEXT
);
CREATE TABLE IF NOT EXISTS equity(
    account_id   INTEGER NOT NULL,
    date         TEXT NOT NULL,
    cash         REAL NOT NULL,
    market_value REAL NOT NULL,
    equity       REAL NOT NULL,
    note         TEXT,
    PRIMARY KEY (account_id, date)
);
"""

OLD_TABLES = ("positions", "trades", "equity", "account")


def init_db() -> None:
    """建表 + 一次性迁移（旧版单账户结构会被重建，行情与自选保留）。"""
    conn = connect()
    try:
        existing = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        legacy = False
        if "positions" in existing:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(positions)").fetchall()]
            legacy = "account_id" not in cols
        if legacy:
            for t in OLD_TABLES:
                conn.execute(f"DROP TABLE IF EXISTS {t}")
        conn.executescript(SCHEMA)
        if conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0:
            conn.execute(
                "INSERT INTO accounts(name, cash, initial_cash, asof, created_at) VALUES(?,?,?,?,?)",
                ("主账户", INITIAL_CASH, INITIAL_CASH, today_str(), now_str()))
        conn.execute(
            "INSERT OR IGNORE INTO instruments(symbol, name, group_name, is_watch, created_at) "
            "VALUES(?, ?, '基准', 0, ?)", (BENCHMARK, "沪深300", now_str()))
        if conn.execute("SELECT COUNT(*) FROM instruments WHERE is_watch=1").fetchone()[0] == 0:
            for sym, name, grp in SEED_WATCHLIST:
                conn.execute(
                    "INSERT OR IGNORE INTO instruments(symbol, name, group_name, is_watch, created_at) "
                    "VALUES(?, ?, ?, 1, ?)", (sym, name, grp, now_str()))
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 账户与时间指针
# --------------------------------------------------------------------------- #
def accounts(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    return conn.execute("SELECT * FROM accounts ORDER BY id").fetchall()


def account(conn: sqlite3.Connection, account_id: Optional[int] = None) -> Dict[str, Any]:
    row = None
    if account_id is not None:
        row = conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
    if row is None:
        row = conn.execute("SELECT * FROM accounts ORDER BY id LIMIT 1").fetchone()
    if row is None:
        raise RuntimeError("没有可用账户，请先初始化数据库")
    d = dict(row)
    d["live"] = d["asof"] >= today_str()
    return d


def create_account(conn: sqlite3.Connection, name: str, cash: float) -> int:
    conn.execute(
        "INSERT INTO accounts(name, cash, initial_cash, asof, created_at) VALUES(?,?,?,?,?)",
        (name, cash, cash, today_str(), now_str()))
    conn.commit()
    return int(conn.execute("SELECT id FROM accounts WHERE name=?", (name,)).fetchone()[0])


def set_asof(conn: sqlite3.Connection, account_id: int, asof: str) -> None:
    if asof > today_str():
        asof = today_str()
    conn.execute("UPDATE accounts SET asof=? WHERE id=?", (asof, account_id))
    conn.commit()


def next_trading_day(conn: sqlite3.Connection, asof: str) -> Optional[str]:
    """从库内基准指数日线里取 asof 之后的下一个交易日。"""
    row = conn.execute(
        "SELECT MIN(date) FROM bars WHERE symbol=? AND date>?", (BENCHMARK, asof)).fetchone()
    if row and row[0]:
        return row[0]
    row = conn.execute(
        "SELECT MIN(date) FROM bars WHERE date>?", (asof,)).fetchone()
    return row[0] if row and row[0] else None


def set_cash(conn: sqlite3.Connection, account_id: int, cash: float) -> None:
    conn.execute("UPDATE accounts SET cash=? WHERE id=?", (cash, account_id))
    conn.commit()


def reset_account(conn: sqlite3.Connection, account_id: int, cash: float) -> None:
    conn.execute("DELETE FROM positions WHERE account_id=?", (account_id,))
    conn.execute("DELETE FROM trades WHERE account_id=?", (account_id,))
    conn.execute("DELETE FROM equity WHERE account_id=?", (account_id,))
    conn.execute("UPDATE accounts SET cash=?, initial_cash=? WHERE id=?", (cash, cash, account_id))
    conn.commit()


# --------------------------------------------------------------------------- #
# 自选股
# --------------------------------------------------------------------------- #
def all_symbols(conn: sqlite3.Connection) -> List[str]:
    syms = set(r[0] for r in conn.execute("SELECT symbol FROM instruments").fetchall())
    syms |= set(r[0] for r in conn.execute("SELECT symbol FROM positions").fetchall())
    syms.add(BENCHMARK)
    return sorted(syms)


def add_instrument(conn: sqlite3.Connection, symbol: str, name: str, group_name: str) -> None:
    conn.execute(
        "INSERT INTO instruments(symbol, name, group_name, is_watch, created_at) VALUES(?, ?, ?, 1, ?) "
        "ON CONFLICT(symbol) DO UPDATE SET name=excluded.name, group_name=excluded.group_name, is_watch=1",
        (symbol, name or symbol, group_name or "自选", now_str()))
    conn.commit()


def remove_instrument(conn: sqlite3.Connection, symbol: str) -> None:
    conn.execute("DELETE FROM instruments WHERE symbol=?", (symbol,))
    conn.commit()


def update_group(conn: sqlite3.Connection, symbol: str, group_name: str) -> None:
    conn.execute("UPDATE instruments SET group_name=? WHERE symbol=?", (group_name, symbol))
    conn.commit()


def close_on(conn: sqlite3.Connection, symbol: str, asof: str) -> Optional[Dict[str, Any]]:
    """asof 当日或之前最近一根日线。"""
    rows = conn.execute(
        "SELECT date, close FROM bars WHERE symbol=? AND date<=? ORDER BY date DESC LIMIT 2",
        (symbol, asof)).fetchall()
    if not rows:
        return None
    last = rows[0]
    prev = rows[1] if len(rows) > 1 else None
    pct = (last["close"] / prev["close"] - 1) * 100 if prev and prev["close"] else None
    return {"date": last["date"], "close": float(last["close"]), "pct": pct}


def watchlist_with_quotes(conn: sqlite3.Connection, asof: str,
                          live: bool) -> List[Dict[str, Any]]:
    """自选股 + 截止 asof 的价格。live 时优先用快照，否则用日线收盘。"""
    rows = conn.execute(
        """SELECT i.symbol, i.name AS iname, i.group_name,
                  q.name AS qname, q.price, q.pct, q.change, q.open, q.high, q.low,
                  q.source, q.updated_at
           FROM instruments i LEFT JOIN quotes q ON q.symbol = i.symbol
           WHERE i.is_watch = 1 ORDER BY i.group_name, i.symbol"""
    ).fetchall()
    out: List[Dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        info = close_on(conn, d["symbol"], asof)
        if live and d.get("price"):
            d["last_close"] = info["close"] if info else None
            d["last_date"] = info["date"] if info else None
        else:
            d["price"] = info["close"] if info else None
            d["pct"] = info["pct"] if info else None
            d["change"] = None
            d["last_close"] = info["close"] if info else None
            d["last_date"] = info["date"] if info else None
            d["source"] = "日线"
            d["updated_at"] = info["date"] if info else None
            d["open"] = d["high"] = d["low"] = None
        out.append(d)
    return out


# --------------------------------------------------------------------------- #
# 行情
# --------------------------------------------------------------------------- #
def last_bar_date(conn: sqlite3.Connection, symbol: str) -> Optional[str]:
    r = conn.execute("SELECT MAX(date) FROM bars WHERE symbol=?", (symbol,)).fetchone()
    return r[0] if r and r[0] else None


def upsert_bars(conn: sqlite3.Connection, symbol: str, rows: Iterable[Dict[str, Any]]) -> int:
    n = 0
    for r in rows:
        conn.execute(
            "INSERT INTO bars(symbol, date, open, high, low, close, volume, amount) "
            "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(symbol, date) DO UPDATE SET "
            "open=excluded.open, high=excluded.high, low=excluded.low, close=excluded.close, "
            "volume=excluded.volume, amount=excluded.amount",
            (symbol, r["date"], r["open"], r["high"], r["low"], r["close"],
             r.get("volume"), r.get("amount")))
        n += 1
    conn.commit()
    return n


def upsert_quotes(conn: sqlite3.Connection, items: Iterable[Dict[str, Any]]) -> int:
    n = 0
    for q in items:
        if q.get("price") is None:
            continue
        conn.execute(
            "INSERT INTO quotes(symbol, name, price, pct, change, open, high, low, prev_close, "
            "volume, amount, source, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(symbol) DO UPDATE SET name=excluded.name, price=excluded.price, "
            "pct=excluded.pct, change=excluded.change, open=excluded.open, high=excluded.high, "
            "low=excluded.low, prev_close=excluded.prev_close, volume=excluded.volume, "
            "amount=excluded.amount, source=excluded.source, updated_at=excluded.updated_at",
            (q["symbol"], q.get("name"), q.get("price"), q.get("pct"), q.get("change"),
             q.get("open"), q.get("high"), q.get("low"), q.get("prev_close"),
             q.get("volume"), q.get("amount"), q.get("source"), now_str()))
        n += 1
    conn.commit()
    return n


def get_quote(conn: sqlite3.Connection, symbol: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM quotes WHERE symbol=?", (symbol,)).fetchone()


def bars_until(conn: sqlite3.Connection, symbol: str, asof: str,
               limit: int = 250) -> List[sqlite3.Row]:
    rows = conn.execute(
        "SELECT * FROM bars WHERE symbol=? AND date<=? ORDER BY date DESC LIMIT ?",
        (symbol, asof, limit)).fetchall()
    return list(reversed(rows))


def bar_stats(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    rows = conn.execute(
        """SELECT b.symbol, i.name, COUNT(*) AS n, MIN(b.date) AS first, MAX(b.date) AS last
           FROM bars b LEFT JOIN instruments i ON i.symbol=b.symbol
           GROUP BY b.symbol ORDER BY b.symbol"""
    ).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------- #
# 持仓 / 成交 / 净值
# --------------------------------------------------------------------------- #
def positions(conn: sqlite3.Connection, account_id: int) -> List[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM positions WHERE account_id=? ORDER BY symbol", (account_id,)).fetchall()


def get_position(conn: sqlite3.Connection, account_id: int, symbol: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM positions WHERE account_id=? AND symbol=?", (account_id, symbol)).fetchone()


def save_position(conn: sqlite3.Connection, account_id: int, symbol: str,
                  name: str, qty: int, avg_cost: float) -> None:
    if qty <= 0:
        conn.execute("DELETE FROM positions WHERE account_id=? AND symbol=?", (account_id, symbol))
    else:
        conn.execute(
            "INSERT INTO positions(account_id, symbol, name, qty, avg_cost, opened_at) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(account_id, symbol) DO UPDATE SET "
            "qty=excluded.qty, avg_cost=excluded.avg_cost, name=excluded.name",
            (account_id, symbol, name, qty, avg_cost, now_str()))
    conn.commit()


def add_trade(conn: sqlite3.Connection, **kw: Any) -> int:
    cur = conn.execute(
        "INSERT INTO trades(account_id, trade_date, ts, symbol, name, side, qty, price, "
        "amount, fee, pnl, note) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (kw["account_id"], kw["trade_date"], kw["ts"], kw["symbol"], kw.get("name"),
         kw["side"], kw["qty"], kw["price"], kw["amount"], kw["fee"], kw.get("pnl"),
         kw.get("note")))
    conn.commit()
    return int(cur.lastrowid)


def trades(conn: sqlite3.Connection, account_id: int, limit: int = 200,
           until: Optional[str] = None) -> List[sqlite3.Row]:
    if until:
        return conn.execute(
            "SELECT * FROM trades WHERE account_id=? AND trade_date<=? "
            "ORDER BY id DESC LIMIT ?", (account_id, until, limit)).fetchall()
    return conn.execute(
        "SELECT * FROM trades WHERE account_id=? ORDER BY id DESC LIMIT ?",
        (account_id, limit)).fetchall()


def record_equity(conn: sqlite3.Connection, account_id: int, date_: str,
                  cash: float, market_value: float, note: str = "") -> None:
    conn.execute(
        "INSERT INTO equity(account_id, date, cash, market_value, equity, note) VALUES(?,?,?,?,?,?) "
        "ON CONFLICT(account_id, date) DO UPDATE SET cash=excluded.cash, "
        "market_value=excluded.market_value, equity=excluded.equity, note=excluded.note",
        (account_id, date_, round(cash, 2), round(market_value, 2),
         round(cash + market_value, 2), note))
    conn.commit()


def equity_curve(conn: sqlite3.Connection, account_id: int, until: Optional[str] = None,
                 limit: int = 500) -> List[Dict[str, Any]]:
    if until:
        rows = conn.execute(
            "SELECT * FROM equity WHERE account_id=? AND date<=? ORDER BY date DESC LIMIT ?",
            (account_id, until, limit)).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM equity WHERE account_id=? ORDER BY date DESC LIMIT ?",
            (account_id, limit)).fetchall()
    return [dict(r) for r in reversed(rows)]
