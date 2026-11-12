"""独立入口：给进程外的调用方（如 DSH 插件）用，stdout 只输出一段 JSON。

用法：
    python run_debate.py --symbol 600519.SH [--asof 2026-11-11]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

import council  # noqa: E402
import db  # noqa: E402
import debate  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--asof", default="")
    ap.add_argument("--data-dir", default="")
    ap.add_argument("--no-record", action="store_true")
    args = ap.parse_args()

    if args.data_dir:
        db.DATA_DIR = Path(args.data_dir).resolve()
        db.DB_PATH = db.DATA_DIR / "app.db"
    db.init_db()
    conn = db.connect()
    try:
        asof = args.asof.strip() or db.today_str()
        c = council.run(conn, asof, symbols=[args.symbol], record=False)
        res = debate.run(conn, args.symbol, asof, council_payload=c, record=not args.no_record)
        print(json.dumps(res, ensure_ascii=False))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
