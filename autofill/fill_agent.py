# -*- coding: utf-8 -*-
"""fill 阶段的投递 Agent（独立文件，不与 pipeline 逻辑混用）。

策略：一次整页分析，避免多轮 agent 死循环。
    1) 打开目标岗位页；
    2) 截「整页长图」+ 取全文；
    3) 一次调用多模态模型，返回 {state, required[], optional[]}；
    4) 按模型给的值填一次（不循环），然后进入下一家公司。

调用方（pipeline/fill.py）只负责「选公司、建 session、传参、收集结果」。
"""
from __future__ import annotations

import base64
import json
import os

ANALYZE_PROMPT = """你是招聘表单分析助手。下面是一个招聘网站页面的整页截图和页面文本。
请判断当前页面状态，并列出所有表单字段，区分必填与选填。

只输出 JSON：
{
  "state": "login|job_detail|apply_form|submitted|unknown",
  "required": [{"label": "字段名", "value": "应填的值", "type": "text|select|radio|file"}],
  "optional": [{"label": "字段名", "value": "应填的值", "type": "text|select|radio|file"}]
}

规则：
1. state=apply_form 表示当前就是填写表单的页面；
2. required 是带 * 的必填项，optional 是其它可填项；
3. value 必须是「我的信息/简历」里的**原文逐字摘录**，严禁改写、精简、概括或编造；
4. 简历里找不到对应原文的字段，value 留空字符串（后续会跳过）；
5. 长文本（实习内容、工作职责等）也要**完整摘录简历原文**，不要截断、不要总结；
6. 选择类字段（select/radio）的 value 只能填选项文字本身；
7. 文件/附件类字段 type 填 file，value 留空。
"""

REVIEW_PROMPT = """请检查当前招聘表单页面的**实际填写状态**（以截图中每个输入框/选择框当前显示的值或选中项为准）。

只输出 JSON：
{
  "filled_fields": [{"label": "字段名", "value": "页面当前显示的值"}],
  "empty_required": [{"label": "仍为空的必填字段"}],
  "empty_optional": [{"label": "仍为空的选填字段"}]
}

规则：
1. 页面上**已经有值**的字段放进 filled_fields（value 用页面上看到的值，原文照抄）；
2. 页面上**空着**的必填字段放进 empty_required；
3. 页面上**空着**的选填字段放进 empty_optional；
4. 不要根据简历推测，只看页面当前状态。
"""

# 按 label 文本给受控输入框赋值（触发 input/change，兼容 Vue/React）
_JS_FILL = """
([label, value]) => {
    const els = Array.from(document.querySelectorAll('input,textarea'));
    for (const el of els) {
        let c = el;
        for (let i = 0; i < 6 && c; i++) {
            c = c.parentElement; if (!c) break;
            const cn = String(c.className || '') + ' ' + String(c.getAttribute('class') || '');
            if (/form-item|formitem|form-group|field|el-form|ant-form/i.test(cn)) break;
        }
        const container = c || el.parentElement;
        let lab = '';
        if (container) {
            const l = container.querySelector('label, .el-form-item__label, .ant-form-item-label, .label');
            if (l) lab = (l.innerText || '').trim();
        }
        lab = lab.replace(/[*＊\\s:：]/g, '');
        if (lab !== label) continue;
        const proto = el.tagName === 'TEXTAREA'
            ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
        const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
        setter.call(el, value);
        el.dispatchEvent(new Event('input', { bubbles: true }));
        el.dispatchEvent(new Event('change', { bubbles: true }));
        el.dispatchEvent(new Event('blur', { bubbles: true }));
        return true;
    }
    return false;
}
"""

_JS_SELECT = """
([label, value]) => {
    const els = Array.from(document.querySelectorAll('select'));
    for (const el of els) {
        let c = el;
        for (let i = 0; i < 6 && c; i++) {
            c = c.parentElement; if (!c) break;
            const cn = String(c.className || '') + ' ' + String(c.getAttribute('class') || '');
            if (/form-item|formitem|field|el-form|ant-form/i.test(cn)) break;
        }
        const container = c || el.parentElement;
        let lab = '';
        if (container) {
            const l = container.querySelector('label, .el-form-item__label, .ant-form-item-label, .label');
            if (l) lab = (l.innerText || '').trim();
        }
        lab = lab.replace(/[*＊\\s:：]/g, '');
        if (lab !== label) continue;
        const opt = Array.from(el.options).find(o => (o.text || '').trim() === value)
                 || Array.from(el.options).find(o => (o.text || '').includes(value));
        if (!opt) return false;
        el.value = opt.value;
        el.dispatchEvent(new Event('change', { bubbles: true }));
        return true;
    }
    return false;
}
"""


class FillAgent:
    def __init__(self, session, llm, company: str, profile: dict,
                 attachments: list[str] | None = None, agent_cfg: dict | None = None,
                 vision_client=None,
                 use_screenshot: bool | None = None, resume_path: str | None = None):
        self.session = session
        self.llm = llm
        self.company = company or "company"
        self.profile = dict(profile or {})
        self.attachments = [p for p in (attachments or []) if p and os.path.exists(p)]
        # 明确指定哪份是简历（不再靠文件名猜）
        self.resume_path = resume_path if (resume_path and os.path.exists(resume_path)) else ""
        if not self.resume_path:
            self.resume_path = next((p for p in self.attachments
                                     if "简历" in os.path.basename(p)), "")
        self.agent_cfg = agent_cfg or {}
        self.vision_client = vision_client
        if use_screenshot is None:
            use_screenshot = bool(self.agent_cfg.get("use_screenshot", True))
        self.use_screenshot = use_screenshot

    # ---------- 内部 ----------
    def _log(self, msg: str) -> None:
        print("[fill:%s] %s" % (self.company, msg), flush=True)

    def _wait_login(self) -> None:
        self._log("页面需要登录，请在浏览器完成登录后回车继续")
        try:
            input("[fill] 登录完成后按回车 >>> ")
        except EOFError:
            pass
        try:
            self.session.page.wait_for_timeout(1500)
        except Exception:  # noqa: BLE001
            pass

    def _fill_text_by_label(self, label: str, value: str) -> bool:
        try:
            return bool(self.session.page.evaluate(_JS_FILL, [label, value]))
        except Exception:  # noqa: BLE001
            return False

    def _select_by_label(self, label: str, value: str) -> bool:
        try:
            return bool(self.session.page.evaluate(_JS_SELECT, [label, value]))
        except Exception:  # noqa: BLE001
            return False

    # ---------- 对外 ----------
    def analyze(self, url: str) -> dict:
        """打开页面 -> 整页截图 + 全文 -> 一次模型分析 -> {state, required, optional}。"""
        page = self.session.page
        self._log("打开 %s" % url)
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=self.session.timeout_ms)
            page.wait_for_timeout(2500)
        except Exception as e:  # noqa: BLE001
            self._log("打开失败：%s" % e)
            return {"state": "error", "required": [], "optional": [], "error": str(e)}
        return self._analyze_current()

    def _analyze_current(self) -> dict:
        """对当前页面做一次整页分析（不跳转）。"""
        page = self.session.page
        if self._login():
            self._wait_login()

        text = ""
        try:
            text = page.inner_text("body")[:6000]
        except Exception:  # noqa: BLE001
            pass
        prompt = ANALYZE_PROMPT + "\n\n我的信息（JSON）：%s\n\n页面文本：\n%s" % (
            json.dumps(self.profile, ensure_ascii=False), text)

        content = prompt
        if self.use_screenshot:
            try:
                png = page.screenshot(type="jpeg", quality=70, full_page=True)
                b64 = base64.b64encode(png).decode("ascii")
                content = [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b64}},
                ]
                self._log("已附带整页截图（%d KB）" % (len(png) // 1024))
            except Exception as e:  # noqa: BLE001
                self._log("截图失败，改用纯文本：%s" % e)

        try:
            client = (self.vision_client or self.llm) if self.use_screenshot else self.llm
            data = client.chat_json(
                [{"role": "user", "content": content}], max_tokens=8000,
                model=(self.agent_cfg.get("vision_model") if self.use_screenshot else None))
        except Exception as e:  # noqa: BLE001
            self._log("模型分析失败：%s" % e)
            data = {}
        if not isinstance(data, dict):
            data = {}
        data.setdefault("state", "unknown")
        data.setdefault("required", [])
        data.setdefault("optional", [])
        return data

    def _review_current(self) -> dict:
        """对当前页面做复审：读「页面上实际的值」+「仍为空的字段」（不再按简历推测）。"""
        page = self.session.page
        text = ""
        try:
            text = page.inner_text("body")[:6000]
        except Exception:  # noqa: BLE001
            pass
        prompt = REVIEW_PROMPT + "\n\n页面文本：\n" + text
        content = prompt
        if self.use_screenshot:
            try:
                png = page.screenshot(type="jpeg", quality=70, full_page=True)
                b64 = base64.b64encode(png).decode("ascii")
                content = [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b64}},
                ]
            except Exception as e:  # noqa: BLE001
                self._log("复审截图失败：%s" % e)
        try:
            client = (self.vision_client or self.llm) if self.use_screenshot else self.llm
            data = client.chat_json(
                [{"role": "user", "content": content}], max_tokens=8000,
                model=(self.agent_cfg.get("vision_model") if self.use_screenshot else None))
        except Exception as e:  # noqa: BLE001
            self._log("复审分析失败：%s" % e)
            data = {}
        if not isinstance(data, dict):
            data = {}
        data.setdefault("filled_fields", [])
        data.setdefault("empty_required", [])
        data.setdefault("empty_optional", [])
        return data

    def _login(self) -> bool:
        try:
            from browser.browser import needs_login
            return needs_login(self.session.page)
        except Exception:  # noqa: BLE001
            return False

    def fill_from_analysis(self, analysis: dict) -> int:
        """按模型给出的字段值填一次（不循环）。"""
        n = 0
        for item in (analysis.get("required") or []) + (analysis.get("optional") or []):
            if not isinstance(item, dict):
                continue
            label = (item.get("label") or "").strip()
            value = item.get("value") or ""
            if not label or not value:
                continue
            t = (item.get("type") or "").lower()
            ok = (self._select_by_label(label, value)
                  if t in ("select", "dropdown", "radio") else self._fill_text_by_label(label, value))
            if ok:
                n += 1
        return n

    def _upload_resume(self) -> bool:
        """把简历塞进第一个 file 上传框（若页面支持自动解析）。"""
        resume = self.resume_path
        if not resume:
            return False
        try:
            inputs = self.session.page.query_selector_all("input[type='file']")
            if not inputs:
                return False
            inputs[0].set_input_files(resume)
            self._log("已上传简历待解析：%s" % os.path.basename(resume))
            return True
        except Exception as e:  # noqa: BLE001
            self._log("上传简历失败：%s" % e)
            return False

    def _handle_apply_form(self, analysis: dict) -> dict:
        """到达表单页：上传简历（若有自动解析）→ 分析一次 → 规则填写 → 复查剩余。"""
        page = self.session.page
        has_file = False
        try:
            has_file = bool(page.query_selector_all("input[type='file']"))
        except Exception:  # noqa: BLE001
            pass

        if has_file:
            # 有简历自动解析：上传后等 5 秒让平台回填
            if self._upload_resume():
                page.wait_for_timeout(5000)
                self._log("已上传简历，等待平台自动解析（5s）")
        else:
            self._log("页面无简历上传框，直接按简历内容分析")

        analysis = self._analyze_current()
        # 统一补填：有值就填，空值跳过（值均为简历原文摘录，不做改写）
        filled = self.fill_from_analysis(analysis)
        self._log("已按分析结果填写 %d 项（空值项自动跳过）" % filled)

        # 填完整页截图复查：读页面实际值 + 仍为空字段
        after = self._review_current()
        rem_req = after.get("empty_required") or []
        rem_opt = after.get("empty_optional") or []
        self._log("复查：剩余必填 %d 个，剩余选填 %d 个" % (len(rem_req), len(rem_opt)))
        return {"analysis": analysis, "after": after, "filled": filled,
                "remaining_required": rem_req, "remaining_optional": rem_opt}

    def _click_apply(self) -> bool:
        page = self.session.page
        for sel in ("text=立即投递", "text=申请职位", "text=投递简历", "text=我要投递",
                    "text=立即申请", "text=去投递", "button:has-text('投递')",
                    "a:has-text('投递')"):
            try:
                page.click(sel, timeout=2500)
                page.wait_for_timeout(2000)
                return True
            except Exception:  # noqa: BLE001
                continue
        return False

    def _agent_fallback(self) -> bool:
        """状态 unknown 时，用 LLM agent 点两下（校园招聘/岗位）。"""
        try:
            from browser.agent import BrowserAgent
            ag = BrowserAgent(self.session, self.llm, max_rounds=2, label=self.company)
            ag.run("如果停在首页，点「校园招聘」；如果看到岗位列表，点开一个岗位；"
                   "如果看到「立即投递」，点它。最多点两下。")
            return True
        except Exception as e:  # noqa: BLE001
            self._log("agent 兜底失败：%s" % e)
            return False

    def run(self, url: str, max_steps: int = 6) -> tuple[dict, list]:
        """状态机：没到表单页就点「立即投递」，到了就分析必填/选填并返回（进入下一家）。"""
        analysis = self.analyze(url)
        for step in range(1, max_steps + 1):
            state = analysis.get("state")
            req = analysis.get("required") or []
            opt = analysis.get("optional") or []
            self._log("第 %d 步：state=%s，必填 %d 个，选填 %d 个" % (
                step, state, len(req), len(opt)))

            if state == "apply_form":
                # 到达简历填写页：上传简历→分析→规则填写→复查，然后进入下一家
                return self._handle_apply_form(analysis), []
            if state == "submitted":
                return {"analysis": analysis, "filled": 0}, []
            if state == "job_detail":
                if not self._click_apply():
                    self._log("没找到投递按钮")
                analysis = self._analyze_current()
                continue
            if state == "login":
                self._wait_login()
                analysis = self._analyze_current()
                continue
            # unknown / error
            self._agent_fallback()
            analysis = self._analyze_current()

        return {"analysis": analysis, "filled": 0,
                "error": "超过 %d 步仍未到表单页" % max_steps}, []
