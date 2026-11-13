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
        bars = conn.execute(
            "select count(*) from bars where symbol=? and date<=?",
            (args.symbol, asof),
        ).fetchone()[0]
        if bars < 30:
            # 空工作区直接返回可读原因，避免拿空证据调用整条 LLM 流水线。
            print(json.dumps({
                "ok": False,
                "code": "NEED_DATA",
                "symbol": args.symbol,
                "asof": asof,
                "bars": bars,
                "error": (
                    f"{args.symbol} 在 {asof} 之前只有 {bars} 条本地日线（至少需要 30 条），"
                    "无法评估。请先同步行情：cd <ICC 代码目录> && python app.py --sync"
                ),
            }, ensure_ascii=False))
            return
        c = council.run(conn, asof, symbols=[args.symbol], record=False)
        try:
            res = debate.run(conn, args.symbol, asof, council_payload=c,
                             record=not args.no_record)
        except debate.DebateError as e:
            msg = str(e)
            print(json.dumps({
                "ok": False,
                "code": "NEED_API_KEY" if "DEEPSEEK_API_KEY" in msg else "LLM_ERROR",
                "symbol": args.symbol,
                "asof": asof,
                "error": msg,
                "hint": (
                    "辩论需要 DeepSeek API：把密钥写入环境变量 DEEPSEEK_API_KEY，"
                    "或 DSH 的 ~/.dsh/.credentials.yaml；也支持在 "
                    "config/debate_prompt.yaml 里改 model / base_url"
                ),
            }, ensure_ascii=False))
            return
        print(json.dumps(res, ensure_ascii=False))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
