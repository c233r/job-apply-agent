# -*- coding: utf-8 -*-
"""阶段5（select）：按「岗位 × 简历」匹配结果把公司分成三类，各写一个 JSON。

输入：
    data/jobs/jobs.jsonl            匹配通过（>= 阈值）的岗位
    data/jobs/scraped_companies.csv  所有尝试抓取的公司（name,url,status,reason,jobs_count）
    data/jobs/scrape_failures.jsonl  抓取失败/跳过的公司

输出（每条记录 = 一家公司）：
    data/output/applicable_companies.json  有岗位匹配上（每家公司包含全部匹配岗位，按 url 去重）
    data/output/failed_companies.json      抓到了公司/岗位，但没有任何岗位匹配上
    data/output/skipped_companies.json     链接没打开 / 站点未适配 / 主动跳过

applicable 每条记录字段：
    company_name  公司名
    url           公司链接（取最高分岗位的 url）
    max_score     该公司最高岗位分数（用于排序）
    positions     该公司所有匹配岗位列表，每个含 title/url/resume/score/tier

failed / skipped 每条记录字段：
    company_name  公司名
    url           链接
"""
from __future__ import annotations

import csv
import json
import os
from collections import defaultdict

from core import config as config_mod
from core.store import ensure_dir, write_json


def _load_jsonl(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    out: list[dict] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:  # noqa: BLE001
                continue
    return out


def _load_scraped(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    rows: list[dict] = []
    try:
        with open(path, encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                name = (row.get("name") or "").strip()
                if not name:
                    continue
                rows.append({
                    "company_name": name,
                    "url": (row.get("url") or "").strip(),
                    "status": (row.get("status") or "").strip().lower(),
                    "reason": (row.get("reason") or "").strip(),
                    "jobs_count": int(row.get("jobs_count") or 0),
                })
    except Exception as e:  # noqa: BLE001
        print("[select] 读取 scraped_companies.csv 失败：%s" % e, flush=True)
    return rows


def _record(name: str, url: str, position: str = "", resume: str = "") -> dict:
    return {"company_name": name, "position": position, "url": url, "resume": resume}


def run_select(cfg) -> dict:
    jobs_dir = config_mod.abspath(cfg.get("paths", {}).get("data_jobs", "data/jobs"))
    out_dir = config_mod.abspath(cfg.get("paths", {}).get("data_output", "data/output"))
    ensure_dir(out_dir)

    jobs = _load_jsonl(os.path.join(jobs_dir, "jobs.jsonl"))
    scraped = _load_scraped(os.path.join(jobs_dir, "scraped_companies.csv"))
    failures = _load_jsonl(os.path.join(jobs_dir, "scrape_failures.jsonl"))

    # 1) 有匹配岗位的公司：每个公司保留所有匹配岗位（按 url 去重，保留分数最高的）
    grouped: dict[str, list[dict]] = defaultdict(list)
    for j in jobs:
        name = (j.get("company_name") or "").strip()
        if not name:
            continue
        grouped[name].append(j)

    applicable = []
    for name, job_list in grouped.items():
        # 同公司内按 url 去重（同一个 url 可能因多份简历匹配而重复出现），保留分数最高的
        dedup: dict[str, dict] = {}
        for j in job_list:
            url = j.get("url", "")
            if url not in dedup or j.get("score", 0) > dedup[url].get("score", 0):
                dedup[url] = j
        # 同公司内岗位按分数降序
        positions_sorted = sorted(dedup.values(), key=lambda x: x.get("score", 0), reverse=True)
        max_score = positions_sorted[0].get("score", 0) if positions_sorted else 0
        applicable.append({
            "company_name": name,
            "url": positions_sorted[0].get("url", "") if positions_sorted else "",
            "max_score": max_score,
            "positions": [
                {
                    "title": j.get("title", ""),
                    "url": j.get("url", ""),
                    "resume": j.get("resume_name", ""),
                    "score": j.get("score", 0),
                    "tier": j.get("tier", ""),
                }
                for j in positions_sorted
            ],
        })
    # 公司间按最高岗位分数降序
    applicable.sort(key=lambda x: x["max_score"], reverse=True)

    # 2) 其余公司：抓到了但没匹配上 -> failed；链接没开/未适配/跳过 -> skipped
    failed, skipped = [], []
    seen = set(grouped)
    skip_status = {"skipped", "skip", "error", "failed", "unadapted", "dead"}
    for row in scraped:
        name = row["company_name"]
        if name in seen:
            continue
        seen.add(name)
        rec = _record(name, row["url"])
        if row["status"] in skip_status:
            skipped.append(rec)
        elif row["status"] == "done":
            failed.append(rec)
        else:
            skipped.append(rec)

    # 3) scrape_failures 里补充（scraped 表可能没记到）
    for f in failures:
        name = (f.get("company_name") or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        skipped.append(_record(name, f.get("url", "")))

    ap = os.path.join(out_dir, "applicable_companies.json")
    fl = os.path.join(out_dir, "failed_companies.json")
    sk = os.path.join(out_dir, "skipped_companies.json")
    write_json(applicable, ap)
    write_json(failed, fl)
    write_json(skipped, sk)

    print("\n[select] ===== 结果 =====", flush=True)
    print("[select] 可投递 %d 家（共 %d 个岗位）-> %s" % (
        len(applicable), sum(len(c["positions"]) for c in applicable), ap), flush=True)
    for r in applicable:
        print("    [OK] %s（%d 个岗位，最高 %.0f 分）" % (
            r["company_name"], len(r["positions"]), r["max_score"]), flush=True)
        for p in r["positions"]:
            print("        - %s | %.0f 分 | %s | 简历：%s" % (
                p["title"], p["score"], p["tier"], p["resume"] or "-"), flush=True)
    print("[select] 失败（无匹配岗位）%d 家 -> %s" % (len(failed), fl), flush=True)
    for r in failed:
        print("    [FAIL] %s" % r["company_name"], flush=True)
    print("[select] 跳过（链接未打开/未适配）%d 家 -> %s" % (len(skipped), sk), flush=True)
    for r in skipped:
        print("    [SKIP] %s" % r["company_name"], flush=True)

    return {
        "applicable": len(applicable),
        "failed": len(failed),
        "skipped": len(skipped),
        "applicable_json": ap,
        "failed_json": fl,
        "skipped_json": sk,
    }
