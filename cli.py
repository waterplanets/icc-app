"""命令行接口：让 AI 或脚本直接调仓、推进时间、查状态。

用法示例：
    python cli.py status
    python cli.py buy 600519.SH 100
    python cli.py sell 600519.SH 100 --note 止盈
    python cli.py asof --next              # 时间指针前进一个交易日
    python cli.py asof --set 2026-10-08
    python cli.py quote 600519.SH
    python cli.py list
    python cli.py sync

默认输出中文文本；加 --json 输出机器可读 JSON（方便 AI 解析）。
账户用 --account 指定 id 或名称，省略时用第一个账户。
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Dict, Optional

import broker
import db
import feed
import sync


def resolve_account(conn, ref: Optional[str]) -> Dict[str, Any]:
    if not ref:
        return db.account(conn)
    if ref.isdigit():
        return db.account(conn, int(ref))
    row = conn.execute("SELECT id FROM accounts WHERE name=?", (ref,)).fetchone()
    if not row:
        raise SystemExit(f"账户不存在：{ref}")
    return db.account(conn, int(row[0]))


def fmt_money(v: Optional[float]) -> str:
    return "—" if v is None else f"{v:,.2f}"


def print_status(conn, acc) -> None:
    pf = broker.portfolio(conn, acc["id"])
    print(f"账户：{acc['name']}（id={acc['id']}）  虚拟现在：{acc['asof']}"
          f"{'  实时' if acc['live'] else '  回放'}")
    print(f"现金：{fmt_money(pf['cash'])}   市值：{fmt_money(pf['market_value'])}   "
          f"净值：{fmt_money(pf['equity'])}（{pf['total_pnl_pct']:+.2f}%）")
    if not pf["positions"]:
        print("持仓：空仓")
        return
    print("持仓：")
    print(f"  {'代码':<11}{'名称':<10}{'数量':>7}{'成本':>10}{'现价':>10}{'浮盈':>12}{'权重':>8}")
    for p in pf["positions"]:
        print(f"  {p['symbol']:<11}{p['name']:<10}{p['qty']:>7}{p['avg_cost']:>10.2f}"
              f"{p['price']:>10.2f}{p['pnl']:>12.2f}{p['weight']:>7.1f}%")


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="ICC 工作台命令行")
    parser.add_argument("--account", help="账户 id 或名称，默认第一个账户")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--version", action="version", version=f"icc-app {db.VERSION}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("accounts", help="列出账户")
    sub.add_parser("status", help="账户与持仓")
    sub.add_parser("list", help="自选股与当前时间点价格")
    sub.add_parser("sync", help="同步日线与快照").add_argument(
        "--bars-only", action="store_true", help="只同步日线")

    p_buy = sub.add_parser("buy", help="买入")
    p_buy.add_argument("symbol")
    p_buy.add_argument("qty", type=int)
    p_buy.add_argument("--price", type=float, help="不填则用当前时间点价格")
    p_buy.add_argument("--note", default="")

    p_sell = sub.add_parser("sell", help="卖出")
    p_sell.add_argument("symbol")
    p_sell.add_argument("qty", type=int)
    p_sell.add_argument("--price", type=float)
    p_sell.add_argument("--note", default="")

    p_asof = sub.add_parser("asof", help="查看/推进时间指针")
    g = p_asof.add_mutually_exclusive_group()
    g.add_argument("--set", help="跳到指定日期 YYYY-MM-DD")
    g.add_argument("--next", action="store_true", help="前进一个交易日")
    g.add_argument("--today", action="store_true", help="回到今天")

    p_quote = sub.add_parser("quote", help="当前时间点可用价")
    p_quote.add_argument("symbol")

    p_kline = sub.add_parser("kline", help="查看截止当前时间的日线")
    p_kline.add_argument("symbol")
    p_kline.add_argument("--n", type=int, default=10)

    args = parser.parse_args(argv)
    db.init_db()
    conn = db.connect()
    try:
        acc = resolve_account(conn, args.account)

        if args.cmd == "accounts":
            rows = [{"id": r["id"], "name": r["name"], "asof": r["asof"],
                     "cash": r["cash"], "initial_cash": r["initial_cash"]}
                    for r in db.accounts(conn)]
            if args.json:
                print(json.dumps(rows, ensure_ascii=False, indent=2))
            else:
                for r in rows:
                    print(f"{r['id']:>3}  {r['name']:<10} {r['asof']}  "
                          f"初始 {fmt_money(r['initial_cash'])}")
            return 0

        if args.cmd == "status":
            if args.json:
                pf = broker.portfolio(conn, acc["id"])
                print(json.dumps({**pf, "account": acc["name"]}, ensure_ascii=False, indent=2))
            else:
                print_status(conn, acc)
            return 0

        if args.cmd == "list":
            rows = db.watchlist_with_quotes(conn, acc["asof"], acc["live"])
            if args.json:
                print(json.dumps(rows, ensure_ascii=False, indent=2, default=str))
            else:
                print(f"虚拟现在 {acc['asof']}（{'实时' if acc['live'] else '回放'}）")
                for r in rows:
                    nm = r.get("qname") or r.get("iname") or r["symbol"]
                    print(f"  {r['symbol']:<11}{nm:<10}{fmt_money(r.get('price')):>12}"
                          f"  {(r['pct'] if r['pct'] is not None else 0):+7.2f}%  {r['last_date'] or ''}")
            return 0

        if args.cmd == "sync":
            res = sync.sync_bars(conn) if args.bars_only else sync.sync_all(conn)
            print(json.dumps(res, ensure_ascii=False, indent=2))
            return 0

        if args.cmd == "asof":
            if args.set:
                target = args.set
            elif args.next:
                target = db.next_trading_day(conn, acc["asof"]) or acc["asof"]
            elif args.today:
                target = db.today_str()
            else:
                print(f"{acc['asof']}（{'实时' if acc['live'] else '回放'}）")
                return 0
            db.set_asof(conn, acc["id"], target)
            pf = broker.portfolio(conn, acc["id"])
            db.record_equity(conn, acc["id"], target, pf["cash"], pf["market_value"], "时间指针")
            print(f"时间指针 {acc['asof']} → {target}")
            return 0

        if args.cmd == "quote":
            sym = feed.normalize_symbol(args.symbol)
            px = broker.trade_price(conn, acc, sym)
            info = db.close_on(conn, sym, acc["asof"])
            out = {"symbol": sym, "asof": acc["asof"], "price": px,
                   "date": info["date"] if info else None}
            print(json.dumps(out, ensure_ascii=False, indent=2) if args.json
                  else f"{sym} @ {acc['asof']}：{fmt_money(px)}（日线 {out['date'] or '—'}）")
            return 0

        if args.cmd == "kline":
            rows = db.bars_until(conn, feed.normalize_symbol(args.symbol), acc["asof"], args.n)
            data = [{"date": r["date"], "open": r["open"], "high": r["high"],
                     "low": r["low"], "close": r["close"], "volume": r["volume"]}
                    for r in rows]
            if args.json:
                print(json.dumps(data, ensure_ascii=False, indent=2))
            else:
                for d in data:
                    print(f"{d['date']}  O {d['open']:.2f}  H {d['high']:.2f}  "
                          f"L {d['low']:.2f}  C {d['close']:.2f}")
            return 0

        if args.cmd in ("buy", "sell"):
            sym = feed.normalize_symbol(args.symbol)
            row = conn.execute("SELECT name FROM instruments WHERE symbol=?", (sym,)).fetchone()
            name = row[0] if row else sym
            px = args.price if args.price else broker.trade_price(conn, acc, sym)
            if px is None:
                print(f"{sym} 在 {acc['asof']} 没有可用价格，请用 --price 指定", file=sys.stderr)
                return 2
            res = (broker.buy(conn, acc["id"], sym, name, args.qty, px, args.note)
                   if args.cmd == "buy"
                   else broker.sell(conn, acc["id"], sym, args.qty, px, args.note))
            if args.json:
                print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
            elif res["status"] == "filled":
                print(f"{'买入' if res['side'] == 'buy' else '卖出'} {sym} {res['qty']} 股 "
                      f"@ {res['price']}（成交日 {res['trade_date']}），费用 {res['fee']} 元"
                      + (f"，本次盈亏 {res['pnl']}" if res.get("pnl") is not None else ""))
                print(f"剩余现金 {fmt_money(db.account(conn, acc['id'])['cash'])}")
            else:
                print(f"委托被拒：{res['reason']}", file=sys.stderr)
                return 1
            return 0

        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
