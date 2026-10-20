# -*- coding: utf-8 -*-
"""阶段1：抓取腾讯文档智能表，归一化为公司/岗位清单并落盘。

使用 asyncio 协程并发抓取：多个文档、多个子表并行请求（HTTP 是 I/O 密集，
协程并发能显著缩短总耗时）。底层仍是 requests，通过 asyncio.to_thread 并发化。
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import config as config_mod
from core.async_utils import run_async
from core.dedup import classify_company_names, load_applied_companies
from core.field_alias import normalize_row
from core.models import CANONICAL_FIELDS, CompanyRecord
from core.store import ensure_dir, write_csv, write_json, write_jsonl
from tencent_docs.smartsheet import fetch_doc

# 这些子表是「参考/面经/笔试/资料」类，不是投递目标，抓取时跳过。
EXCLUDE_SHEET_KEYS = ["面经", "真题", "笔试", "题库", "图片", "薪资", "资料", "面试", "参考", "转正"]


def _is_recruitment_sheet(name: str) -> bool:
    return not any(k in name for k in EXCLUDE_SHEET_KEYS)


def _log(msg: str) -> None:
    print(msg, flush=True)


async def _read_one(sem: asyncio.Semaphore, idx: int, doc, sheet: dict):
    """在协程里并发读取单个子表。"""
    async with sem:
        try:
            sd = await asyncio.to_thread(doc.read_sheet, sheet["id"])
            return idx, sd, None
        except Exception as e:
            return idx, None, str(e)


async def _fetch_all_async(docs_cfg: list, concurrency: int):
    """并发：列举每个文档的子表 + 读取所有子表。返回 (doc_results, meta, datas)。"""
    sem = asyncio.Semaphore(max(1, concurrency))

    async def load_doc(d):
        url = d["url"] if isinstance(d, dict) else d
        name = d.get("name", url) if isinstance(d, dict) else url
        doc = await asyncio.to_thread(fetch_doc, url)          # 轻，不发请求
        sheets = await asyncio.to_thread(lambda: doc.sheets)   # 发一次请求列出子表
        return name, url, doc, sheets

    _log("[fetch] 并发列举 %d 个文档的子表..." % len(docs_cfg))
    doc_results = await asyncio.gather(*[load_doc(d) for d in docs_cfg])
    for name, url, doc, sheets in doc_results:
        _log("[fetch]   文档「%s」：%d 个子表" % (name, len(sheets)))

    meta, tasks = [], []
    for name, url, doc, sheets in doc_results:
        for sheet in sheets:
            idx = len(meta)
            meta.append((name, url, sheet))
            tasks.append(_read_one(sem, idx, doc, sheet))

    _log("[fetch] 并发读取 %d 个子表（并发度 %d）..." % (len(tasks), concurrency))
    datas = [None] * len(tasks)
    errors = []
    for fut in asyncio.as_completed(tasks):
        idx, sd, err = await fut
        datas[idx] = sd
        name, _, sheet = meta[idx]
        if err:
            errors.append((sheet["name"], err))
            _log("[fetch]   [FAIL] 子表失败：%s（%s）" % (sheet["name"], err[:60]))
        else:
            _log("[fetch]   [OK] 子表完成：%s" % sheet["name"])
    return doc_results, meta, datas


def run_fetch(cfg) -> dict:
    """抓取全部子表，输出归一化公司清单（并标记是否已投递）。返回汇总信息。"""
    out_dir = config_mod.abspath(cfg.get("paths", {}).get("data_raw", "data/raw"))
    ensure_dir(out_dir)

    # 每次执行都重新读取「已投递」清单（用户可能更新），与源表实时抓取保持一致
    applied_path = config_mod.abspath(cfg.get("applied_xlsx", "data/applied/applied_companies.xlsx"))
    threshold = float(cfg.get("applied_threshold", 0.85))
    _log("[fetch] 读取已投递清单：%s" % applied_path)
    applied_list = load_applied_companies(applied_path)
    _log("[fetch] 已投递清单：%d 家公司（相似度阈值 %.2f）" % (len(applied_list), threshold))

    docs = cfg.get("docs") or []
    if isinstance(docs, dict):  # 兼容单文档配置
        docs = [docs]
    if not docs:
        raise ValueError("未配置抓取源：请在 config.yaml 的 docs 中填写腾讯文档智能表链接")
    concurrency = int(cfg.get("fetch", {}).get("concurrency", 8))

    doc_results, meta, datas = run_async(_fetch_all_async(docs, concurrency))

    # 同步归一化（CPU 密集但很快）
    all_records: list[CompanyRecord] = []
    summary = {"docs": [], "total_companies": 0,
               "applied_source": applied_path, "applied_count": len(applied_list),
               "already_applied": 0, "concurrency": concurrency}

    doc_info_map = {}
    for name, url, _doc, _sheets in doc_results:
        doc_info_map[name] = {"name": name, "url": url, "sheets": []}

    for (name, url, sheet), sd in zip(meta, datas):
        rows = sd.row_dicts() if sd is not None else []
        recruitment = _is_recruitment_sheet(sheet["name"])
        companies = 0
        if recruitment:
            for r in rows:
                rec = normalize_row(r, source_doc=name, sheet_name=sheet["name"], sheet_id=sheet["id"])
                if rec.company_name.strip():
                    all_records.append(rec)
                    companies += 1
        doc_info_map[name]["sheets"].append({
            "id": sheet["id"], "name": sheet["name"],
            "hidden": sheet["hidden"], "rows": len(rows),
            "recruitment": recruitment, "companies": companies,
        })

    for name, url, _doc, _sheets in doc_results:
        summary["docs"].append(doc_info_map[name])

    # 去重（同一 doc+sheet+record_id）
    seen = set()
    unique = []
    for r in all_records:
        key = (r.source_doc, r.sheet_id, r.record_id)
        if key not in seen:
            seen.add(key)
            unique.append(r)
    summary["total_companies"] = len(unique)
    _log("[fetch] 抓取完成：去重后共 %d 条公司/岗位记录" % len(unique))

    # 批量判定「是否已投递」：有 embedding 配置则用余弦相似度，否则字符相似度兜底
    unique_names = list(dict.fromkeys(r.company_name for r in unique))
    _log("[fetch] 判定已投递：%d 个公司名 vs %d 家已投递清单..." % (len(unique_names), len(applied_list)))
    store_path = config_mod.abspath(
        cfg.get("paths", {}).get("data_cache", "data/cache") + "/embeddings.db")
    classification, backend = classify_company_names(
        unique_names, applied_list, cfg, threshold, store_path)
    for r in unique:
        ok, match, _ = classification.get(r.company_name, (False, "", 0.0))
        r.already_applied = ok
        r.applied_match = match if ok else ""
    summary["dedup_backend"] = backend
    summary["already_applied"] = sum(1 for r in unique if r.already_applied)
    _log("[fetch] 判定完成（后端：%s）：已投递 %d 家 / 未投递 %d 家" % (
        backend, summary["already_applied"], len(unique) - summary["already_applied"]))

    csv_path = os.path.join(out_dir, "companies.csv")
    write_csv([r.to_dict() for r in unique], csv_path, fields=CANONICAL_FIELDS)
    write_jsonl([r.to_dict() for r in unique], os.path.join(out_dir, "companies.jsonl"))
    write_json(summary, os.path.join(out_dir, "fetch_summary.json"))
    _log("[fetch] 已写入：%s" % csv_path)

    summary["csv_path"] = csv_path
    return summary
