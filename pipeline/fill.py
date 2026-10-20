# -*- coding: utf-8 -*-
"""阶段5：自动填写投递（选岗 → 浏览器逐家预填 → 人工检查确认 → 记录复用）。"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import config as config_mod
from core.cache import KV
from core.store import ensure_dir, write_json
from llm.client import build_client
from resume.parser import extract_text, parse_profile
from autofill.attachments import attachment_files
from autofill.fill_agent import FillAgent


def select_from_jobs(cfg, limit: int = 10) -> list[dict]:
    """直接从 scrape 产出的 jobs.jsonl 选岗位（已含匹配分/档位/匹配到的简历），不再重复 LLM 匹配。

    每家公司只保留分数最高的一个岗位，按分数降序取前 limit 家。
    """
    path = config_mod.abspath(cfg.get("paths", {}).get("data_jobs", "data/jobs") + "/jobs.jsonl")
    if not os.path.exists(path):
        return []
    jobs = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    jobs.append(json.loads(line))
                except Exception:  # noqa: BLE001
                    continue
    best = {}
    for j in jobs:
        name = j.get("company_name") or ""
        if not name:
            continue
        if name not in best or j.get("score", 0) > best[name].get("score", 0):
            best[name] = j
    ordered = sorted(best.values(), key=lambda x: x.get("score", 0), reverse=True)
    return [{
        "company_name": j.get("company_name", ""),
        "position": j.get("title", ""),
        "score": j.get("score", 0),
        "tier": j.get("tier", ""),
        "url": j.get("url", ""),
        "resume_path": j.get("resume_path", ""),
        "resume_name": j.get("resume_name", ""),
    } for j in ordered[:limit]]


def run_fill(cfg, limit_each: int = 10, auto_confirm: bool = False) -> dict:
    """主流程：选岗 -> 浏览器逐家预填 -> 人工检查确认 -> 记录结果。

    limit_each = 本次最多处理多少家公司（按匹配分从高到低取）。
    """
    resume_path = config_mod.abspath(cfg.get("resume", {}).get("path", "resume.pdf"))
    out_dir = config_mod.abspath(cfg.get("paths", {}).get("data_output", "data/output"))
    db_path = config_mod.abspath(cfg.get("paths", {}).get("data_cache", "data/cache") + "/cache.db")
    ensure_dir(out_dir)
    kv = KV(db_path)
    llm = build_client(cfg)

    print("[fill] 解析简历：%s" % resume_path, flush=True)
    resume_text = extract_text(resume_path)
    profile = parse_profile(resume_text, llm)
    print("[fill] 简历画像：%s / %s / %s" % (
        profile.get("name", ""), profile.get("degree", ""), profile.get("school", "")), flush=True)

    fill_cfg = cfg.get("fill", {}) or {}
    # 直接用 scrape 阶段的结果（已含匹配分/档位/匹配到的简历），不再重复 LLM 匹配
    items = select_from_jobs(cfg, limit_each)
    total = len(items)
    print("[选岗] 使用 scrape 结果：%d 家（已含匹配分/简历）" % total)
    if total == 0:
        return {"selected": 0, "reason": "无匹配公司"}

    print("[人工确认] 以下 %d 家公司将进入浏览器逐家投递（按匹配分从高到低）：" % total)
    for c in items:
        print("  [%s档] %s | %s | %.0f 分 | 简历：%s" % (
            c.get("tier", ""), c["company_name"], c["position"],
            c.get("score", 0), c.get("resume_name") or "默认"))
    if not auto_confirm:
        input("  确认开始？回车继续，Ctrl+C 退出。")

    # 浏览器逐家填写（需要 Playwright）
    from browser.browser import BrowserSession
    bc = cfg.get("browser", {})
    session = BrowserSession(
        channel=bc.get("channel", "chrome"),
        headless=bc.get("headless", False),
        user_data_dir=config_mod.abspath(bc.get("user_data_dir", "data/browser_profile")),
        timeout_ms=bc.get("timeout_ms", 30000),
    )
    agent_cfg = cfg.get("agent", {}) or {}
    keep_pages = bool(fill_cfg.get("keep_pages", True))
    # fill 阶段单独开关（默认用图像识别）
    fill_use_screenshot = bool(fill_cfg.get("use_screenshot",
                                            agent_cfg.get("use_screenshot", True)))
    # 视觉模型若单独配了供应商就用它，否则复用 llm
    vision_client = None
    v_base = agent_cfg.get("vision_base_url") or ""
    v_key = agent_cfg.get("vision_api_key") or ""
    if v_base and v_key:
        from llm.client import DeepSeekClient
        vision_client = DeepSeekClient(
            v_key, v_base,
            model=agent_cfg.get("vision_model") or "deepseek-flash",
            max_tokens=int(agent_cfg.get("max_tokens", 0) or 0))
    attach_dir = config_mod.abspath(cfg.get("resume", {}).get("attachments_dir", "data/attachments"))
    atts = attachment_files(attach_dir)

    applied = []
    analysis_rows: list[dict] = []
    try:
        print("[fill] 正在启动浏览器（channel=%s, headless=%s）..." % (
            bc.get("channel", "chrome"), bc.get("headless", False)), flush=True)
        session.start()
        print("[fill] 浏览器已启动，开始逐家填写...", flush=True)
        # ---- 每家直接交给「截图 + 多模态模型」agent（图像识别判断页面） ----
        context_path = config_mod.abspath(
            cfg.get("paths", {}).get("data_output", "data/output") + "/fill_context.json")
        answers = {}
        try:
            if os.path.exists(context_path):
                answers = (json.load(open(context_path, encoding="utf-8")) or {}).get("answers") or {}
                if answers:
                    print("[上下文] 复用已保存的答案 %d 个" % len(answers), flush=True)
        except Exception:  # noqa: BLE001
            answers = {}
        if answers:
            profile = dict(profile)
            profile.update(answers)  # 上下文文档里的补充信息并入画像
        # ---- 逐家：直接交给「截图 + 模型指挥」的 agent（图像识别） ----
        for c in items:
            if not c.get("url"):
                print("[fill] %s 无投递链接，跳过" % c["company_name"], flush=True)
                continue
            # 用 scrape 阶段匹配到的那份简历（主简历 / 变体）
            job_atts = list(atts)
            rp = c.get("resume_path") or ""
            if rp and os.path.exists(rp) and rp not in job_atts:
                job_atts.append(rp)
            print("[填写] %s -> %s（简历：%s）" % (
                c["company_name"], c["url"], c.get("resume_name") or "默认"), flush=True)
            # 每家公司新开一个标签页，填完不关，便于最后统一审查
            session.open_tab()
            try:
                fa = FillAgent(
                    session, llm, c["company_name"], profile, job_atts,
                    agent_cfg=agent_cfg,
                    vision_client=vision_client,
                    use_screenshot=fill_use_screenshot,
                    resume_path=rp)
                result, _ = fa.run(c["url"])
            except Exception as e:  # noqa: BLE001
                print("[填写]   [FAIL] %s：%s" % (c["company_name"], e), flush=True)
                kv.set_applied(c["company_name"], "failed", str(e)[:200])
                continue
            analysis = (result or {}).get("analysis") or {}
            if analysis:
                record = {
                    "company_name": c["company_name"],
                    "url": c["url"],
                    "resume": c.get("resume_name", ""),
                    "state": analysis.get("state"),
                    "required": analysis.get("required") or [],
                    "optional": analysis.get("optional") or [],
                    "filled": (result or {}).get("filled", 0),
                    "remaining_required": (result or {}).get("remaining_required") or [],
                    "remaining_optional": (result or {}).get("remaining_optional") or [],
                }
                analysis_rows.append(record)
                n_req, n_opt = len(record["required"]), len(record["optional"])
                r_req, r_opt = len(record["remaining_required"]), len(record["remaining_optional"])
                print("[填写]   %s：state=%s | 必填 %d（已有值 %d / 剩余 %d）"
                      " | 选填 %d（已有值 %d / 剩余 %d） | 脚本填 %d" % (
                          c["company_name"], record["state"],
                          n_req, n_req - r_req, r_req,
                          n_opt, n_opt - r_opt, r_opt,
                          record["filled"]), flush=True)
                kv.set_applied(c["company_name"], "filled", record["state"] or "")
                applied.append(c["company_name"])
            else:
                kv.set_applied(c["company_name"], "failed", "分析失败")
                print("[填写]   %s：[FAIL] 分析失败" % c["company_name"], flush=True)
        if keep_pages:
            print("[fill] 已完成，浏览器保持打开（%d 个标签页）。请人工检查/修改后再按回车："
                  % session.tab_count(), flush=True)
            try:
                input("按回车后截图记录你的修改 >>> ")
            except EOFError:
                pass
            # 截图复审：读取你人工修改后的字段值，记录到上下文，供下次投递复用
            try:
                fa = FillAgent(session, llm, "人工审核", profile, agent_cfg=agent_cfg,
                               vision_client=vision_client,
                               use_screenshot=fill_use_screenshot)
                review = fa._review_current()
                ctx = {}
                if os.path.exists(context_path):
                    try:
                        ctx = json.load(open(context_path, encoding="utf-8")) or {}
                    except Exception:  # noqa: BLE001
                        ctx = {}
                merged = ctx.get("answers") or {}
                n = 0
                for f in review.get("filled_fields") or []:
                    lb = (f.get("label") or "").strip()
                    val = f.get("value") or ""
                    if lb and val:
                        merged[lb] = val
                        n += 1
                ctx["answers"] = merged
                ctx["last_review"] = review
                with open(context_path, "w", encoding="utf-8") as f:
                    json.dump(ctx, f, ensure_ascii=False, indent=2)
                print("[fill] 已记录人工审核后的 %d 个字段 -> %s" % (n, context_path), flush=True)
            except Exception as e:  # noqa: BLE001
                print("[fill] 复审记录失败：%s" % e, flush=True)
            print("[fill] 浏览器保持打开，可直接关闭窗口。", flush=True)
            try:
                input("按回车结束进程 >>> ")
            except EOFError:
                pass
    finally:
        if not keep_pages:
            session.close()

    analysis_path = os.path.join(out_dir, "fill_analysis.jsonl")
    try:
        with open(analysis_path, "w", encoding="utf-8") as f:
            for r in analysis_rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    except Exception as e:  # noqa: BLE001
        print("[fill] 写分析结果失败：%s" % e, flush=True)
    summary = {
        "selected": total,
        "applied": len(applied),
        "companies": applied,
        "analysis_jsonl": analysis_path,
    }
    write_json(summary, os.path.join(out_dir, "fill_summary.json"))
    return summary
