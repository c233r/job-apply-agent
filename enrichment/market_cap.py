# -*- coding: utf-8 -*-
"""市值与上市状态查询。

数据源：
1. 东方财富「搜索建议」接口：公司名 -> 股票代码/市场/QuoteID；
2. 腾讯行情 qt.gtimg.cn：QuoteID -> 总市值(亿)、流通市值(亿)、股价、市盈率。

均无需登录、无需额外依赖（仅 requests）。
"""
from __future__ import annotations

import requests

H = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
    "Referer": "https://www.eastmoney.com/",
}

SEARCH_URL = "https://searchapi.eastmoney.com/api/suggest/get"
QUOTE_URL = "https://qt.gtimg.cn/q="

# eastmoney QuoteID 前缀 -> (腾讯代码前缀, 板块名)
MKT_MAP = {
    "1": ("sh", "沪A"),
    "0": ("sz", "深A"),
    "116": ("hk", "港股"),
    "105": ("us", "美股"),
    "90": ("bj", "北交所"),
}

# 仅这些 Classify 视为「真正的股票」，排除债券/基金/板块/新三板/粉单等
EQUITY_CLASSIFY = {"AStock", "HK", "US", "NASDAQ", "NYSE", "AMEX"}


def eastmoney_search(name: str, count: int = 10) -> list[dict]:
    """按名称搜索股票，返回候选列表（原始字段）。"""
    try:
        r = requests.get(SEARCH_URL, params={"input": name, "type": "14", "count": str(count)},
                         headers=H, timeout=20)
        r.raise_for_status()
        t = r.text.strip()
        if t.startswith("(") and t.endswith(")"):
            t = t[1:-1]
        import json
        data = json.loads(t)
        return data.get("QuotationCodeTable", {}).get("Data") or []
    except Exception:
        return []


def _quote_id_to_tencent(quote_id: str) -> str | None:
    if "." not in quote_id:
        return None
    mkt, code = quote_id.split(".", 1)
    entry = MKT_MAP.get(mkt)
    if not entry:
        return None
    return entry[0] + code


def _score(cand: dict, name: str) -> int:
    n = (cand.get("Name") or "").strip()
    q = name.strip()
    s = 0
    if n == q:
        s += 100
    elif n.startswith(q) or q.startswith(n):
        s += 50
    elif q in n or n in q:
        s += 30
    else:
        s -= 50

    # 非股票类型（债券/基金/板块/新三板/粉单）直接排除
    cls = cand.get("Classify") or ""
    if cls not in EQUITY_CLASSIFY:
        s -= 100
        return s

    mkt = str(cand.get("MktNum") or cand.get("MarketType") or "")
    if mkt in ("1", "0"):      # A股
        s += 20
    elif mkt == "116":          # 港股
        st = str(cand.get("SecurityType") or "")
        s += (15 if st == "19" else -20)  # 19=港股普通股，避免 WR/R 柜台/权证
    elif mkt in ("105", "106", "107"):  # 美股
        s += 10
    else:
        s -= 30
    return s


def tencent_quote(tencent_code: str) -> dict | None:
    """腾讯行情，返回 {name, price, market_cap_yi, float_market_cap_yi, pe}。"""
    try:
        r = requests.get(QUOTE_URL + tencent_code, headers={**H, "Referer": "https://gu.qq.com/"}, timeout=15)
        r.encoding = "gbk"
        t = r.text
        if '"' in t:
            t = t.split('"')[1]
        f = t.split("~")
        if len(f) < 46 or not f[1]:
            return None
        def num(i):
            try:
                return float(f[i])
            except (ValueError, IndexError):
                return None
        return {
            "name": f[1],
            "price": num(3),
            "float_market_cap_yi": num(44),
            "market_cap_yi": num(45),
            "pe": num(39),
        }
    except Exception:
        return None


def enrich_market_cap(name: str) -> dict:
    """根据公司名补上市状态与市值。返回字段见下。"""
    out = {
        "listed": -1,            # 1 上市 / 0 未上市 / -1 未知
        "market_cap_yi": None,   # 总市值(亿)
        "board": "",
        "stock_code": "",
        "security_name": "",
    }
    cands = eastmoney_search(name)
    equity = [c for c in cands if (c.get("Classify") or "") in EQUITY_CLASSIFY]
    if not equity:
        out["listed"] = 0
        return out

    # 取评分最高的候选；低于阈值视为不可靠匹配（按未上市处理）
    best = max(equity, key=lambda c: _score(c, name))
    best_score = _score(best, name)
    if best_score < 50:
        out["listed"] = 0
        return out

    quote_id = best.get("QuoteID")
    tcode = _quote_id_to_tencent(quote_id) if quote_id else None
    if tcode is None:
        out["listed"] = 0
        return out

    out["stock_code"] = best.get("Code", "")
    out["security_name"] = best.get("Name", "")
    mkt = str(best.get("MktNum") or best.get("MarketType") or "")
    out["board"] = MKT_MAP.get(mkt, ("", ""))[1]

    q = tencent_quote(tcode)
    if q:
        out["listed"] = 1
        out["market_cap_yi"] = q["market_cap_yi"]
        out["float_market_cap_yi"] = q.get("float_market_cap_yi")
    elif best_score >= 30:
        # 搜到代码但行情拿不到，视为已上市但市值未知
        out["listed"] = 1
    else:
        out["listed"] = 0
    return out


async def enrich_market_cap_async(name: str) -> dict:
    """协程版市值查询（供 enrich 阶段并发调用）。"""
    import asyncio
    return await asyncio.to_thread(enrich_market_cap, name)
