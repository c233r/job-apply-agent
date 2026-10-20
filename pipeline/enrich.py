# -*- coding: utf-8 -*-
"""阶段2：企业信息补全（市值 / 上市状态 / 中国500强）。

用协程并发：每家公司内部「市值 + 500强」两路并发，公司之间也并发
（受 enrich.concurrency 限流）。500强仅查本地名单，不调用 LLM。
"""
from __future__ import annotations

import asyncio
import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import config as config_mod
from core.async_utils import run_async
from core.cache import KV
from core.dedup import EmbeddingStore, clean_company_name
from core.store import ensure_dir, write_csv, write_json
from enrichment.market_cap import enrich_market_cap_async
from enrichment.lists import ListMatcher, load_local_list
from llm.embeddings import build_embedding_client

ENRICH_FIELDS = [
    "listed", "market_cap_yi", "board", "stock_code", "security_name",
    "china500", "china500_evidence",
]


async def enrich_one_async(name: str, kv: KV, do_lists: bool,
                           china500_matcher=None) -> dict:
    """并发补全单家公司（市值 + 500强 同时查），优先用缓存 / 本地名单。

    500强：本地名单匹配（配了 embedding 则用余弦语义，否则字符相似度），
    命中=是(1)，未命中=否(0)。
    """
    raw_name = name
    key = clean_company_name(name)
    cached = kv.get_company(raw_name) or kv.get_company(key)
    result = dict.fromkeys(ENRICH_FIELDS)

    tasks = []  # (label, coro)
    if not (cached and cached.get("listed") in (0, 1)):
        tasks.append(("mc", enrich_market_cap_async(key)))
    # 500强：本地名单匹配，查不到就是「非500强」；只有缓存已是明确 0/1 才跳过
    if not (cached and cached.get("china500") in (0, 1)):
        if do_lists and china500_matcher is not None and china500_matcher.pairs:
            tasks.append(("china500", asyncio.to_thread(china500_matcher.match, key)))
        else:
            result["china500"] = -1

    async def settle(label, coro):
        try:
            return label, await coro, None
        except Exception as e:
            return label, None, str(e)

    if tasks:
        for label, value, err in await asyncio.gather(*[settle(l, c) for l, c in tasks]):
            if label == "mc":
                if err or not value:
                    result["listed"] = -1
                else:
                    result["listed"] = value["listed"]
                    result["market_cap_yi"] = value.get("market_cap_yi")
                    result["board"] = value.get("board", "")
                    result["stock_code"] = value.get("stock_code", "")
                    result["security_name"] = value.get("security_name", "")
            elif label == "china500":
                if err:
                    result["china500"] = -1
                    result["china500_evidence"] = "查询失败: %s" % err
                else:
                    found, matched, score = value
                    result["china500"] = 1 if found else 0
                    result["china500_evidence"] = (
                        "本地名单命中：%s（%.2f）" % (matched, score) if found else "未命中本地名单")

    merged = dict(cached or {})
    for k, v in result.items():
        if v is not None:
            merged[k] = v
    merged.pop("name", None)  # name 由 set_company 的位置参数指定，避免重复
    kv.set_company(raw_name, **merged)
    kv.set_company(key, **merged)
    return merged


async def _enrich_all_async(unique_rows: list, kv: KV, do_lists: bool,
                            concurrency: int, china500_matcher=None) -> list:
    """公司级并发补全，返回与 unique_rows 对齐的结果列表。"""
    sem = asyncio.Semaphore(max(1, concurrency))
    total = len(unique_rows)
    results = [None] * total

    async def one(i, r):
        async with sem:
            name = r.get("company_name", "")
            if not name:
                return i, {}
            return i, await enrich_one_async(name, kv, do_lists, china500_matcher)

    done = 0
    for fut in asyncio.as_completed([one(i, r) for i, r in enumerate(unique_rows)]):
        i, e = await fut
        results[i] = e
        done += 1
        print("[enrich] (%d/%d) 完成 -> 上市=%s 市值=%s亿 500强=%s" % (
            done, total, e.get("listed"), e.get("market_cap_yi"),
            e.get("china500")), flush=True)
    return results


def run_enrich(cfg, limit: int | None = None, names: list[str] | None = None,
               do_lists: bool = True) -> dict:
    raw_csv = config_mod.abspath(cfg.get("paths", {}).get("data_raw", "data/raw") + "/companies.csv")
    out_dir = config_mod.abspath(cfg.get("paths", {}).get("data_enriched", "data/enriched"))
    db_path = config_mod.abspath(cfg.get("paths", {}).get("data_cache", "data/cache") + "/cache.db")
    ensure_dir(out_dir)
    kv = KV(db_path)
    concurrency = int(cfg.get("enrich", {}).get("concurrency", 8))

    if not os.path.exists(raw_csv):
        raise FileNotFoundError("未找到 %s，请先运行: python run_pipeline.py fetch" % raw_csv)

    with open(raw_csv, "r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))

    if names:
        rows = [r for r in rows if any(n in (r.get("company_name") or "") for n in names)]
    if limit:
        rows = rows[:limit]

    # 按公司名去重
    seen = set()
    unique_rows = []
    for r in rows:
        n = r.get("company_name", "")
        if n and n not in seen:
            seen.add(n)
            unique_rows.append(r)

    # 本地 500 强名单：查不到即判「非500强」，不调用 LLM
    lists_cfg = cfg.get("lists", {}) or {}
    china500_list = load_local_list(config_mod.abspath(
        lists_cfg.get("china500", "data/lists/china500.txt")))
    # 名单匹配器：配了 embedding 用余弦语义匹配，否则字符相似度
    emb_store = EmbeddingStore(config_mod.abspath(
        cfg.get("paths", {}).get("data_cache", "data/cache") + "/embeddings.db"))
    embed_client = build_embedding_client(cfg)
    list_threshold = float(lists_cfg.get("threshold", 0.85))
    # 先把所有公司名的向量一次性预算好，再做纯余弦匹配（不边算边匹配）
    query_names = [clean_company_name(r.get("company_name", ""))
                   for r in unique_rows if r.get("company_name")]
    china500_matcher = ListMatcher(china500_list, embed_client, list_threshold,
                                   emb_store, query_names=query_names)
    print("[enrich] 本地中国500强名单：%d 家；匹配后端：%s" % (
        len(china500_list), china500_matcher.backend()), flush=True)

    print("[enrich] 共 %d 行，去重后 %d 家公司；协程并发度 %d，开始补全（市值/500强）..."
          % (len(rows), len(unique_rows), concurrency), flush=True)

    results = run_async(_enrich_all_async(
        unique_rows, kv, do_lists, concurrency, china500_matcher))

    out_rows = []
    stats = {"total": 0, "listed": 0, "unlisted": 0, "china500_yes": 0}
    for r, e in zip(unique_rows, results):
        if not r.get("company_name"):
            continue
        merged = dict(r)
        merged.update(e or {})
        out_rows.append(merged)
        stats["total"] += 1
        if (e or {}).get("listed") == 1:
            stats["listed"] += 1
        elif (e or {}).get("listed") == 0:
            stats["unlisted"] += 1
        if (e or {}).get("china500") == 1:
            stats["china500_yes"] += 1

    fields = list(rows[0].keys()) + ENRICH_FIELDS
    csv_path = os.path.join(out_dir, "companies_enriched.csv")
    write_csv(out_rows, csv_path, fields=fields)
    print("[enrich] 完成：上市 %d / 未上市 %d；已写入 %s" % (
        stats["listed"], stats["unlisted"], csv_path), flush=True)

    summary = {
        "total_companies": stats["total"],
        "listed": stats["listed"],
        "unlisted": stats["unlisted"],
        "china500_yes": stats["china500_yes"],
        "lists_enabled": do_lists and bool(china500_list),
        "concurrency": concurrency,
        "csv_path": csv_path,
    }
    write_json(summary, os.path.join(out_dir, "enrich_summary.json"))
    return summary
