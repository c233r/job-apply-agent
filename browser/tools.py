# -*- coding: utf-8 -*-
"""浏览器动作执行器：把 LLM 返回的动作 JSON 映射为 Playwright 操作。"""
from __future__ import annotations

import time
from typing import Any


class ToolExecutor:
    """执行动作 JSON，返回一句简短结果说明。"""

    def __init__(self, session):
        self.session = session

    @property
    def page(self):
        return self.session.page

    def execute(self, action: dict[str, Any]) -> str:
        a = action.get("action")
        if a == "goto":
            self.page.goto(action.get("url", ""), wait_until="domcontentloaded",
                           timeout=self.session.timeout_ms)
            return "已打开 %s" % self.page.url
        if a == "click":
            return self._click(action)
        if a == "fill":
            self.page.fill(action["selector"], str(action.get("value", "")))
            return "已填写 %s" % action["selector"]
        if a == "select":
            self.page.select_option(action["selector"], str(action.get("value", "")))
            return "已选择 %s" % action.get("value", "")
        if a == "upload":
            files = action.get("files") or []
            if not files:
                raise ValueError("upload 需要 files 列表")
            self.page.set_input_files(action["selector"], files)
            return "已上传 %s" % files
        if a == "type":
            self.page.keyboard.type(str(action.get("text", "")))
            return "已输入文字"
        if a == "scroll":
            direction = action.get("direction", "down")
            amount = action.get("amount", 500)
            self.page.mouse.wheel(0, amount if direction == "down" else -amount)
            return "已滚动"
        if a == "go_back":
            self.page.go_back()
            return "已后退"
        if a == "wait":
            ms = int(action.get("ms", 1000))
            time.sleep(ms / 1000.0)
            return "已等待 %dms" % ms
        if a == "extract_text":
            txt = self.session._visible_text(6000)
            return txt or "（无文本）"
        if a == "list_forms":
            import json
            return json.dumps(self.session._forms(), ensure_ascii=False)
        if a == "screenshot":
            path = self.session.screenshot(action.get("path", "data/output/shot.png"))
            return "已截图 %s" % path
        if a == "done":
            return "done"
        raise ValueError("未知动作: %s" % a)

    def _click(self, action: dict) -> str:
        # 按「编号截图」里的编号点击：用元素句柄 click()，
        # Playwright 会自动把屏幕外的元素滚到可视区，无需手动滑动。
        if action.get("index") is not None:
            idx = int(action["index"])
            els = getattr(self.session, "_annotated", None) or []
            if 0 <= idx < len(els):
                try:
                    els[idx]["handle"].click(timeout=self.session.timeout_ms)
                    return "已点击编号 %d（%s）" % (idx, els[idx].get("text", ""))
                except Exception as e:  # noqa: BLE001
                    return "编号点击失败：%s" % e
            return "编号越界：%d" % idx
        # 坐标点击（备用）
        if action.get("x") is not None and action.get("y") is not None:
            self.page.mouse.click(int(action["x"]), int(action["y"]))
            return "已点击坐标 (%s,%s)" % (action["x"], action["y"])
        selector = action.get("selector")
        text = action.get("text")
        if selector:
            self.page.click(selector, timeout=self.session.timeout_ms)
            return "已点击 %s" % selector
        if text:
            self.page.get_by_text(str(text)).first.click(timeout=self.session.timeout_ms)
            return "已点击文字 %s" % text
        raise ValueError("click 需要 selector 或 text")
