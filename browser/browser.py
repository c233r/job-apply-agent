# -*- coding: utf-8 -*-
"""Playwright 浏览器封装：启动、页面快照（供 LLM 决策）。"""
from __future__ import annotations

import os
from typing import Any


class BrowserNotInstalled(RuntimeError):
    pass


# 登录墙检测：URL / 标题 / 页面正文里的强特征（避免把普通页面的「登录」按钮误判）
_LOGIN_URL_KEYS = ("login", "signin", "sign-in", "passport", "sso", "auth/")
_LOGIN_TEXT_KEYS = ("扫码登录", "请先登录", "请登录后", "手机号登录", "账号登录",
                    "验证码登录", "密码登录", "请完成验证", "拖动滑块", "安全验证",
                    "captcha", "sign in", "log in", "please log in")


def needs_login(page) -> bool:
    """判断当前页面是否是登录/验证墙。"""
    try:
        url = (page.url or "").lower()
        if any(k in url for k in _LOGIN_URL_KEYS):
            return True
        title = (page.title() or "").lower()
        if "登录" in title or "login" in title or "sign in" in title:
            return True
        text = (page.inner_text("body") or "")[:4000].lower()
        return any(k in text for k in _LOGIN_TEXT_KEYS)
    except Exception:
        return False


class BrowserSession:
    """一个浏览器会话。支持复用登录态（user_data_dir）。"""

    def __init__(self, channel: str = "chrome", headless: bool = False,
                 user_data_dir: str | None = None, timeout_ms: int = 30000):
        self.channel = channel
        self.headless = headless
        self.user_data_dir = user_data_dir
        self._annotated: list[dict] = []
        self.timeout_ms = timeout_ms
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None

    # ---- 生命周期 ----
    def start(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:
            raise BrowserNotInstalled(
                "缺少 playwright。请先安装：pip install playwright，"
                "然后用系统 Chrome 即可（channel=chrome 免下载浏览器）。详见 requirements.txt"
            ) from e
        self._pw = sync_playwright().start()
        launch = dict(headless=self.headless, timeout=self.timeout_ms, channel=self.channel)
        if self.user_data_dir:
            self._context = self._pw.chromium.launch_persistent_context(
                os.path.abspath(self.user_data_dir), **launch)
            self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        else:
            self._browser = self._pw.chromium.launch(**launch)
            self._context = self._browser.new_context()
            self._page = self._context.new_page()
        return self._page

    @property
    def page(self):
        if self._page is None:
            self.start()
        return self._page

    def close(self):
        for closer in (lambda: self._context.close() if self._context else None,
                       lambda: self._browser.close() if self._browser else None,
                       lambda: self._pw.stop() if self._pw else None):
            try:
                closer()
            except Exception:
                pass

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *args):
        self.close()

    # ---- 截图 ----
    def screenshot(self, path: str | None = None, quality: int = 60,
                   full_page: bool = False) -> bytes:
        """截当前页面（JPEG 压缩），返回字节；path 不为空则保存；full_page=True 截整页长图。

        兼容不同 Playwright 版本：正常情况下返回 bytes，个别情况可能返回 base64 字符串。
        """
        try:
            data = self.page.screenshot(type="jpeg", quality=quality, full_page=full_page)
        except Exception:
            data = self.page.screenshot(full_page=full_page)
        if isinstance(data, str):
            import base64
            try:
                data = base64.b64decode(data.split(",")[-1])
            except Exception:
                data = data.encode("utf-8", "ignore")
        if data is None:
            data = b""
        if not isinstance(data, (bytes, bytearray)):
            raise RuntimeError("截图返回类型异常：%s" % type(data).__name__)
        data = bytes(data)
        if path:
            try:
                d = os.path.dirname(os.path.abspath(path))
                if d:
                    os.makedirs(d, exist_ok=True)
                with open(path, "wb") as f:
                    f.write(data)
            except Exception:
                pass
        return data

    # ---- 标签页 ----
    def open_tab(self):
        """新开一个标签页并切为当前页（保留之前页面）；失败则沿用当前页。"""
        try:
            if self._context is None:
                self.start()
            page = self._context.new_page()
            self._page = page
            return page
        except Exception:
            return self.page

    def tab_count(self) -> int:
        try:
            return len(self._context.pages) if self._context else 0
        except Exception:
            return 0

    # ---- 整页截图 + 可交互元素编号（Set-of-Mark，点击走元素句柄） ----
    def annotate_screenshot(self, max_elements: int = 80):
        """整页长截图，并给可交互元素画红框编号。

        - 截图是整页的，屏幕外的元素也会编号；
        - 元素句柄保存在 self._annotated；
        - 点击时用句柄 element.click()，Playwright 会自动把元素滚到可视区，无需手动 scroll。
        返回 (jpeg_bytes, meta)，meta 每项：{i, tag, text, x, y, w, h, cx, cy}。
        """
        import io as _io
        sel = "a, button, input, select, textarea, [role=button], [onclick]"
        try:
            handles = self.page.query_selector_all(sel)
        except Exception:
            handles = []
        elements = []
        for h in handles:
            if len(elements) >= max_elements:
                break
            try:
                box = h.bounding_box()
                if not box or box["width"] < 4 or box["height"] < 4:
                    continue
                text = ""
                try:
                    text = (h.inner_text() or "").strip()[:40]
                except Exception:
                    pass
                if not text:
                    try:
                        text = ((h.get_attribute("value") or h.get_attribute("aria-label")
                                 or h.get_attribute("placeholder") or "")).strip()[:40]
                    except Exception:
                        pass
                try:
                    tag = h.evaluate("e => e.tagName.toLowerCase()")
                except Exception:
                    tag = ""
                elements.append({
                    "i": len(elements), "tag": tag, "text": text, "handle": h,
                    "x": int(box["x"]), "y": int(box["y"]),
                    "w": int(box["width"]), "h": int(box["height"]),
                    "cx": int(box["x"] + box["width"] / 2),
                    "cy": int(box["y"] + box["height"] / 2),
                })
            except Exception:
                continue
        self._annotated = elements
        png = self.page.screenshot(type="jpeg", quality=70, full_page=True)
        try:
            from PIL import Image, ImageDraw
            img = Image.open(_io.BytesIO(png)).convert("RGB")
            draw = ImageDraw.Draw(img)
            for e in elements:
                x, y, w, h = e["x"], e["y"], e["w"], e["h"]
                if y + h < 0 or y > img.height:
                    continue
                draw.rectangle([x, y, x + w, y + h], outline=(255, 0, 0), width=2)
                draw.text((x + 2, max(0, y + 1)), str(e["i"]), fill=(255, 0, 0))
            buf = _io.BytesIO()
            img.save(buf, format="JPEG", quality=70)
            png = buf.getvalue()
        except Exception:
            pass
        meta = [{k: v for k, v in e.items() if k != "handle"} for e in elements]
        return png, meta

    # ---- 快照 ----
    def _visible_text(self, limit: int = 4000) -> str:
        try:
            txt = self.page.inner_text("body")
            return txt[:limit]
        except Exception:
            return ""

    def _links(self, limit: int = 40) -> list[dict]:
        try:
            return self.page.eval_on_selector_all(
                "a[href]",
                """els => els.map(e => ({
                       text: (e.innerText || '').trim().slice(0, 60),
                       href: e.href
                   })).filter(x => x.href).slice(0, %d)""" % limit)
        except Exception:
            return []

    def _buttons(self, limit: int = 40) -> list[dict]:
        try:
            return self.page.eval_on_selector_all(
                "button, [role='button'], input[type='submit'], input[type='button']",
                """els => els.slice(0, %d).map(e => ({
                       text: (e.innerText || e.value || e.getAttribute('aria-label') || '').trim().slice(0, 60)
                   })).filter(x => x.text)""" % limit)
        except Exception:
            return []

    def _forms(self, limit: int = 8) -> list[dict]:
        try:
            return self.page.eval_on_selector_all(
                "form",
                """els => els.slice(0, %d).map((f, fi) => ({
                       form_index: fi,
                       action: f.action || '',
                       fields: Array.from(f.querySelectorAll('input,select,textarea')).slice(0, 60).map(el => {
                           let lab = '';
                           if (el.labels && el.labels[0]) lab = el.labels[0].innerText.trim();
                           if (!lab && el.name) lab = el.name;
                           return {
                               label: lab, name: el.name || '',
                               type: el.type || el.tagName.toLowerCase(),
                               placeholder: el.placeholder || '',
                               value: el.value || '',
                               required: !!el.required,
                               options: el.tagName === 'SELECT'
                                   ? Array.from(el.options).slice(0, 30).map(o => o.text.trim())
                                   : []
                           };
                       })
                   }))""" % limit)
        except Exception:
            return []

    def snapshot(self, max_text: int = 4000, max_links: int = 40, max_forms: int = 8) -> dict[str, Any]:
        """当前页面快照，供 LLM 决策。"""
        return {
            "url": self.page.url,
            "title": self.page.title(),
            "visible_text": self._visible_text(max_text),
            "links": self._links(max_links),
            "buttons": self._buttons(),
            "forms": self._forms(max_forms),
        }


