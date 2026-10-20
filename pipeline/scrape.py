# -*- coding: utf-8 -*-
"""阶段3：打开公司投递页，抓取「全部岗位」→ 逐个提取 JD → 与两份简历匹配
→ 保留匹配分 >= 阈值的、最相关的前 K 条岗位，写入 data/jobs/jobs.jsonl。

需要 Playwright 与 LLM key（见 requirements.txt / config.yaml）。
"""
from __future__ import annotations

import asyncio
import csv
import datetime
import json
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import config as config_mod
from core.async_utils import run_async
from core.cache import KV
from core.dedup import classify_company_names, load_applied_companies
from core.store import ensure_dir, write_csv, write_json, write_jsonl
from browser.browser import BrowserSession, needs_login
from browser.agent import BrowserAgent
from llm.client import build_client
from matching.matcher import match_async
from matching.preference import tier
from pipeline.beisen import fetch_jobs as beisen_fetch_jobs, is_beisen
from pipeline.moka import fetch_jobs as moka_fetch_jobs, is_moka
from pipeline.feishu import fetch_jobs as feishu_fetch_jobs, is_feishu
from pipeline.portal import fetch_jobs as portal_fetch_jobs
from resume.parser import extract_text


def _log(msg: str) -> None:
    """带时间戳的进度日志。"""
    print("%s %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def _flag(row: dict, key: str) -> int:
    return 1 if str(row.get(key, "")).strip() in ("1", "True", "true", "是") else 0


def _sort_by_priority(rows: list[dict]) -> list[dict]:
    """投递优先级：500强 -> 上市公司 -> 其他（同类保持原文件顺序）。"""
    return sorted(rows, key=lambda r: (-_flag(r, "china500"), -_flag(r, "listed")))


def _load_companies(cfg) -> list[dict]:
    enriched = config_mod.abspath(cfg.get("paths", {}).get("data_enriched", "data/enriched") + "/companies_enriched.csv")
    raw = config_mod.abspath(cfg.get("paths", {}).get("data_raw", "data/raw") + "/companies.csv")
    path = enriched if os.path.exists(enriched) else raw
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    # 每次运行都重新读取文件，并按「500强 -> 上市 -> 其他」排序
    return _sort_by_priority(rows)


def _clear_screenshots(dir_path: str) -> None:
    """清空截图目录（运行结束后调用）。"""
    if not dir_path or not os.path.isdir(dir_path):
        return
    n = 0
    for name in os.listdir(dir_path):
        p = os.path.join(dir_path, name)
        if os.path.isfile(p):
            try:
                os.remove(p)
                n += 1
            except Exception:  # noqa: BLE001
                pass
    if n:
        _log("[scrape] 已清理 %d 张截图（%s）" % (n, dir_path))


def _wait_for_login(page, name: str) -> None:
    """遇到登录/验证墙时暂停，等用户在浏览器里登录后回车继续。"""
    print("[scrape]   [登录] 「%s」需要登录/验证，请在浏览器窗口完成登录。" % name, flush=True)
    print("[scrape]   登录完成后回到本终端按【回车】继续；若无法登录可直接回车跳过本页。", flush=True)
    try:
        input("[scrape]   按回车继续 >>> ")
    except EOFError:
        pass
    try:
        page.wait_for_timeout(1500)
    except Exception:
        pass


def _normalize_positions(positions, base_url: str) -> list[dict]:
    from urllib.parse import urljoin
    seen, out = set(), []
    for p in positions:
        if not isinstance(p, dict):
            continue
        url = (p.get("url") or "").strip()
        title = (p.get("title") or "").strip()
        if url and not url.startswith("http"):
            url = urljoin(base_url, url)
        if not url.startswith("http") or url in seen:
            continue
        seen.add(url)
        out.append({"title": title, "url": url})
    return out


def _extract_position_links(page) -> list[dict]:
    """确定性抽取列表页的岗位详情链接（不点进详情页），按常见 job 详情 URL 特征过滤。"""
    try:
        links = page.eval_on_selector_all(
            "a[href]",
            "els => els.map(e => ({text: (e.innerText||'').trim(), href: e.href}))")
    except Exception:
        return []
    out, seen = [], set()
    for l in links:
        href = (l.get("href") or "").strip()
        text = (l.get("text") or "").strip()
        if not href or href in seen:
            continue
        low = href.lower()
        if not any(k in low for k in ["jobadid", "/detail", "/job/", "jobid",
                                      "positionid", "/position", "jobdetail"]):
            continue
        seen.add(href)
        out.append({"title": text, "url": href})
    return out


_DEAD_KEYS = ("已停用", "已被停用", "已删除", "已被删除", "停用或删除", "已被删除或停用",
              "不存在", "页面错误", "页面走丢", "页面过期", "已下线", "已失效",
              "404", "not found", "page not found", "no longer available",
              "has been deleted", "内容已删除")


def _is_dead_page(page) -> tuple[bool, str]:
    """判断是否死链/停用页。返回 (是否, 依据片段)。"""
    try:
        title = page.title() or ""
        text = (page.inner_text("body") or "")[:500]
    except Exception:
        return False, ""
    blob = (title + " " + text).lower()
    for k in _DEAD_KEYS:
        if k.lower() in blob:
            return True, (title or text)[:100]
    return False, ""


_VERIFY_KEYS = ("环境异常", "完成验证", "验证后即可继续", "wappoc_appmsgcaptcha",
                "安全验证", "请完成验证", "访问过于频繁", "操作频繁")


def _is_verify_page(page) -> bool:
    """是否是「需要人工验证」的页面（如微信反爬）。"""
    try:
        url = (page.url or "").lower()
        if "wappoc_appmsgcaptcha" in url:
            return True
        title = page.title() or ""
        text = (page.inner_text("body") or "")[:300]
    except Exception:
        return False
    blob = (title + " " + text).lower()
    return any(k.lower() in blob for k in _VERIFY_KEYS)


_STRONG_TITLE = ("工程师", "经理", "专员", "主管", "实习生", "管培", "顾问",
                 "助理", "设计师", "研究员", "架构师", "分析师")
_WEAK_TITLE = ("开发", "测试", "算法", "数据", "分析", "运营", "销售", "财务",
               "技术", "研究", "设计", "管理", "支持")
_HEADER_WORDS = {"岗位类别", "岗位序列", "岗位名称", "职位名称", "专业", "学历",
                 "工作地点", "点击申请", "立即申请", "序号", "操作", "岗位", "职位"}


def _pick_title(cells) -> str:
    """从一行单元格里挑最像岗位名的（强关键词 > 弱关键词，排除「xx类」）。"""
    best, best_score = "", 0
    for c in cells:
        if not c or len(c) > 30 or c.endswith("类") or c in _HEADER_WORDS:
            continue
        score = 0
        if any(h in c for h in _STRONG_TITLE):
            score = 2
        elif any(h in c for h in _WEAK_TITLE):
            score = 1
        if score > best_score:
            best, best_score = c, score
    return best


def _extract_table_jobs(page) -> list[dict]:
    """从 <table> 行里抽岗位（服务端渲染的岗位表格，没有 <a href>、没有详情页）。"""
    try:
        rows = page.eval_on_selector_all(
            "table tr",
            "els => els.map(tr => Array.from(tr.querySelectorAll('td'))"
            ".map(td => (td.innerText||'').trim()))")
    except Exception:
        return []
    out, seen = [], set()
    for cells in rows:
        cells = [c for c in (cells or []) if c]
        if len(cells) < 2:
            continue
        title = _pick_title(cells)
        if not title or title in seen:
            continue
        seen.add(title)
        out.append({
            "title": title,
            "responsibilities": " | ".join(cells),
            "requirements": "",
            "location": "",
            "url": page.url,
            "_table": True,
        })
    return out


def _extract_positions(session, llm, company: str, max_rounds: int,
                       use_screenshot: bool = False, screenshot_dir: str | None = None,
                       vision_model: str | None = None,
                       vision_client=None) -> list[dict]:
    """抽岗位：死链秒退 → 直接抽链接 → 短轮询(3×2s + 滚动) → LLM 兜底 → agent 兜底。"""
    page = session.page

    # 0) 死链/停用页：立刻放弃，不空等
    dead, snippet = _is_dead_page(page)
    if dead:
        _log("[scrape]   [死链] 页面已停用/删除（%s），跳过" % snippet)
        return []

    # 0.5) 需要人工验证（如微信反爬「环境异常」）：暂停等验证
    if _is_verify_page(page):
        _log("[scrape]   [验证] 页面需要人工验证（环境异常/反爬），请到浏览器窗口完成验证。")
        _log("[scrape]   验证完成后回到本终端按【回车】继续；直接回车则跳过本页。")
        try:
            input("[scrape]   验证完成后按回车 >>> ")
        except EOFError:
            pass
        try:
            page.wait_for_timeout(2000)
        except Exception:
            pass
        if _is_verify_page(page):
            _log("[scrape]   [验证] 仍未通过，跳过本页")
            return []
        _log("[scrape]   [验证] 已通过，继续抽取")

    # 1) 直接抽链接（多数页面一次就能抽到）
    positions = _extract_position_links(page)
    if positions:
        _log("[scrape]   从列表页链接抽取到 %d 个岗位" % len(positions))
        return _normalize_positions(positions, page.url)

    # 1.5) 表格形式的岗位（服务端渲染，没有 <a href>、没有详情页）
    table_jobs = _extract_table_jobs(page)
    if table_jobs:
        _log("[scrape]   从表格抽取到 %d 个岗位" % len(table_jobs))
        return table_jobs

    # 2) 短轮询：不滚动页面，只等渲染（避免把页面滑到底部）
    for attempt in range(1, 4):
        try:
            page.wait_for_timeout(1500)
        except Exception:
            break
        positions = _extract_position_links(page)
        if positions:
            _log("[scrape]   等待 %d 次后抽取到 %d 个岗位" % (attempt, len(positions)))
            return _normalize_positions(positions, page.url)
        dead, snippet = _is_dead_page(page)
        if dead:
            _log("[scrape]   [死链] 页面已停用/删除（%s），跳过" % snippet)
            return []
        if needs_login(page):
            _wait_for_login(page, company)

    # 3) 诊断 + LLM 兜底
    try:
        title = page.title()
    except Exception:
        title = ""
    try:
        text = page.inner_text("body")
    except Exception:
        text = ""
    _log("[scrape]   [warn] 未抽到岗位链接；标题=%r，URL=%s，正文前 200 字：%s" % (
        title, page.url, (text[:200].replace("\n", " ") if text else "(空)")))

    # LLM 从 页面文本 + 链接 抽取
    _log("[scrape]     [3] 改用 LLM 从页面文本抽取（可能较慢）...")
    links = session._links(80)
    prompt = (
        "从下面的「页面文本 + 页面链接」中，提取该公司**所有在招岗位**及其详情链接。\n"
        "只输出 JSON：{\"positions\":[{\"title\":\"岗位名称\",\"url\":\"完整链接\"}]}。\n"
        "规则：文本里只有岗位名、链接在 links 里时，请把两者对应起来；"
        "只保留岗位详情链接，不要导航/菜单/首页链接；拿不到链接的岗位不列。"
    )
    payload = "页面文本：\n%s\n\n页面链接：\n%s" % (text, json.dumps(links, ensure_ascii=False))
    content = prompt + "\n\n" + payload
    use_vision = use_screenshot
    if use_vision:
        # 按用户要求：不滚动页面，直接整页截图给模型
        try:
            import base64
            png = session.page.screenshot(type="jpeg", quality=60, full_page=True)
            b64 = base64.b64encode(png).decode("ascii")
            content = [
                {"type": "text", "text": content},
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b64}},
            ]
            _log("[scrape]     已附带整页截图（%d KB）" % (len(png) // 1024))
        except Exception as e:  # noqa: BLE001
            _log("[scrape]     截图失败，改用纯文本：%s" % e)
            use_vision = False
    positions = []
    t0 = time.time()
    try:
        # 没配独立视觉供应商时 vision_client 为 None，回退到主 llm（同一 key，仅换模型）
        client = (vision_client or llm) if use_vision else llm
        data = client.chat_json([{"role": "user", "content": content}], max_tokens=2500,
                                model=vision_model if use_vision else None,
                                session_id=company or None)
        positions = (data or {}).get("positions") or []
        _log("[scrape]     [OK] LLM 抽取到 %d 个岗位（%.1fs）" % (len(positions), time.time() - t0))
    except Exception as e:  # noqa: BLE001
        _log("[scrape]     [warn] LLM 岗位抽取失败（%.1fs）：%s" % (time.time() - t0, e))

    # 3) agent 兜底（可点击/翻页）
    if not positions:
        _log("[scrape]     [4] 改用 agent 兜底（可点击/翻页，最慢）...")
        goal = (
            "目标公司：%s。请浏览当前招聘页面，列出「公司所有在招岗位」的名称和详情链接。"
            "如果岗位列表分页或需要按「全部/社会招聘/校园招聘」切换，尽量都翻一遍。"
            "最终用 done 返回 result：{\"positions\":[{\"title\":\"岗位名称\",\"url\":\"完整链接\"}]}。"
        ) % company
        agent = BrowserAgent(session, llm, max_rounds=max_rounds,
                             on_login_required=lambda: _wait_for_login(session.page, company),
                             use_screenshot=use_screenshot,
                             screenshot_dir=screenshot_dir,
                             vision_model=vision_model,
                             vision_client=vision_client,
                             label=company or "scrape")
        result, _ = agent.run(goal)
        positions = (result or {}).get("positions") or []
        _log("[scrape]     [OK] agent 兜底抽取到 %d 个岗位" % len(positions))

    return _normalize_positions(positions, page.url)


def _extract_jd(session, llm, pos: dict, company: str = "", use_screenshot: bool = False,
                vision_model: str | None = None, vision_client=None) -> dict:
    """打开单个岗位链接，提取 JD。

    JD 一般就在正文里，所以不再死等「岗位职责」关键词，加载后直接取正文；
    正文信息不足时，再用整页长截图 + 视觉模型兜底。
    """
    import base64
    url = pos.get("url", "")
    title = pos.get("title", "")
    t0 = time.time()
    _log("[scrape]       打开岗位：%s" % url)
    try:
        session.page.goto(url, wait_until="domcontentloaded", timeout=session.timeout_ms)
    except Exception as e:  # noqa: BLE001
        _log("[scrape]       [warn] 打开岗位失败：%s" % e)
        return {}
    # 不再等特定关键词，给页面一点渲染时间即可
    try:
        session.page.wait_for_timeout(2500)
    except Exception:
        pass
    if needs_login(session.page):
        _log("[scrape]       [登录] JD 页需要登录，跳过")
        return {}
    text = ""
    try:
        text = session.page.inner_text("body")[:6000]
    except Exception:
        pass
    _log("[scrape]       JD 页加载 %.1fs，正文 %d 字，提取 JD..." % (time.time() - t0, len(text)))
    prompt = (
        "从下面的岗位页面文本（可能还附有整页截图）中提取岗位信息，只输出 JSON："
        '{"title":"岗位名称","responsibilities":"岗位职责","requirements":"任职要求","location":"工作地点"}'
    )
    t1 = time.time()
    data = {}
    # 优先：整页长截图 + 视觉模型（截图能覆盖懒加载/折叠区域）
    if use_screenshot:
        try:
            png = session.page.screenshot(type="jpeg", quality=60, full_page=True)
            b64 = base64.b64encode(png).decode("ascii")
            content = [
                {"type": "text", "text": prompt + "\n\n页面文本：\n" + text},
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b64}},
            ]
            data = (vision_client or llm).chat_json(
                [{"role": "user", "content": content}], max_tokens=2500,
                model=vision_model, session_id=company or None)
            _log("[scrape]       JD 提取完成（整页截图 + 视觉，%.1fs）" % (time.time() - t1))
        except Exception as e:  # noqa: BLE001
            _log("[scrape]       [warn] 截图/视觉提取失败，回退纯文本：%s" % e)
            data = {}
    if not data:
        try:
            data = llm.chat_json([{"role": "user", "content": prompt + "\n\n页面文本：\n" + text}],
                                 session_id=company or None)
            _log("[scrape]       JD 提取完成（纯文本，%.1fs）" % (time.time() - t1))
        except Exception as e:  # noqa: BLE001
            _log("[scrape]       [warn] JD 提取失败：%s" % e)
            data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("title", title)
    data["url"] = url
    data["source_link"] = url
    return data


async def _match_jobs_async(job_list, resumes, llm, pref, concurrency):
    """并发匹配：每个岗位 × 每份简历一个协程。

    返回 [(岗位, 最高分, 匹配到该分数的简历下标)]（保持原顺序）。
    """
    sem = asyncio.Semaphore(max(1, concurrency))

    async def one(jd, ri, r):
        async with sem:
            try:
                res = await match_async(r, jd, llm, pref)
                return ri, (res.score if res else 0.0)
            except Exception as e:  # noqa: BLE001
                print("[scrape]       匹配失败：%s" % e, flush=True)
                return ri, 0.0

    tasks = [one(jd, ri, r) for jd in job_list for ri, r in enumerate(resumes)]
    scores = await asyncio.gather(*tasks) if tasks else []
    out = []
    k = 0
    for jd in job_list:
        row = scores[k:k + len(resumes)]
        k += len(resumes)
        if row:
            best = max(s for _, s in row)
            ri = next(i for i, s in row if s == best)
        else:
            best, ri = 0.0, 0
        out.append((jd, best, ri))
    return out


def run_scrape(cfg, limit: int | None = None, names: list[str] | None = None,
               force: bool = False) -> dict:
    out_dir = config_mod.abspath(cfg.get("paths", {}).get("data_jobs", "data/jobs"))
    ensure_dir(out_dir)
    kv = KV(config_mod.abspath(
        cfg.get("paths", {}).get("data_cache", "data/cache") + "/cache.db"))

    companies = _load_companies(cfg)
    # ---- 1) 先剔除「已经查找过」的公司（标志位 scrape.skip_processed）----
    _scrape_cfg = cfg.get("scrape", {}) or {}
    skip_processed = bool(_scrape_cfg.get("skip_processed", True))
    scraped_map = {r["name"]: r for r in kv.all_scraped()}
    skipped_scraped = 0
    if skip_processed and not force and not names:
        before0 = len(companies)
        companies = [c for c in companies
                     if (c.get("company_name") or "") not in scraped_map]
        skipped_scraped = before0 - len(companies)
    # ---- 2) 再对剩余公司做「已投递」去重 ----
    applied_path = config_mod.abspath(cfg.get("applied_xlsx", "data/applied/applied_companies.xlsx"))
    threshold_dedup = float(cfg.get("applied_threshold", 0.85))
    applied = load_applied_companies(applied_path)
    names_all = [c.get("company_name", "") for c in companies]
    store_path = config_mod.abspath(
        cfg.get("paths", {}).get("data_cache", "data/cache") + "/embeddings.db")
    classification, _ = classify_company_names(names_all, applied, cfg, threshold_dedup, store_path)
    before = len(companies)
    companies = [c for c in companies
                 if not classification.get(c.get("company_name", ""), (False, "", 0.0))[0]]
    skipped_applied = before - len(companies)
    # 只处理有投递链接的公司
    companies = [c for c in companies if (c.get("apply_link") or c.get("official_link"))]
    if names:
        companies = [c for c in companies if any(n in (c.get("company_name") or "") for n in names)]
    if limit:
        companies = companies[:limit]

    # 两份简历（主简历 + alternates）
    resume_cfg = cfg.get("resume", {})
    resume_paths = [config_mod.abspath(resume_cfg.get("path", "resume.pdf"))]
    resume_paths += [config_mod.abspath(p) for p in (resume_cfg.get("alternates") or [])]
    resumes = []
    for p in resume_paths:
        try:
            resumes.append(extract_text(p))
        except Exception as e:  # noqa: BLE001
            print("[scrape] 简历读取失败 %s：%s" % (p, e), flush=True)
    if not resumes:
        raise RuntimeError("没有可用简历，请检查 resume.path")

    pref = cfg.get("preference", {}) or {}
    scrape_cfg = cfg.get("scrape", {}) or {}
    match_threshold = float(scrape_cfg.get("match_threshold", 80))
    top_k = int(scrape_cfg.get("top_k", 10))
    max_positions = int(scrape_cfg.get("max_positions", 100))
    match_concurrency = int(scrape_cfg.get("concurrency", 8))
    # 没有接口适配的站点是否回退浏览器（默认不回退，直接记失败，人工看）
    browser_fallback = bool(scrape_cfg.get("browser_fallback", False))
    # 找到 N 家「有岗位入选」的公司后停止寻找，进入下一流程
    max_matched = int(scrape_cfg.get("max_matched_companies", 10))
    # 默认跳过微信公众号（反爬验证 + 推文不是岗位列表页）
    skip_domains = scrape_cfg.get("skip_domains")
    if skip_domains is None:
        skip_domains = ["mp.weixin.qq.com"]
    max_rounds = cfg.get("agent", {}).get("max_rounds", 10)
    agent_cfg = cfg.get("agent", {}) or {}
    # scrape 阶段单独开关（默认跟随 agent.use_screenshot；scrape 一般不需要图像识别）
    use_screenshot = bool(scrape_cfg.get("use_screenshot",
                                         agent_cfg.get("use_screenshot", False)))
    screenshot_dir = config_mod.abspath(agent_cfg.get("screenshot_dir", "data/screenshots"))
    # DeepSeek 支持图片的模型是 deepseek-flash（deepseek-chat 是纯文本）
    vision_model = agent_cfg.get("vision_model") or ("deepseek-flash" if use_screenshot else None)
    clear_screenshots = bool(agent_cfg.get("clear_screenshots", True))

    llm = build_client(cfg)
    # 视觉模型可来自另一家供应商（OpenAI 兼容）
    vision_client = None
    v_base = agent_cfg.get("vision_base_url") or ""
    v_key = agent_cfg.get("vision_api_key") or ""
    if use_screenshot and v_base and v_key:
        from llm.client import DeepSeekClient
        vision_client = DeepSeekClient(v_key, v_base, model=vision_model or "deepseek-flash")
        _log("[scrape] 视觉模型：%s @ %s" % (vision_model or "deepseek-flash", v_base))
    elif use_screenshot:
        _log("[scrape] 视觉模型：%s（复用 llm 的 base_url/key）" % vision_model)
    bc = cfg.get("browser", {})
    session = BrowserSession(
        channel=bc.get("channel", "chrome"),
        headless=bc.get("headless", False),
        user_data_dir=config_mod.abspath(bc.get("user_data_dir", "data/browser_profile")),
        timeout_ms=bc.get("timeout_ms", 30000),
    )

    all_jobs = []
    failures: list[dict] = []
    matched_companies = 0
    total = len(companies)
    _log("[scrape] 待抓取 %d 家公司（已投递跳过 %d 家，已查找跳过 %d 家）；"
         "每家公司最多看 %d 个岗位，匹配分 >= %.0f 保留，最多保留 %d 条" % (
             total, skipped_applied, skipped_scraped, max_positions, match_threshold, top_k))
    browser_started = False
    try:
        for i, c in enumerate(companies, 1):
            name = c.get("company_name", "")
            url = c.get("apply_link") or c.get("official_link")
            c_t0 = time.time()
            _log("[scrape] ===== (%d/%d) %s -> %s =====" % (i, total, name, url))
            try:
                # ---- 跳过指定域名（默认：微信公众号，反爬 + 推文非岗位页） ----
                if any(d in url for d in skip_domains):
                    failures.append({"company_name": name, "url": url, "source": "skip",
                                     "reason": "跳过域名（公众号推文/反爬）"})
                    kv.set_scraped(name, url, "skipped", "跳过域名")
                    _log("[scrape]   跳过域名（公众号/反爬）：%s" % url)
                    continue

                # ---- 优先走接口（Moka / 北森）：秒级拿全部岗位，含 JD ----
                t0 = time.time()
                job_list, api_ok, api_kind = [], False, ""
                if is_moka(url):
                    job_list, api_ok = moka_fetch_jobs(url)
                    api_kind = "Moka"
                else:
                    job_list, api_ok = beisen_fetch_jobs(url)
                    api_kind = "北森"
                if not api_ok:
                    job_list, api_ok = portal_fetch_jobs(url)
                    api_kind = "通用门户"
                if api_ok:
                    _log("[scrape]   %s 接口返回 %d 个岗位（含 JD，耗时 %.1fs）" % (
                        api_kind, len(job_list), time.time() - t0))
                elif is_feishu(url):
                    # 飞书：直接调 search/job/posts 接口（带 csrf token），不开浏览器
                    job_list, feishu_ok = feishu_fetch_jobs(url)
                    api_ok = feishu_ok
                    api_kind = "飞书"
                    _log("[scrape]   飞书接口返回 %d 个岗位（含 JD，耗时 %.1fs）" % (
                        len(job_list), time.time() - t0))
                elif browser_fallback:
                    if not browser_started:
                        _log("[scrape] 启动浏览器（channel=%s, headless=%s）..." % (
                            bc.get("channel", "chrome"), bc.get("headless", False)))
                        session.start()
                        browser_started = True
                        _log("[scrape] 浏览器已启动")
                    t0 = time.time()
                    session.page.goto(url, wait_until="domcontentloaded", timeout=session.timeout_ms)
                    _log("[scrape]   列表页打开 %.1fs" % (time.time() - t0))
                    if needs_login(session.page):
                        _wait_for_login(session.page, name)
                    t0 = time.time()
                    positions = _extract_positions(
                        session, llm, name, max_rounds,
                        use_screenshot=use_screenshot, screenshot_dir=screenshot_dir,
                        vision_model=vision_model, vision_client=vision_client)
                    _log("[scrape]   岗位列表：%d 个（抽取耗时 %.1fs）" % (
                        len(positions), time.time() - t0))
                    # 逐个岗位提取 JD
                    job_list = []
                    for j, pos in enumerate(positions[:max_positions], 1):
                        if pos.get("_table"):
                            job_list.append(pos)  # 表格岗位已带信息，无需再开详情页
                            continue
                        _log("[scrape]   ---- 提取 JD %d/%d：%s" % (
                            j, min(len(positions), max_positions), pos.get("title", "")))
                        jd = _extract_jd(session, llm, pos, company=name,
                                         use_screenshot=use_screenshot,
                                         vision_model=vision_model, vision_client=vision_client)
                        if jd.get("title"):
                            job_list.append(jd)
                else:
                    # 无接口适配、也未开启浏览器兜底：直接记失败，留给人工
                    failures.append({"company_name": name, "url": url,
                                     "source": "unadapted",
                                     "reason": "未适配站点（无接口，已跳过）"})
                    kv.set_scraped(name, url, "failed", "未适配站点")
                    _log("[scrape]   未适配站点，已记入失败清单")
                    continue

                if not job_list:
                    # 抓不到不代表「真的没岗位」，记录下来供人工排查
                    diag = {}
                    if not is_beisen(url) and browser_started:
                        try:
                            diag = {
                                "page_title": session.page.title(),
                                "page_url": session.page.url,
                                "page_text": (session.page.inner_text("body") or "")[:300],
                            }
                        except Exception:  # noqa: BLE001
                            pass
                    failures.append({
                        "company_name": name, "url": url,
                        "source": "beisen" if is_beisen(url) else "browser",
                        "reason": "未获取到岗位",
                        **diag,
                    })
                    kv.set_scraped(name, url, "failed", "未获取到岗位")
                    _log("[scrape]   [warn] 该公司未取到岗位，已记录到失败清单")
                    continue

                # ---- 统一：并发匹配（每个岗位 × 每份简历一个协程），保留 >=阈值，取前 top_k ----
                t1 = time.time()
                pairs = run_async(_match_jobs_async(
                    job_list[:max_positions], resumes, llm, pref, match_concurrency))
                scored = []
                for jd, best, ri in pairs:
                    jd["company_name"] = name
                    jd["score"] = best
                    jd["tier"] = tier(best)
                    # 记录是哪份简历匹配上的，fill 阶段直接用这份简历投递
                    jd["resume_path"] = resume_paths[ri] if ri < len(resume_paths) else ""
                    jd["resume_name"] = (os.path.basename(jd["resume_path"])
                                         if jd["resume_path"] else "")
                    if best >= match_threshold:
                        scored.append(jd)
                    _log("[scrape]   %s -> %.1f 分 [%s档] 简历=%s%s" % (
                        jd.get("title", ""), best, jd["tier"], jd["resume_name"],
                        "（入选）" if best >= match_threshold else ""))
                _log("[scrape]   并发匹配 %d 个岗位完成（%.1fs，并发度 %d）" % (
                    len(pairs), time.time() - t1, match_concurrency))
                scored.sort(key=lambda x: x["score"], reverse=True)
                top = scored[:top_k]
                _log("[scrape]   >=%.0f 分岗位 %d 个，保留 %d 个（该公司总耗时 %.1fs）" % (
                    match_threshold, len(scored), len(top), time.time() - c_t0))
                all_jobs.extend(top)
                write_jsonl(all_jobs, os.path.join(out_dir, "jobs.jsonl"))
                kv.set_scraped(name, url, "done", "", len(top))
                if top:
                    matched_companies += 1
                    _log("[scrape]   已找到 %d/%d 家入选公司" % (matched_companies, max_matched))
                    if max_matched and matched_companies >= max_matched:
                        _log("[scrape] 已达 %d 家入选公司，停止寻找，进入下一流程" % matched_companies)
                        break
            except Exception as e:  # noqa: BLE001
                failures.append({
                    "company_name": name, "url": url,
                    "source": "beisen" if is_beisen(url) else "browser",
                    "reason": "异常: %s" % e,
                })
                kv.set_scraped(name, url, "error", str(e)[:200])
                _log("[scrape]   [FAIL] %s：%s（已记录到失败清单）" % (name, e))
                continue
    finally:
        session.close()
        # 运行结束后清理截图，避免占空间
        if clear_screenshots:
            _clear_screenshots(screenshot_dir)

    write_jsonl(all_jobs, os.path.join(out_dir, "jobs.jsonl"))
    # 抓不到岗位的公司单独记录，便于人工排查（可能是脚本没覆盖到）
    fail_path = os.path.join(out_dir, "scrape_failures.jsonl")
    write_jsonl(failures, fail_path)
    # 已查找公司清单（打标签，供下次跳过和人工查看）
    scraped_rows = kv.all_scraped()
    scraped_csv = os.path.join(out_dir, "scraped_companies.csv")
    write_csv(scraped_rows, scraped_csv)
    # ---- 把当天结果归档到 data/jobs/<日期>/ ----
    day_dir = os.path.join(out_dir, datetime.date.today().isoformat())
    ensure_dir(day_dir)
    for fn in ("jobs.jsonl", "scrape_failures.jsonl", "scraped_companies.csv"):
        src = os.path.join(out_dir, fn)
        if os.path.exists(src):
            try:
                shutil.copy2(src, os.path.join(day_dir, fn))
            except Exception:  # noqa: BLE001
                pass
    summary = {"total_jobs": len(all_jobs), "matched_companies": matched_companies,
               "skipped_applied": skipped_applied, "skipped_scraped": skipped_scraped,
               "match_threshold": match_threshold, "top_k": top_k,
               "max_positions": max_positions,
               "max_matched_companies": max_matched,
               "failures": len(failures),
               "scraped_total": len(scraped_rows),
               "jobs_jsonl": os.path.join(out_dir, "jobs.jsonl"),
               "failures_jsonl": fail_path,
               "scraped_csv": scraped_csv,
               "day_dir": day_dir}
    write_json(summary, os.path.join(out_dir, "scrape_summary.json"))
    try:
        shutil.copy2(os.path.join(out_dir, "scrape_summary.json"),
                     os.path.join(day_dir, "scrape_summary.json"))
    except Exception:  # noqa: BLE001
        pass
    print("[scrape] 完成：入选公司 %d 家，保留 %d 条岗位，失败 %d 家；当天归档：%s" % (
        matched_companies, len(all_jobs), len(failures), day_dir), flush=True)
    return summary
