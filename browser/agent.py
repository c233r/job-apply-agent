# -*- coding: utf-8 -*-
"""多轮交互浏览器 Agent。

每轮：页面快照 -> LLM 决策下一个动作(JSON) -> 执行 -> 追加历史，
直到 LLM 返回 done 或达到 max_rounds。
"""
from __future__ import annotations

import json
import os

from .tools import ToolExecutor

ACTIONS_DESC = """
你是浏览器操作 Agent，目标由用户给定。请根据当前页面快照决定「下一步单个动作」。
可用动作（返回单个 JSON 对象，必须有 action 字段）：
  {"action":"goto","url":"https://..."}               打开网址
  {"action":"click","selector":"css选择器"}            按选择器点击
  {"action":"click","text":"可见文字"}                 按可见文字点击
  {"action":"click","index":3}                          点击「编号截图」中标号为 3 的元素（推荐）
  {"action":"fill","selector":"css选择器","value":"内容"} 填输入框
  {"action":"select","selector":"css选择器","value":"选项"} 选下拉框
  {"action":"upload","selector":"css选择器","files":["路径1","路径2"]} 上传文件
  {"action":"type","text":"内容"}                       在当前聚焦元素输入
  {"action":"scroll","direction":"down|up","amount":500} 滚动
  {"action":"go_back"}                                  后退
  {"action":"wait","ms":1000}                           等待
  {"action":"extract_text"}                             提取当前页正文
  {"action":"list_forms"}                               列出当前页表单
  {"action":"done","result":{...}}                      任务完成，result 含最终结果
规则：
1. 只输出一个 JSON 对象，不要输出其它文字。
2. 若页面信息已足够完成任务，立即返回 done。
3. 若多次点击无进展，及时返回 done 并说明原因，避免空转。
4. 只在当前招聘页面内操作，**不要点击网站导航菜单**（首页/社会招聘/校园招聘/关于我们/公司官网等）；
   如果发现自己已经离开了目标页面，立刻用 go_back 返回原页面。
5. 不要点击「投递/申请/提交」等会改变状态或跳转外部的按钮，除非任务明确要求。
"""


class BrowserAgent:
    def __init__(self, session, llm, max_rounds: int = 10, tools=None,
                 on_login_required=None, use_screenshot: bool = False,
                 screenshot_dir: str | None = None, vision_model: str | None = None,
                 vision_client=None, label: str = ""):
        self.session = session
        self.llm = llm
        self.max_rounds = max_rounds
        self.tools = tools or ToolExecutor(session)
        # 遇到登录/验证墙时的回调（由调用方提供：暂停等待人工登录）
        self.on_login_required = on_login_required
        self._login_paused = False
        # 每步截图发给多模态模型，让模型「看图」决定下一步
        self.use_screenshot = use_screenshot
        self.screenshot_dir = screenshot_dir
        self.vision_model = vision_model or None
        # 视觉模型可能来自另一家供应商（如智谱/通义），单独给一个 client
        self.vision_client = vision_client or llm
        self.label = label or "page"
        self._round = 0
        self._elements: list[dict] = []

    def _screenshot_content(self, user_text: str):
        """返回带「编号截图 + 可点元素列表」的 user content；失败则退化为纯文本。"""
        import base64
        try:
            png, elements = self.session.annotate_screenshot()
            self._elements = elements
            lines = ["%d. <%s> %s" % (e["i"], e["tag"], e["text"]) for e in elements]
            listing = "\n".join(lines) if lines else "(无可点元素)"
            text = (user_text + "\n\n截图上的红框带有编号。要点击某个元素时，"
                    "返回 {\"action\":\"click\",\"index\":编号}。可点击元素如下：\n" + listing)
            b64 = base64.b64encode(png).decode("ascii")
            if self.screenshot_dir:
                try:
                    path = os.path.join(self.screenshot_dir,
                                        "%s_r%d.jpg" % (self.label, self._round))
                    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
                    with open(path, "wb") as f:
                        f.write(png)
                except Exception:
                    pass
            return [
                {"type": "text", "text": text},
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b64}},
            ]
        except Exception as e:  # noqa: BLE001
            print("[agent] 截图失败，改用纯文本：%s" % e, flush=True)
            return user_text

    def plan(self, goal: str, snapshot: dict, history: list[dict]) -> dict | None:
        """让 LLM 决策下一个动作。返回动作 dict；失败返回 None。"""
        user = (
            "目标：%s\n\n当前页面快照：\n%s\n\n已执行历史：\n%s\n\n"
            "请结合页面截图与快照，返回下一个动作 JSON。"
            % (goal, json.dumps(snapshot, ensure_ascii=False)[:6000],
               json.dumps(history, ensure_ascii=False)[-2000:])
        )
        content = self._screenshot_content(user) if self.use_screenshot else user
        messages = [
            {"role": "system", "content": ACTIONS_DESC},
            {"role": "user", "content": content},
        ]
        try:
            # deepseek-flash 是推理模型，会先花 token 在 reasoning_content 上，
            # max_tokens 给小了 content 会为空，这里留足预算
            data = self.vision_client.chat_json(messages, max_tokens=2500, model=self.vision_model)
        except Exception as e:  # noqa: BLE001
            print("[agent] LLM 决策失败，本页终止：%s" % e, flush=True)
            return None
        if not isinstance(data, dict) or "action" not in data:
            return None
        return data

    def run(self, goal: str) -> tuple[dict, list[dict]]:
        history: list[dict] = []
        last_actions: list[str] = []
        for rnd in range(1, self.max_rounds + 1):
            self._round = rnd
            # 登录墙检测：暂停等人工登录；登录后（不再命中）自动复位，后续再遇到还能暂停
            if self.on_login_required:
                try:
                    from .browser import needs_login
                    if needs_login(self.session.page):
                        if not self._login_paused:
                            self._login_paused = True
                            self.on_login_required()
                    else:
                        self._login_paused = False
                except Exception:
                    pass
            snapshot = self.session.snapshot()
            action = self.plan(goal, snapshot, history)
            if action is None:
                history.append({"round": rnd, "action": None, "result": "LLM 未返回有效动作"})
                break
            try:
                result = self.tools.execute(action)
            except Exception as e:
                result = "执行失败: %s" % e
            history.append({"round": rnd, "action": action, "result": str(result)[:800]})
            if action.get("action") == "done":
                return action.get("result") or {"history": history}, history

            # 防死循环：连续 3 轮相同动作无进展
            sig = json.dumps(action, ensure_ascii=False, sort_keys=True)
            last_actions.append(sig)
            if len(last_actions) > 3:
                last_actions.pop(0)
            if len(last_actions) == 3 and len(set(last_actions)) == 1:
                history.append({"round": rnd, "note": "连续重复动作，提前终止"})
                break

        return {"error": "未完成（达到交互上限或被终止）", "history": history}, history
