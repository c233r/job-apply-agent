# -*- coding: utf-8 -*-
"""飞书招聘站（*.jobs.feishu.cn）直连接口。

流程（来自浏览器抓包）：
1. GET 页面，拿到站点 base 与 path（URL 第一段，如 /840753/...）；
2. POST {base}/api/v1/csrf/token  {"portal_entrance":1} -> data.token + cookie atsx-csrf-token；
3. POST {base}/api/v1/search/job/posts  （带 x-csrf-token / website-path 头）
   body {"keyword":"","limit":100,"offset":0,...,"portal_type":6,...}
   -> data.job_post_list[] 含 title / description / requirement。
"""
from __future__ import annotations

from urllib.parse import urlparse

import requests

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36")


def is_feishu(url: str) -> bool:
    return "jobs.feishu.cn" in (url or "").lower()


def _site(url: str) -> tuple[str, str]:
    p = urlparse(url)
    base = "%s://%s" % (p.scheme or "https", p.netloc)
    segs = [s for s in (p.path or "").split("/") if s]
    path = ""
    if segs:
        path = segs[0] if segs[0].isdigit() else segs[0]
    return base, path


def _headers(url: str, path: str) -> dict:
    return {
        "User-Agent": _UA,
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Referer": url,
        "website-path": path,
        "portal-channel": "saas-career",
        "portal-platform": "pc",
        "accept-language": "zh-CN",
    }


def fetch_jobs(url: str, page_size: int = 100, max_pages: int = 20,
               timeout: int = 30) -> tuple[list[dict], bool]:
    """返回 (jobs, ok)。ok=False 表示该站结构不匹配。"""
    base, path = _site(url)
    if not path:
        return [], False
    headers = _headers(url, path)
    s = requests.Session()
    try:
        s.get(url, headers={"User-Agent": _UA}, timeout=timeout)
        r1 = s.post(base + "/api/v1/csrf/token", headers=headers,
                    json={"portal_entrance": 1}, timeout=timeout)
        token = (r1.json().get("data") or {}).get("token") or ""
    except Exception:  # noqa: BLE001
        return [], False
    if not token:
        return [], False

    api_headers = dict(headers)
    api_headers["x-csrf-token"] = token
    out: list[dict] = []
    offset = 0
    total = 0
    for _ in range(max_pages):
        body = {
            "keyword": "", "limit": page_size, "offset": offset,
            "job_category_id_list": [], "tag_id_list": [], "location_code_list": [],
            "subject_id_list": [], "recruitment_id_list": [], "portal_type": 6,
            "job_function_id_list": [], "storefront_id_list": [], "portal_entrance": 1,
        }
        try:
            r2 = s.post(base + "/api/v1/search/job/posts", headers=api_headers,
                        json=body, timeout=timeout)
            d = r2.json()
        except Exception:  # noqa: BLE001
            break
        if d.get("code") != 0:
            break
        data = d.get("data") or {}
        lst = data.get("job_post_list") or []
        total = data.get("count") or data.get("total") or total
        if not lst:
            break
        for j in lst:
            jid = j.get("id") or ""
            city = ""
            ci = j.get("city_info")
            if isinstance(ci, dict):
                city = ci.get("name") or ""
            elif isinstance(ci, list) and ci and isinstance(ci[0], dict):
                city = ci[0].get("name") or ""
            rt = j.get("recruit_type") or {}
            out.append({
                "title": j.get("title") or "",
                "responsibilities": j.get("description") or "",
                "requirements": j.get("requirement") or "",
                "location": city,
                "url": ("%s/%s/position/%s/detail" % (base, path, jid)) if jid else url,
                "job_id": jid,
                "recruit_type": rt.get("name") or "",
            })
        if total and len(out) >= total:
            break
        offset += page_size
    return out, True
