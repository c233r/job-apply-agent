# -*- coding: utf-8 -*-
"""阶段4：简历解析 + 匹配度检测（协程并发匹配多个岗位）。"""
from __future__ import annotations

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import config as config_mod
from core.async_utils import run_async
from core.store import ensure_dir, write_csv, write_json
from llm.client import build_client
from matching.matcher import match_async
from matching.preference import tier
from resume.parser import extract_text


def _load_jobs(cfg) -> list[dict]:
    path = config_mod.abspath(cfg.get("paths", {}).get("data_jobs", "data/jobs") + "/jobs.jsonl")
    if not os.path.exists(path):
        raise FileNotFoundError("未找到 %s，请先运行: python run_pipeline.py scrape" % path)
    jobs = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                jobs.append(json.loads(line))
    return jobs


async def _match_all_async(resume_text: str, jobs: list, llm, concurrency: int,
                           pref: dict | None = None) -> list:
    sem = asyncio.Semaphore(max(1, concurrency))
    total = len(jobs)
    results = [None] * total

    async def one(i, job):
        async with sem:
            return i, await match_async(resume_text, job, llm, pref)

    done = 0
    for fut in asyncio.as_completed([one(i, j) for i, j in enumerate(jobs)]):
        i, r = await fut
        results[i] = r
        done += 1
        print("[match] (%d/%d) %s | %s -> %s 分" % (
            done, total, r.company_name, r.job_title, r.score), flush=True)
    return results


def run_match(cfg, limit: int | None = None) -> dict:
    resume_path = config_mod.abspath(cfg.get("resume", {}).get("path", "resume.pdf"))
    out_dir = config_mod.abspath(cfg.get("paths", {}).get("data_output", "data/output"))
    ensure_dir(out_dir)
    concurrency = int(cfg.get("match", {}).get("concurrency", 8))

    print("[match] 解析简历：%s" % resume_path, flush=True)
    resume_text = extract_text(resume_path)
    print("[match] 简历解析完成（%d 字），开始并发匹配（并发度 %d）..." % (
        len(resume_text), concurrency), flush=True)
    llm = build_client(cfg)
    jobs = _load_jobs(cfg)
    if limit:
        jobs = jobs[:limit]

    pref = cfg.get("preference", {}) or {}
    results = run_async(_match_all_async(resume_text, jobs, llm, concurrency, pref))

    # 排序 + 阈值过滤
    results.sort(key=lambda r: r.score, reverse=True)
    min_score = float(cfg.get("matching", {}).get("min_score", 0))
    passed = [r for r in results if r.score >= min_score]
    print("[match] 匹配完成：%d 个岗位中 %d 个达到 %.0f 分阈值" % (
        len(results), len(passed), min_score), flush=True)

    rows = [{
        "company_name": r.company_name, "job_title": r.job_title,
        "score": r.score, "tier": tier(r.score),
        "matched": r.matched, "gaps": r.gaps, "advice": r.advice,
    } for r in results]
    csv_path = os.path.join(out_dir, "match_report.csv")
    write_csv(rows, csv_path, fields=["company_name", "job_title", "score", "matched", "gaps", "advice"])
    write_json([r.__dict__ for r in results], os.path.join(out_dir, "match_report.json"))

    summary = {
        "resume_path": resume_path,
        "total_jobs": len(results),
        "passed_threshold": len(passed),
        "min_score": min_score,
        "concurrency": concurrency,
        "top": [{"company_name": r.company_name, "job_title": r.job_title, "score": r.score}
                for r in results[:5]],
        "csv_path": csv_path,
    }
    write_json(summary, os.path.join(out_dir, "match_summary.json"))
    return summary
