"""行情抓取层：东方财富 push2 / push2his 为主，腾讯行情为兜底。

只做两件事：
1. 批量实时快照（quotes）
2. 日线历史（daily_bars，支持从指定日期增量拉取）

所有请求串行并限速（间隔 >= 350ms），避免匿名高频被东财掐连接。
"""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# 指数预设：内部代码统一为 000001.SH 这种「代码.市场」形式
INDEX_PRESETS: Dict[str, Dict[str, str]] = {
    "sh": {"symbol": "000001.SH", "name": "上证指数"},
    "sz": {"symbol": "399001.SZ", "name": "深证成指"},
    "cyb": {"symbol": "399006.SZ", "name": "创业板指"},
    "hs300": {"symbol": "000300.SH", "name": "沪深300"},
    "kc50": {"symbol": "000688.SH", "name": "科创50"},
    "kczh": {"symbol": "000680.SH", "name": "科创综指"},
}

BENCHMARK = "000300.SH"


class FeedError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# 代码归一与限速
# --------------------------------------------------------------------------- #
def normalize_symbol(sym: str) -> str:
    """`600519` → `600519.SH`；已带后缀的原样返回。"""
    s = (sym or "").strip().upper()
    if not s:
        raise FeedError("空代码")
    if "." in s:
        code, mkt = s.split(".", 1)
        return f"{code}.{mkt}"
    if s.isdigit() and len(s) == 6:
        if s[0] == "6" or s[:3] in ("510", "511", "512", "513", "588"):
            return f"{s}.SH"
        if s[0] in ("0", "3"):
            return f"{s}.SZ"
        if s[0] in ("4", "8"):
            return f"{s}.BJ"
    raise FeedError(f"无法识别的代码: {sym}")


def to_secid(sym: str) -> str:
    code, mkt = normalize_symbol(sym).split(".")
    return f"{'1' if mkt == 'SH' else '0'}.{code}"


def to_tencent(sym: str) -> str:
    code, mkt = normalize_symbol(sym).split(".")
    return f"{ {'SH': 'sh', 'SZ': 'sz', 'BJ': 'bj'}[mkt] }{code}"


_MIN_INTERVAL = 0.35
_last_call = 0.0


def _throttle() -> None:
    global _last_call
    gap = time.time() - _last_call
    if gap < _MIN_INTERVAL:
        time.sleep(_MIN_INTERVAL - gap)
    _last_call = time.time()


def _http(url: str, referer: str = "https://quote.eastmoney.com/",
          timeout: float = 8.0, retries: int = 2) -> str:
    headers = {
        "User-Agent": UA, "Referer": referer, "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9", "Accept-Encoding": "identity",
        "Connection": "close",
    }
    last_err: Optional[Exception] = None
    for attempt in range(retries):
        _throttle()
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
            for enc in ("utf-8", "gbk"):
                try:
                    return raw.decode(enc)
                except Exception:
                    continue
            return raw.decode("utf-8", "replace")
        except Exception as e:      # noqa: BLE001 - 网络异常统一重试
            last_err = e
            if attempt < retries - 1:
                time.sleep(0.6 * (2 ** attempt))
    raise FeedError(f"请求失败: {urllib.parse.urlsplit(url).netloc} {type(last_err).__name__}")


def _f(v: Any, div: float = 1.0) -> Optional[float]:
    if v in (None, "-", ""):
        return None
    try:
        return float(v) / div
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# 实时快照
# --------------------------------------------------------------------------- #
_QUOTE_FIELDS = "f1,f2,f3,f4,f5,f6,f12,f13,f14,f15,f16,f17,f18,f20,f21,f86"


def _em_single(sym: str, timeout: float = 8.0) -> Dict[str, Any]:
    url = (f"https://push2.eastmoney.com/api/qt/stock/get?secid={to_secid(sym)}"
           f"&fields=f43,f44,f45,f46,f47,f48,f57,f58,f60,f169,f170,f86"
           f"&_={int(time.time() * 1000)}")
    d = (json.loads(_http(url, timeout=timeout)) or {}).get("data") or {}
    if d.get("f43") in (None, "-"):
        raise FeedError(f"东财无数据: {sym}")
    mkt = normalize_symbol(sym).split(".")[1]
    return {
        "symbol": f"{d.get('f57') or sym.split('.')[0]}.{mkt}", "name": d.get("f58"),
        "price": _f(d.get("f43")), "pct": _f(d.get("f170")), "change": _f(d.get("f169")),
        "volume": _f(d.get("f47")), "amount": _f(d.get("f48")),
        "high": _f(d.get("f44")), "low": _f(d.get("f45")), "open": _f(d.get("f46")),
        "prev_close": _f(d.get("f60")), "ts": str(d.get("f86") or ""), "source": "eastmoney",
    }


def _tx_quotes(syms: List[str], timeout: float = 8.0) -> List[Dict[str, Any]]:
    codes = ",".join(to_tencent(s) for s in syms)
    text = _http(f"https://qt.gtimg.cn/q={codes}", referer="https://gu.qq.com/", timeout=timeout)
    out: List[Dict[str, Any]] = []
    for line in text.split(";"):
        line = line.strip()
        if "~" not in line:
            continue
        f = line.split('="', 1)[-1].rstrip('"').split("~")
        if len(f) < 35:
            continue

        def g(i: int, div: float = 1.0) -> Optional[float]:
            try:
                v = float(f[i])
                return v / div if v else None
            except Exception:
                return None

        out.append({
            "symbol": normalize_symbol(f[2]), "name": f[1], "price": g(3),
            "prev_close": g(4), "open": g(5), "volume": g(6),
            "high": g(33), "low": g(34), "change": g(31), "pct": g(32),
            "amount": (g(37) or 0) * 1e4, "ts": f[30], "source": "tencent",
        })
    return out


def quotes(symbols: List[str], timeout: float = 8.0) -> List[Dict[str, Any]]:
    """批量快照：东财批量 → 东财单只 → 腾讯，单只失败不拖累整体。"""
    syms = [normalize_symbol(s) for s in symbols]
    out: List[Dict[str, Any]] = []
    missing: List[str] = []

    for i in range(0, len(syms), 50):
        chunk = syms[i:i + 50]
        secids = ",".join(to_secid(s) for s in chunk)
        url = (f"https://push2.eastmoney.com/api/qt/ulist.np/get?secids={secids}"
               f"&fields={_QUOTE_FIELDS}&fltt=2&invt=2&_={int(time.time() * 1000)}")
        try:
            data = json.loads(_http(url, timeout=timeout))
            diff = ((data or {}).get("data") or {}).get("diff") or []
            if isinstance(diff, dict):
                diff = list(diff.values())
            got = set()
            for row in diff:
                code = str(row.get("f12") or "")
                mkt = "SH" if str(row.get("f13")) == "1" else ("SZ" if str(row.get("f13")) == "0" else "BJ")
                out.append({
                    "symbol": f"{code}.{mkt}", "name": row.get("f14"),
                    "price": _f(row.get("f2")), "pct": _f(row.get("f3")), "change": _f(row.get("f4")),
                    "volume": _f(row.get("f5")), "amount": _f(row.get("f6")),
                    "high": _f(row.get("f15")), "low": _f(row.get("f16")),
                    "open": _f(row.get("f17")), "prev_close": _f(row.get("f18")),
                    "ts": str(row.get("f86") or ""), "source": "eastmoney",
                })
                got.add(f"{code}.{mkt}")
            missing.extend([s for s in chunk if s not in got])
        except Exception:
            missing.extend(chunk)

    for sym in list(missing):
        try:
            out.append(_em_single(sym, timeout=timeout))
            missing.remove(sym)
        except Exception:
            pass

    if missing:
        try:
            out.extend(_tx_quotes(missing, timeout=timeout))
            missing = []
        except Exception:
            pass

    for sym in missing:
        out.append({"symbol": sym, "error": "获取失败"})

    order = {s: i for i, s in enumerate(syms)}
    out.sort(key=lambda x: order.get(x["symbol"], 999))
    return out


# --------------------------------------------------------------------------- #
# 日线
# --------------------------------------------------------------------------- #
def _em_kline(sym: str, start: str) -> List[List[str]]:
    beg = start.replace("-", "") if start else "0"
    url = (f"https://push2his.eastmoney.com/api/qt/stock/kline/get?secid={to_secid(sym)}"
           f"&ut=fa5fd1943c7b386f172d6893dbfba10b&klt=101&fqt=1"
           f"&beg={beg}&end=20500101&fields1=f1,f2,f3,f4,f5,f6"
           f"&fields2=f51,f52,f53,f54,f55,f56,f57")
    klines = ((json.loads(_http(url)) or {}).get("data") or {}).get("klines") or []
    if not klines:
        raise FeedError("东财 K 线为空")
    return [str(k).split(",") for k in klines]


def _tx_kline(sym: str, count: int) -> List[List[str]]:
    url = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?"
           f"param={to_tencent(sym)},day,,,{count},qfq")
    node = ((json.loads(_http(url, referer="https://gu.qq.com/")) or {}).get("data") or {}).get(
        to_tencent(sym)) or {}
    rows = node.get("qfqday") or node.get("day") or []
    # 腾讯：[日期, 开, 收, 高, 低, 量]
    return [[r[0], r[1], r[2], r[3], r[4], r[5]] for r in rows if len(r) >= 6]


def daily_bars(symbol: str, start: str = "", count: int = 800) -> Dict[str, Any]:
    """拉日线。start 为空表示全量（首次），否则从该日期起增量。

    返回 {'symbol','source','rows':[{date,open,high,low,close,volume,amount}]}
    """
    sym = normalize_symbol(symbol)
    try:
        raw = _em_kline(sym, start)
        source = "eastmoney"
        rows = [{
            "date": r[0], "open": float(r[1]), "close": float(r[2]),
            "high": float(r[3]), "low": float(r[4]),
            "volume": float(r[5]), "amount": float(r[6]) if len(r) > 6 else None,
        } for r in raw]
    except Exception:
        raw = _tx_kline(sym, count)
        if not raw:
            raise FeedError(f"两个源都拿不到 K 线: {sym}")
        source = "tencent"
        rows = [{
            "date": r[0], "open": float(r[1]), "close": float(r[2]),
            "high": float(r[3]), "low": float(r[4]), "volume": float(r[5]), "amount": None,
        } for r in raw]

    if start:
        rows = [r for r in rows if r["date"] >= start]
    rows.sort(key=lambda r: r["date"])
    return {"symbol": sym, "source": source, "rows": rows}
