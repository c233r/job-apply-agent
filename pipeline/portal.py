# -*- coding: utf-8 -*-
"""通用「招聘门户」适配器（Vue SPA + /prod-api/recruit/portal/listPosition）。

很多国企/集团招聘门户用同一套 SaaS：
    POST {base}/prod-api/recruit/portal/listPosition  {"pageNum":1,"pageSize":100}
返回明文 JSON：data.list[] 含 positionName / workDuty / qualification / workPlace 等。
"""
from __future__ import annotations

import requests

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def _base(url: str) -> str:
    from urllib.parse import urlparse
    p = urlparse(url)
    return "%s://%s" % (p.scheme or "https", p.netloc)


def fetch_jobs(url: str, page_size: int = 100, max_pages: int = 20,
               timeout: int = 30) -> tuple[list[dict], bool]:
    """尝试通用门户接口，返回 (jobs, ok)。ok=False 表示该站不适用。"""
    base = _base(url)
    api = base + "/prod-api/recruit/portal/listPosition"
    headers = {
        "User-Agent": _UA, "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json", "Referer": url,
    }
    out: list[dict] = []
    total = 0
    for page in range(1, max_pages + 1):
        try:
            r = requests.post(api, headers=headers,
                              json={"pageNum": page, "pageSize": page_size}, timeout=timeout)
            d = r.json()
        except Exception:  # noqa: BLE001
            return [], False
        if d.get("code") != 200:
            return [], False
        data = d.get("data") or {}
        lst = data.get("list") or []
        total = data.get("total") or total
        if not lst:
            break
        for j in lst:
            out.append({
                "title": j.get("positionName") or j.get("positionNameEn") or "",
                "responsibilities": j.get("workDuty") or "",
                "requirements": j.get("qualification") or "",
                "location": j.get("workPlace") or "",
                "url": url,
                "job_id": j.get("positionId"),
                "department": j.get("departmentName") or "",
                "education": j.get("education") or "",
            })
        if total and len(out) >= total:
            break
    return out, True
