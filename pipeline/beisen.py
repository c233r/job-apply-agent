# -*- coding: utf-8 -*-
"""北森（Beisen）招聘站：直接调用岗位列表接口，返回含 JD 的岗位。

北森站既可能是 *.zhiye.com，也可能是企业自定义域名（如 career.xxx.com），
所以这里不靠域名判断，而是「试一下接口」：接口返回合法 JSON 就认为可用，
并按域名缓存结果，避免每个公司都重复探测。

接口：POST {base}/api/Jobad/GetJobAdPageList
     body {"PageIndex": 0, "PageSize": 500}   # PageIndex 从 0 开始
返回 Data[] 里每条含 Id / JobAdId / JobAdName / Duty / Require / LocNames / Category / Kind。
"""
from __future__ import annotations

from urllib.parse import urlparse

import requests

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# 域名 -> 是否北森站（探测结果缓存）
_PROBE_CACHE: dict[str, bool] = {}


def _host(url: str) -> str:
    return urlparse(url).netloc.lower()


def _base(url: str) -> str:
    p = urlparse(url)
    return "%s://%s" % (p.scheme or "https", p.netloc)


def is_beisen(url: str) -> bool:
    """是否是（已确认的）北森站。"""
    host = _host(url)
    return host.endswith("zhiye.com") or _PROBE_CACHE.get(host, False)


def fetch_jobs(url: str, page_size: int = 500, timeout: int = 30) -> tuple[list[dict], bool]:
    """拉取岗位，返回 (jobs, is_beisen)。

    is_beisen=False 表示该站不是北森接口站（调用方应回退到浏览器抓取）。
    """
    host = _host(url)
    if host.endswith("zhiye.com"):
        pass  # 已知北森，直接请求
    elif _PROBE_CACHE.get(host) is False:
        return [], False  # 之前探测过，不是北森

    base = _base(url)
    api = base + "/api/Jobad/GetJobAdPageList"
    headers = {
        "User-Agent": _UA,
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Referer": url,
    }
    try:
        r = requests.post(api, headers=headers,
                          json={"PageIndex": 0, "PageSize": page_size}, timeout=timeout)
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, dict) or "Data" not in data:
            raise ValueError("not beisen response")
    except Exception:  # noqa: BLE001
        _PROBE_CACHE[host] = False
        return [], False

    _PROBE_CACHE[host] = True
    rows = data.get("Data") or []
    out = []
    for j in rows:
        jid = j.get("Id") or ""
        out.append({
            "title": j.get("JobAdName") or "",
            "responsibilities": j.get("Duty") or "",
            "requirements": j.get("Require") or "",
            "location": ",".join(j.get("LocNames") or []),
            "url": (base + "/campus/detail?jobAdId=" + jid) if jid else url,
            "job_ad_id": j.get("JobAdId"),
            "category": j.get("Category") or "",
            "kind": j.get("Kind") or "",
        })
    return out, True
