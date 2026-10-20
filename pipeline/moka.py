# -*- coding: utf-8 -*-
"""Moka（app.mokahr.com）招聘站：解析 init-data + AES 解密其接口，直接拿全部岗位 + JD。

原理：
- 页面里 <input id="init-data" value="{...}"> 是明文 JSON，含 org.id / siteId / aesIv；
- 接口 /api/outer/ats-apply/website/jobs/v2 的响应是 AES-CBC(Key=necromancer, IV=init-data.aesIv) 加密的；
- 解密后 data.jobs 每条含完整 jobDescription（HTML）。
"""
from __future__ import annotations

import base64
import html as html_mod
import json
import re

import requests
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def is_moka(url: str) -> bool:
    return "mokahr.com" in (url or "").lower()


def _aes_decrypt(data_b64: str, key: str, iv: str) -> str:
    key_b = key.encode("utf-8")
    iv_b = (iv or "").encode("utf-8")[:16].ljust(16, b"\x00")
    dec = Cipher(algorithms.AES(key_b), modes.CBC(iv_b)).decryptor()
    pt = dec.update(base64.b64decode(data_b64)) + dec.finalize()
    unp = padding.PKCS7(128).unpadder()
    return (unp.update(pt) + unp.finalize()).decode("utf-8")


def _parse_body(body: dict, iv: str):
    """接口响应：{data, necromancer} -> 解密后的 dict；未加密则原样返回。"""
    if isinstance(body, dict) and body.get("necromancer") and body.get("data"):
        try:
            return json.loads(_aes_decrypt(body["data"], body["necromancer"], iv))
        except Exception:  # noqa: BLE001
            return body
    return body


def _parse_init(page_html: str) -> dict | None:
    m = re.search(r'id="init-data"[^>]*value="([^"]*)"', page_html, re.S)
    if not m:
        return None
    try:
        return json.loads(html_mod.unescape(m.group(1)))
    except Exception:  # noqa: BLE001
        return None


def _strip_html(s: str) -> str:
    s = re.sub(r"<br\s*/?>", "\n", s or "", flags=re.I)
    s = re.sub(r"</p>|</div>|</li>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = html_mod.unescape(s)
    return re.sub(r"\n{2,}", "\n", s).strip()


def _location(job: dict) -> str:
    parts = []
    for loc in job.get("locations") or []:
        c = loc.get("cityName") or ""
        p = loc.get("provinceName") or ""
        parts.append("·".join(x for x in (p, c) if x))
    return ",".join(dict.fromkeys(p for p in parts if p))


def fetch_jobs(url: str, max_pages: int = 20, page_size: int = 100,
               timeout: int = 30) -> tuple[list[dict], bool]:
    """返回 (jobs, is_moka)。is_moka=False 表示页面结构不匹配（调用方可回退浏览器）。"""
    headers = {"User-Agent": _UA, "Accept": "text/html,application/xhtml+xml,*/*",
               "Accept-Language": "zh-CN,zh;q=0.9"}
    try:
        r = requests.get(url, headers=headers, timeout=timeout)
        init = _parse_init(r.text)
    except Exception:  # noqa: BLE001
        return [], False
    if not init:
        return [], False

    org = (init.get("org") or {}).get("id") or ""
    site = init.get("siteId") or ""
    iv = init.get("aesIv") or ""
    base = "https://app.mokahr.com"
    api = base + "/api/outer/ats-apply/website/jobs/v2"
    api_headers = {
        "User-Agent": _UA, "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json", "Referer": url,
    }
    out: list[dict] = []
    total = 0
    for page in range(1, max_pages + 1):
        body = {"orgId": org, "siteId": site, "needStat": True,
                "locale": "zh-CN", "page": page, "size": page_size}
        try:
            r2 = requests.post(api, headers=api_headers, json=body, timeout=timeout)
            d = _parse_body(r2.json(), iv)
        except Exception:  # noqa: BLE001
            break
        data = d.get("data") or {}
        jobs = data.get("jobs") or []
        total = (data.get("jobStats") or {}).get("total") or total
        if not jobs:
            break
        for j in jobs:
            jid = j.get("id") or ""
            jd = _strip_html(j.get("jobDescription") or "")
            out.append({
                "title": j.get("title") or "",
                "responsibilities": jd,
                "requirements": "",
                "location": _location(j),
                "url": "%s#/job/%s" % (url.split("#")[0], jid) if jid else url,
                "job_id": jid,
                "department": (j.get("department") or {}).get("name") or "",
                "education": j.get("education") or "",
            })
        if total and len(out) >= total:
            break
    return out, True
