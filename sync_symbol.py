"""按需补一只标的：加自选 → 3 年日线 → 快照 → 可选新闻。

stdout 只输出一段 JSON，供 DSH 插件调用：
    python sync_symbol.py --symbol 600360.SH --data-dir data --with-news
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

import db  # noqa: E402
import feed  # noqa: E402
import newsfeed  # noqa: E402
import sync  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--name", default="")
    ap.add_argument("--data-dir", default="")
    ap.add_argument("--years", type=int, default=3)
    ap.add_argument("--with-news", action="store_true")
    args = ap.parse_args()

    if args.data_dir:
        db.DATA_DIR = Path(args.data_dir).resolve()
        db.DB_PATH = db.DATA_DIR / "app.db"
    db.init_db()
    conn = db.connect()
    try:
        symbol = args.symbol.strip().upper()
        name = args.name.strip()
        if not name:
            row = conn.execute(
                "SELECT name FROM instruments WHERE symbol=?", (symbol,)
            ).fetchone()
            name = row[0] if row else ""
        if not name:
            try:
                items = feed.quotes([symbol])
                name = items[0].get("name", "") if items else ""
            except Exception:  # noqa: BLE001
                name = ""

        db.add_instrument(conn, symbol, name, "自选")
        synced = sync.sync_symbol(conn, symbol, years=args.years)
        news = {"fetched": 0, "kept": 0, "scored": 0}
        if args.with_news:
            news = newsfeed.sync(conn, [(symbol, name or symbol)], per_symbol=8)
        print(json.dumps({
            "ok": True,
            "symbol": symbol,
            "name": name or symbol,
            **synced,
            "news": news,
        }, ensure_ascii=False))
    except Exception as e:  # noqa: BLE001 - 统一返回结构化错误
        print(json.dumps({
            "ok": False,
            "code": "SYNC_FAILED",
            "symbol": args.symbol,
            "error": str(e),
        }, ensure_ascii=False))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
