# -*- coding: utf-8 -*-
"""分阶段 CLI 入口。

用法：
  python run_pipeline.py fetch      # 阶段1：抓取智能表 -> 公司清单（自动标记已投递）
  python run_pipeline.py enrich     # 阶段2：企业信息补全（市值/上市状态/中国500强）
  python run_pipeline.py scrape     # 阶段3：抓岗位 JD + 简历匹配
  python run_pipeline.py match      # 阶段4：独立匹配（可选，默认由 scrape 完成）
  python run_pipeline.py select     # 阶段5：按匹配结果分 applicable/failed/skipped
  python run_pipeline.py fill       # 阶段6：浏览器逐家预填 + 人工确认
  python run_pipeline.py run        # 端到端：fetch -> enrich -> scrape -> select -> report
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core import config as config_mod
from llm.client import LLMError


def cmd_fetch(cfg) -> int:
    from pipeline.fetch import run_fetch
    summary = run_fetch(cfg)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print("CSV ->", summary.get("csv_path"))
    return 0


def cmd_enrich(cfg, limit=None, names=None, do_lists=True) -> int:
    from pipeline.enrich import run_enrich
    summary = run_enrich(cfg, limit=limit, names=names, do_lists=do_lists)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print("CSV ->", summary.get("csv_path"))
    return 0


def cmd_scrape(cfg, limit=None, names=None, force=False) -> int:
    from pipeline.scrape import run_scrape
    summary = run_scrape(cfg, limit=limit, names=names, force=force)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def cmd_match(cfg, limit=None) -> int:
    from pipeline.match import run_match
    summary = run_match(cfg, limit=limit)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print("CSV ->", summary.get("csv_path"))
    return 0


def cmd_fill(cfg, limit_each=10, auto_confirm=False) -> int:
    from pipeline.fill import run_fill
    summary = run_fill(cfg, limit_each=limit_each, auto_confirm=auto_confirm)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def cmd_select(cfg) -> int:
    """按匹配结果把公司分成 applicable / failed / skipped 三个 JSON。"""
    from pipeline.select import run_select
    summary = run_select(cfg)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def cmd_run(cfg, limit=None, names=None, do_lists=True, force=False) -> int:
    """端到端编排 fetch -> enrich -> scrape -> [match] -> select -> report。"""
    from pipeline.fetch import run_fetch
    from pipeline.enrich import run_enrich
    from pipeline.scrape import run_scrape
    from pipeline.match import run_match
    from pipeline.select import run_select
    from report.exporter import export_markdown

    results = {}
    steps = [
        ("fetch", lambda: run_fetch(cfg)),
        ("enrich", lambda: run_enrich(cfg, limit=limit, names=names, do_lists=do_lists)),
        ("scrape", lambda: run_scrape(cfg, limit=limit, names=names, force=force)),
    ]
    # scrape 阶段已经对两份简历做过匹配；match.enabled=false 时跳过独立匹配阶段
    if cfg.get("match", {}).get("enabled", True):
        steps.append(("match", lambda: run_match(cfg, limit=limit)))
    else:
        print("[run] match.enabled=false，跳过 match 阶段（scrape 已完成匹配）", flush=True)
    # 不再自动填写：按匹配结果分 applicable / failed / skipped 三个 JSON
    steps.append(("select", lambda: run_select(cfg)))
    for name, fn in steps:
        try:
            print("\n========== 阶段 %s 开始 ==========" % name, flush=True)
            results[name] = fn()
            print("[%s] 完成" % name, flush=True)
        except Exception as e:
            results[name] = {"error": str(e)}
            print("[%s] 失败：%s" % (name, e), flush=True)
            if name in ("fetch", "enrich"):
                break  # 前两阶段失败则后续无意义
    try:
        out = config_mod.abspath(cfg.get("paths", {}).get("data_output", "data/output"))
        results["report"] = export_markdown(out)
    except Exception as e:
        results["report"] = {"error": str(e)}
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="简历投递自动化 Agent 分阶段 CLI")
    parser.add_argument("stage",
                        choices=["fetch", "enrich", "scrape", "match", "select", "fill", "run"])
    parser.add_argument("--config", default=None, help="配置文件路径（默认仓库根目录 config.yaml）")
    parser.add_argument("--limit", type=int, default=None, help="仅处理前 N 家公司（测试用）")
    parser.add_argument("--names", default=None, help="逗号分隔的公司名过滤（测试用）")
    parser.add_argument("--no-lists", action="store_true", help="跳过中国500强名单匹配")
    parser.add_argument("--auto-confirm", action="store_true", help="fill 时跳过开始前的人工确认")
    parser.add_argument("--limit-each", type=int, default=10,
                        help="fill 阶段最多处理多少家公司（默认 10）")
    parser.add_argument("--force", action="store_true", help="忽略「已查找」记录，强制重查（scrape）")
    args = parser.parse_args(argv)

    cfg = config_mod.load_config(args.config)

    try:
        if args.stage == "fetch":
            return cmd_fetch(cfg)
        if args.stage == "enrich":
            names = [n.strip() for n in args.names.split(",")] if args.names else None
            return cmd_enrich(cfg, limit=args.limit, names=names, do_lists=not args.no_lists)
        if args.stage == "scrape":
            names = [n.strip() for n in args.names.split(",")] if args.names else None
            return cmd_scrape(cfg, limit=args.limit, names=names, force=args.force)
        if args.stage == "match":
            return cmd_match(cfg, limit=args.limit)
        if args.stage == "select":
            return cmd_select(cfg)
        if args.stage == "fill":
            return cmd_fill(cfg, limit_each=args.limit_each, auto_confirm=args.auto_confirm)
        if args.stage == "run":
            names = [n.strip() for n in args.names.split(",")] if args.names else None
            return cmd_run(cfg, limit=args.limit, names=names, do_lists=not args.no_lists,
                           force=args.force)
    except (LLMError, RuntimeError, FileNotFoundError, ValueError) as e:
        print("\n[错误] %s" % e)
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
