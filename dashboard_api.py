"""仪表盘 JSON 接口：给 dsh-icc-tools 插件的宿主端调用。

每次调用都是一个独立子进程，stdout 只输出一行 JSON。
子命令：
    state                 账户/组合/自选/成交/净值/最近决议/定时任务
    create-account        新建虚拟仓账户
    reset-account         重置账户
    auto-trade            让 AI 按辩论决议调仓，并给出报告
    report                生成持仓与决议报告
    schedules-list        列出定时任务
    schedules-add         新增定时任务
    schedules-toggle      启用/停用定时任务
    schedules-delete      删除定时任务
    scheduler-tick        由宿主每分钟调用，执行到期任务
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

BASE_DIR = Path(__file__).resolve().parent
REPO = BASE_DIR.parent
# 工作空间、报告目录、数据目录都由宿主通过命令行参数传入，不读环境变量。
WORKSPACE = REPO
REPORT_DIR = WORKSPACE / ".icc" / "reports"
SCHEDULE_FILE = BASE_DIR / "data" / "schedules.json"

import broker  # noqa: E402
import council  # noqa: E402
import db  # noqa: E402
import debate  # noqa: E402
import feed  # noqa: E402
import newsfeed  # noqa: E402
import sync  # noqa: E402

TASK_TYPES = {"auto_trade", "report", "sync_all", "snapshot", "news_sync"}
TASK_LABELS = {
    "auto_trade": "AI 操盘",
    "report": "生成报告",
    "sync_all": "全量同步行情",
    "snapshot": "刷新快照",
    "news_sync": "抓取并打分新闻",
}


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def emit(value: Dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(value, ensure_ascii=False, default=str) + "\n")


def rows_to_dicts(rows: List[sqlite3.Row]) -> List[Dict[str, Any]]:
    return [dict(r) for r in rows]


def _account(conn: sqlite3.Connection, account_id: Optional[int]) -> Dict[str, Any]:
    return db.account(conn, account_id)


def _read_schedules() -> List[Dict[str, Any]]:
    if not SCHEDULE_FILE.exists():
        return []
    try:
        data = json.loads(SCHEDULE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _write_schedules(items: List[Dict[str, Any]]) -> None:
    SCHEDULE_FILE.parent.mkdir(parents=True, exist_ok=True)
    SCHEDULE_FILE.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def _normalize_time(value: str) -> str:
    m = re.match(r"^(\d{1,2}):(\d{2})$", (value or "").strip())
    if not m:
        raise ValueError("时间需为 HH:MM")
    hour, minute = int(m.group(1)), int(m.group(2))
    if hour > 23 or minute > 59:
        raise ValueError("时间超出范围")
    return f"{hour:02d}:{minute:02d}"


def _normalize_days(value: Any) -> List[int]:
    if value in (None, ""):
        return [1, 2, 3, 4, 5]
    days: List[int] = []
    for item in value if isinstance(value, list) else str(value).split(","):
        s = str(item).strip()
        if not s:
            continue
        try:
            d = int(s)
        except ValueError:
            continue
        if 1 <= d <= 7 and d not in days:
            days.append(d)
    if not days:
        raise ValueError("至少选择一个星期")
    return sorted(days)


# --------------------------------------------------------------------------- #
# 状态
# --------------------------------------------------------------------------- #
def _indices(conn: sqlite3.Connection, asof: str) -> List[Dict[str, Any]]:
    out = []
    for key in ("sh", "sz", "cyb", "hs300"):
        preset = feed.INDEX_PRESETS[key]
        info = db.close_on(conn, preset["symbol"], asof)
        out.append({
            "key": key, "name": preset["name"], "symbol": preset["symbol"],
            "close": info["close"] if info else None,
            "pct": info["pct"] if info else None,
        })
    return out


def _debate_summary(conn: sqlite3.Connection, asof: str) -> Optional[Dict[str, Any]]:
    data = debate.latest(conn)
    if not data:
        return None
    return {
        "symbol": data.get("symbol"),
        "name": data.get("name"),
        "rating": data.get("rating"),
        "verdict": data.get("verdict"),
        "confidence": data.get("confidence"),
        "position_pct": data.get("position_pct"),
        "generated_at": data.get("generated_at"),
        "summary": (data.get("judge") or {}).get("executive_summary"),
    }


def cmd_news_status(args: argparse.Namespace) -> Dict[str, Any]:
    """本地新闻时效：给对话模式先判断“要不要更新”。"""
    asof = args.asof or datetime.now().strftime("%Y-%m-%d")
    conn = db.connect()
    try:
        fresh = newsfeed.freshness(conn, args.symbol, asof, args.max_age_days)
        count = conn.execute(
            "SELECT COUNT(*) FROM news WHERE symbol=? AND date<=? AND label IS NOT NULL",
            (args.symbol, asof)).fetchone()[0]
        return {"ok": True, "symbol": args.symbol, "asof": asof, "count": count, **fresh}
    finally:
        conn.close()


def cmd_search(args: argparse.Namespace) -> Dict[str, Any]:
    """按代码/名称/拼音搜索 A 股，供仪表盘添加自选。"""
    keyword = str(args.keyword or "").strip()
    if not keyword:
        return {"ok": True, "items": []}
    return {"ok": True, "items": feed.search(keyword, limit=args.limit)}


def cmd_watch_add(args: argparse.Namespace) -> Dict[str, Any]:
    """加自选并补齐数据：3 年日线、快照、可选新闻打分。"""
    raw = str(args.symbol or "").strip().upper()
    if re.fullmatch(r"\d{6}", raw):
        raw = raw + (".SH" if raw.startswith("6") else ".SZ")
    if not re.fullmatch(r"\d{6}\.(SH|SZ)", raw):
        return {"ok": False, "code": "BAD_SYMBOL", "error": f"代码格式不对：{args.symbol}"}
    conn = db.connect()
    try:
        name = str(args.name or "").strip()
        if not name:
            row = conn.execute("SELECT name FROM instruments WHERE symbol=?", (raw,)).fetchone()
            name = row[0] if row else ""
        if not name:
            try:
                items = feed.quotes([raw])
                name = items[0].get("name", "") if items else ""
            except Exception:  # noqa: BLE001
                name = ""
        db.add_instrument(conn, raw, name, "自选")
        synced = sync.sync_symbol(conn, raw, years=args.years)
        news = {"fetched": 0, "kept": 0, "scored": 0}
        if args.with_news:
            news = newsfeed.sync(conn, [(raw, name or raw)], per_symbol=8)
        return {"ok": True, "symbol": raw, "name": name or raw, **synced, "news": news}
    finally:
        conn.close()


def cmd_state(args: argparse.Namespace) -> Dict[str, Any]:
    conn = db.connect()
    try:
        acc = _account(conn, args.account)
        pf = broker.portfolio(conn, acc["id"])
        watch = rows_to_dicts(db.watchlist_with_quotes(conn, acc["asof"], acc["live"]))
        trades = rows_to_dicts(db.trades(conn, acc["id"], int(args.limit), until=acc["asof"]))
        equity = db.equity_curve(conn, acc["id"], until=acc["asof"])
        last_sync = conn.execute("SELECT MAX(updated_at) FROM quotes").fetchone()[0]
        return {
            "ok": True,
            "accounts": rows_to_dicts(db.accounts(conn)),
            "account": acc,
            "portfolio": pf,
            "watchlist": watch,
            "trades": trades,
            "equity": equity,
            "indices": _indices(conn, acc["asof"]),
            "latest_debate": _debate_summary(conn, acc["asof"]),
            "schedules": _read_schedules(),
            "task_types": [{"id": k, "label": v} for k, v in TASK_LABELS.items()],
            "last_sync": last_sync,
            "paths": {
                "workspace": str(WORKSPACE),
                "reports": str(REPORT_DIR),
                "data": str(db.DATA_DIR),
            },
        }
    finally:
        conn.close()


def cmd_create_account(args: argparse.Namespace) -> Dict[str, Any]:
    conn = db.connect()
    try:
        name = (args.name or "").strip() or "新账户"
        cash = max(float(args.cash or db.INITIAL_CASH), 0.0)
        try:
            aid = db.create_account(conn, name, cash)
        except sqlite3.IntegrityError:
            return {"ok": False, "error": f"账户名「{name}」已存在"}
        return {"ok": True, "account_id": aid, "account": _account(conn, aid)}
    finally:
        conn.close()


def cmd_reset_account(args: argparse.Namespace) -> Dict[str, Any]:
    conn = db.connect()
    try:
        acc = _account(conn, args.account)
        cash = max(float(args.cash or acc["initial_cash"] or db.INITIAL_CASH), 0.0)
        db.reset_account(conn, acc["id"], cash)
        return {"ok": True, "account": _account(conn, acc["id"])}
    finally:
        conn.close()


def cmd_set_asof(args: argparse.Namespace) -> Dict[str, Any]:
    conn = db.connect()
    try:
        acc = _account(conn, args.account)
        target = "today" if args.action == "today" else (args.date or acc["asof"])
        if args.action == "next":
            target = db.next_trading_day(conn, acc["asof"]) or acc["asof"]
        if target == "today":
            target = db.today_str()
        db.set_asof(conn, acc["id"], target)
        return {"ok": True, "account": _account(conn, acc["id"])}
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# AI 操盘
# --------------------------------------------------------------------------- #
def _pick_symbols(conn: sqlite3.Connection, acc: Dict[str, Any], raw: str,
                  limit: int) -> List[str]:
    picked = [s.strip() for s in (raw or "").split(",") if s.strip()]
    if picked:
        return picked[:limit]
    rows = conn.execute(
        "SELECT symbol FROM instruments WHERE is_watch=1 ORDER BY group_name, symbol LIMIT ?",
        (limit,)).fetchall()
    return [r[0] for r in rows]


def _execute_decision(conn: sqlite3.Connection, acc: Dict[str, Any], symbol: str,
                      name: str, rating: str, position_pct: float,
                      equity: float) -> Dict[str, Any]:
    """把五档评级落成虚拟盘委托。仓位上限来自辩论产出的 position_pct（百分比）。"""
    px = broker.trade_price(conn, acc, symbol)
    if px is None:
        return {"symbol": symbol, "action": "skip", "reason": "没有可用成交价"}
    pos = db.get_position(conn, acc["id"], symbol)
    held = int(pos["qty"]) if pos else 0
    held_value = held * px
    target_value = equity * max(0.0, position_pct) / 100.0

    if rating in ("Sell", "Underweight"):
        if held <= 0:
            return {"symbol": symbol, "action": "skip", "reason": "无持仓，无需卖出"}
        res = broker.sell(conn, acc["id"], symbol, None, px, note=f"AI 操盘：{rating}")
        return {"symbol": symbol, "action": "sell", "result": res}

    if rating in ("Buy", "Overweight"):
        gap = target_value - held_value
        lot = broker.FEE["lot_size"]
        qty = int((gap / px) // lot) * lot
        if qty < lot:
            return {"symbol": symbol, "action": "skip",
                    "reason": f"目标仓位 {position_pct}% 与当前持仓差距不足一手"}
        res = broker.buy(conn, acc["id"], symbol, name, qty, px, note=f"AI 操盘：{rating}")
        return {"symbol": symbol, "action": "buy", "result": res}

    return {"symbol": symbol, "action": "hold",
            "reason": f"{rating}：维持现状（目标 {position_pct}%）"}


def cmd_auto_trade(args: argparse.Namespace) -> Dict[str, Any]:
    conn = db.connect()
    try:
        acc = _account(conn, args.account)
        symbols = _pick_symbols(conn, acc, args.symbols, max(1, int(args.limit)))
        if not symbols:
            return {"ok": False, "error": "自选股为空，请先在 ICC 工作台添加标的"}
        started = datetime.now().strftime("%H:%M:%S")
        decisions: List[Dict[str, Any]] = []
        for sym in symbols:
            name_row = conn.execute(
                "SELECT name FROM instruments WHERE symbol=?", (sym,)).fetchone()
            name = name_row[0] if name_row else sym
            try:
                payload = None
                if not args.no_council:
                    payload = council.run(conn, acc["asof"], symbols=[sym], record=False)
                res = debate.run(conn, sym, acc["asof"], council_payload=payload, record=True)
            except Exception as exc:  # 单个标的失败不影响其他标的
                decisions.append({"symbol": sym, "name": name, "status": "failed",
                                  "error": f"{type(exc).__name__}: {exc}"})
                continue
            pf = broker.portfolio(conn, acc["id"])
            act = _execute_decision(conn, acc, sym, name, res["rating"],
                                    float(res.get("position_pct") or 0), pf["equity"])
            decisions.append({
                "symbol": sym, "name": name, "status": "ok",
                "rating": res.get("rating"), "verdict": res.get("verdict"),
                "confidence": res.get("confidence"),
                "position_pct": res.get("position_pct"),
                "run_id": res.get("run_id"),
                "summary": (res.get("judge") or {}).get("executive_summary"),
                "action": act,
            })

        pf = broker.portfolio(conn, acc["id"])
        report = _build_report(conn, acc, pf, decisions=decisions,
                               title=f"AI 操盘记录 {acc['asof']}")
        return {
            "ok": True, "asof": acc["asof"], "started": started,
            "finished": datetime.now().strftime("%H:%M:%S"),
            "decisions": decisions, "portfolio": pf,
            "report": report["markdown"], "report_path": report["path"],
        }
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 报告
# --------------------------------------------------------------------------- #
def _build_report(conn: sqlite3.Connection, acc: Dict[str, Any], pf: Dict[str, Any],
                  decisions: Optional[List[Dict[str, Any]]] = None,
                  title: str = "ICC 投研报告") -> Dict[str, str]:
    lines: List[str] = [
        f"# {title}",
        "",
        f"- 账户：**{acc['name']}**（#{acc['id']}）",
        f"- 虚拟日期：{acc['asof']}（{'实时' if acc['live'] else '回放'}）",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "## 组合概览",
        "",
        "| 指标 | 数值 |",
        "|:--|--:|",
        f"| 总资产 | {pf['equity']:,.2f} 元 |",
        f"| 现金 | {pf['cash']:,.2f} 元 |",
        f"| 持仓市值 | {pf['market_value']:,.2f} 元 |",
        f"| 累计盈亏 | {pf['total_pnl']:+,.2f} 元 |",
        f"| 收益率 | {pf['total_pnl_pct']:+.2f}% |",
        f"| 仓位 | {100 - pf['cash_ratio']:.2f}% |",
        "",
        "## 持仓明细",
        "",
    ]
    if pf["positions"]:
        lines += ["| 代码 | 名称 | 数量 | 成本 | 现价 | 市值 | 盈亏 | 权重 |",
                  "|:--|:--|--:|--:|--:|--:|--:|--:|"]
        for p in pf["positions"]:
            lines.append(
                f"| {p['symbol']} | {p['name']} | {p['qty']} | {p['avg_cost']:.3f} | "
                f"{p['price']:.3f} | {p['value']:,.2f} | {p['pnl']:+,.2f} | {p['weight']:.2f}% |")
    else:
        lines.append("暂无持仓。")

    if decisions:
        lines += ["", "## AI 操盘决议", ""]
        for d in decisions:
            if d.get("status") != "ok":
                lines.append(f"- {d['symbol']}：辩论失败（{d.get('error', '未知错误')}）")
                continue
            act = d.get("action") or {}
            res = act.get("result") or {}
            if act.get("reason") or res.get("reason"):
                detail = act.get("reason") or res.get("reason")
            elif res.get("side"):
                detail = f"{res['side']} {res.get('qty', '')} 股 @ {res.get('price', '')}"
            else:
                detail = "未执行"
            lines.append(
                f"- {d['symbol']} {d['name']}：**{d['rating']} / {d['verdict']}**，"
                f"信心 {d['confidence']}，目标仓位 {d['position_pct']}%，执行 {detail}")
            if d.get("summary"):
                lines.append(f"  > {str(d['summary']).strip()[:220]}")

    latest = debate.latest(conn)
    if latest:
        lines += ["", "## 最近一次辩论", "",
                  f"- 标的：{latest.get('name')}（{latest.get('symbol')}）",
                  f"- 结论：{latest.get('rating')} / {latest.get('verdict')}，"
                  f"仓位 {latest.get('position_pct')}%",
                  f"- 生成：{latest.get('generated_at')}"]
        summary = (latest.get("judge") or {}).get("executive_summary")
        if summary:
            lines += ["", str(summary).strip()]

    markdown = "\n".join(lines).strip() + "\n"
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = REPORT_DIR / f"icc-dashboard-{stamp}.md"
    path.write_text(markdown, encoding="utf-8")
    return {"markdown": markdown, "path": str(path)}


def cmd_report(args: argparse.Namespace) -> Dict[str, Any]:
    conn = db.connect()
    try:
        acc = _account(conn, args.account)
        pf = broker.portfolio(conn, acc["id"])
        res = _build_report(conn, acc, pf, title=f"ICC 投研报告 {acc['asof']}")
        return {"ok": True, **res}
    finally:
        conn.close()


def cmd_debate_latest(args: argparse.Namespace) -> Dict[str, Any]:
    """返回完整辩论记录（含 stages 留痕与证据快照）。

    - 给仪表盘的「辩论」页签：按 symbol 取最近一次；
    - 给 ICC 模式的 debate_record 工具：按 run_id 精确取回，避免上下文被裁剪后丢失细节。
    """
    conn = db.connect()
    try:
        run_id = getattr(args, "run_id", None)
        if run_id:
            row = conn.execute(
                "SELECT payload FROM debate_runs WHERE id=?", (int(run_id),)).fetchone()
            data = json.loads(row[0]) if row else None
            if data is not None:
                data["run_id"] = int(run_id)
        else:
            sym = getattr(args, "symbol", "") or None
            if sym:
                row = conn.execute(
                    "SELECT id, payload FROM debate_runs WHERE symbol=? ORDER BY id DESC LIMIT 1",
                    (sym,)).fetchone()
            else:
                row = conn.execute(
                    "SELECT id, payload FROM debate_runs ORDER BY id DESC LIMIT 1").fetchone()
            data = json.loads(row[1]) if row else None
            if data is not None:
                data["run_id"] = row[0]
        if not data:
            return {"ok": False, "error": "没有找到对应的辩论记录"}
        return {"ok": True, "debate": data}
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 定时任务
# --------------------------------------------------------------------------- #
def cmd_schedules_list(_args: argparse.Namespace) -> Dict[str, Any]:
    return {"ok": True, "items": _read_schedules(),
            "task_types": [{"id": k, "label": v} for k, v in TASK_LABELS.items()]}


def cmd_schedules_add(args: argparse.Namespace) -> Dict[str, Any]:
    if args.type not in TASK_TYPES:
        return {"ok": False, "error": f"未知任务类型 {args.type}"}
    try:
        when = _normalize_time(args.time)
        days = _normalize_days(args.days)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    conn = db.connect()
    try:
        acc = _account(conn, args.account)
        item = {
            "id": uuid.uuid4().hex[:8],
            "type": args.type,
            "label": TASK_LABELS[args.type],
            "time": when,
            "days": days,
            "account_id": acc["id"],
            "symbols": (args.symbols or "").strip(),
            "enabled": True,
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "last_run": None,
            "last_result": None,
        }
        items = _read_schedules()
        items.append(item)
        _write_schedules(items)
        return {"ok": True, "item": item, "items": items}
    finally:
        conn.close()


def cmd_schedules_toggle(args: argparse.Namespace) -> Dict[str, Any]:
    items = _read_schedules()
    for item in items:
        if item.get("id") == args.id:
            item["enabled"] = bool(args.enabled)
            _write_schedules(items)
            return {"ok": True, "items": items}
    return {"ok": False, "error": "任务不存在"}


def cmd_schedules_delete(args: argparse.Namespace) -> Dict[str, Any]:
    items = [i for i in _read_schedules() if i.get("id") != args.id]
    _write_schedules(items)
    return {"ok": True, "items": items}


def _run_task(item: Dict[str, Any]) -> Dict[str, Any]:
    conn = db.connect()
    try:
        acc = _account(conn, item.get("account_id"))
        kind = item.get("type")
        if kind == "sync_all":
            res = sync.sync_all(conn)
            return {"ok": True, "detail": f"日线 {res['bars']['ok']}/{res['bars']['total']} 只"}
        if kind == "snapshot":
            res = sync.sync_quotes(conn)
            return {"ok": True, "detail": f"快照 {res.get('ok', 0)}/{res.get('total', 0)} 只"}
        if kind == "news_sync":
            targets = [(r["symbol"], r["name"]) for r in conn.execute(
                "SELECT symbol, name FROM instruments WHERE is_watch=1").fetchall()]
            res = newsfeed.sync(conn, targets, per_symbol=8)
            return {"ok": True, "detail": f"新闻 {res['kept']} 条，打分 {res['scored']} 条"}
        if kind == "report":
            pf = broker.portfolio(conn, acc["id"])
            res = _build_report(conn, acc, pf, title=f"定时报告 {acc['asof']}")
            return {"ok": True, "detail": f"已写入 {res['path']}"}
        if kind == "auto_trade":
            ns = argparse.Namespace(account=acc["id"], symbols=item.get("symbols") or "",
                                    limit=3, no_council=False)
            res = cmd_auto_trade(ns)
            if not res.get("ok"):
                return {"ok": False, "detail": res.get("error", "操盘失败")}
            done = [d for d in res["decisions"] if d.get("status") == "ok"]
            return {"ok": True, "detail": f"已处理 {len(done)} 个标的，报告 {res['report_path']}"}
        return {"ok": False, "detail": f"未知任务 {kind}"}
    finally:
        conn.close()


def cmd_scheduler_tick(_args: argparse.Namespace) -> Dict[str, Any]:
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    hhmm = now.strftime("%H:%M")
    weekday = now.isoweekday()
    items = _read_schedules()
    executed: List[Dict[str, Any]] = []
    changed = False
    for item in items:
        if not item.get("enabled"):
            continue
        if item.get("time") != hhmm:
            continue
        if weekday not in (item.get("days") or []):
            continue
        if (item.get("last_run") or "")[:10] == today:
            continue
        try:
            result = _run_task(item)
        except Exception as exc:
            result = {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}
        item["last_run"] = now.strftime("%Y-%m-%d %H:%M:%S")
        item["last_result"] = result
        changed = True
        executed.append({"id": item.get("id"), "type": item.get("type"), **result})
    if changed:
        _write_schedules(items)
    return {"ok": True, "executed": executed, "checked_at": now.strftime("%Y-%m-%d %H:%M:%S")}


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main() -> None:
    # 宿主端按 UTF-8 读取子进程输出，这里强制统一，避免 Windows 默认 cp936。
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass
    parser = argparse.ArgumentParser(description="ICC 仪表盘 JSON 接口")
    # 目录设置由宿主插件按插件配置传入，命令形式：
    #   dashboard_api.py --workspace ... --reports-dir ... --data-dir ... <子命令>
    parser.add_argument("--workspace", default="")
    parser.add_argument("--reports-dir", default="")
    parser.add_argument("--data-dir", default="")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("state")
    p.add_argument("--account", type=int, default=None)
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_state)

    p = sub.add_parser("watch-add")
    p.add_argument("--symbol", required=True)
    p.add_argument("--name", default="")
    p.add_argument("--years", type=int, default=3)
    p.add_argument("--with-news", action="store_true")
    p.set_defaults(func=cmd_watch_add)

    p = sub.add_parser("search")
    p.add_argument("--keyword", required=True)
    p.add_argument("--limit", type=int, default=8)
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("news-status")
    p.add_argument("--symbol", required=True)
    p.add_argument("--asof", default="")
    p.add_argument("--max-age-days", type=int, default=3)
    p.set_defaults(func=cmd_news_status)

    p = sub.add_parser("create-account")
    p.add_argument("--name", required=True)
    p.add_argument("--cash", default=str(db.INITIAL_CASH))
    p.set_defaults(func=cmd_create_account)

    p = sub.add_parser("reset-account")
    p.add_argument("--account", type=int, default=None)
    p.add_argument("--cash", default=None)
    p.set_defaults(func=cmd_reset_account)

    p = sub.add_parser("set-asof")
    p.add_argument("--account", type=int, default=None)
    p.add_argument("--action", default="set")
    p.add_argument("--date", default="")
    p.set_defaults(func=cmd_set_asof)

    p = sub.add_parser("auto-trade")
    p.add_argument("--account", type=int, default=None)
    p.add_argument("--symbols", default="")
    p.add_argument("--limit", type=int, default=3)
    p.add_argument("--no-council", action="store_true")
    p.set_defaults(func=cmd_auto_trade)

    p = sub.add_parser("report")
    p.add_argument("--account", type=int, default=None)
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("debate-latest")
    p.add_argument("--symbol", default="")
    p.add_argument("--run-id", type=int, default=0)
    p.set_defaults(func=cmd_debate_latest)

    p = sub.add_parser("schedules-list")
    p.set_defaults(func=cmd_schedules_list)

    p = sub.add_parser("schedules-add")
    p.add_argument("--type", required=True)
    p.add_argument("--time", required=True)
    p.add_argument("--days", default="1,2,3,4,5")
    p.add_argument("--account", type=int, default=None)
    p.add_argument("--symbols", default="")
    p.set_defaults(func=cmd_schedules_add)

    p = sub.add_parser("schedules-toggle")
    p.add_argument("--id", required=True)
    p.add_argument("--enabled", type=int, required=True)
    p.set_defaults(func=cmd_schedules_toggle)

    p = sub.add_parser("schedules-delete")
    p.add_argument("--id", required=True)
    p.set_defaults(func=cmd_schedules_delete)

    p = sub.add_parser("scheduler-tick")
    p.set_defaults(func=cmd_scheduler_tick)

    args = parser.parse_args()
    global WORKSPACE, REPORT_DIR, SCHEDULE_FILE
    if args.workspace:
        WORKSPACE = Path(args.workspace).resolve()
    REPORT_DIR = (Path(args.reports_dir).resolve() if args.reports_dir
                  else WORKSPACE / ".icc" / "reports")
    if args.data_dir:
        db.DATA_DIR = Path(args.data_dir).resolve()
        db.DB_PATH = db.DATA_DIR / "app.db"
    SCHEDULE_FILE = db.DATA_DIR / "schedules.json"
    db.init_db()
    try:
        emit(args.func(args))
    except Exception as exc:  # 任何异常都变成 JSON，宿主端不用解析堆栈
        emit({"ok": False, "error": f"{type(exc).__name__}: {exc}"})


if __name__ == "__main__":
    main()
