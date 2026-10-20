# -*- coding: utf-8 -*-
"""DeepSeek（OpenAI 兼容）调用层：chat 与 JSON 输出。

- chat：POST {base_url}/chat/completions
- 支持按 session_id 隔离多轮上下文，以及同步 / 协程两种调用方式。
"""
from __future__ import annotations

import json
import re
from typing import Any

import requests


class LLMError(Exception):
    pass


def parse_json_loose(content: str):
    """健壮解析模型返回的 JSON：去 markdown 代码块；多余内容只取第一个 JSON 对象。"""
    s = (content or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    try:
        return json.loads(s)
    except json.JSONDecodeError as e:
        last = e
    # 找到第一个 { 或 [ 并只解析该对象（忽略后面的额外内容）
    for i, ch in enumerate(s):
        if ch in "{[":
            try:
                obj, _ = json.JSONDecoder().raw_decode(s[i:])
                return obj
            except json.JSONDecodeError:
                continue
    raise last


class DeepSeekClient:
    def __init__(self, api_key: str, base_url: str = "https://api.deepseek.com",
                 model: str = "deepseek-chat", timeout: int = 60, debug: bool = False,
                 max_tokens: int | None = None):
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.debug = debug
        # 全局输出上限：设置后覆盖各调用点传入的 max_tokens（0/None = 不限制）
        self.max_tokens = max_tokens
        base = base_url.rstrip("/")
        self.chat_url = base + "/chat/completions"
        # 按 session_id 隔离的对话上下文（一个公司一个 session）
        self._sessions: dict[str, list[dict]] = {}

    @staticmethod
    def _text_only(content) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "\n".join(str(p.get("text", "")) for p in content
                             if isinstance(p, dict) and p.get("type") == "text")
        return str(content)

    def _inject_session(self, messages: list[dict], session_id: str | None) -> list[dict]:
        if not session_id:
            return messages
        hist = self._sessions.get(session_id) or []
        if not hist:
            return messages
        system = [m for m in messages if m.get("role") == "system"]
        rest = [m for m in messages if m.get("role") != "system"]
        return system + hist + rest

    def _record_session(self, session_id: str | None, user_msg, assistant: str) -> None:
        if not session_id:
            return
        hist = self._sessions.setdefault(session_id, [])
        hist.append({"role": "user", "content": self._text_only(user_msg)[:2000]})
        hist.append({"role": "assistant", "content": str(assistant)[:2000]})
        if len(hist) > 6:  # 只保留最近 3 轮，控制 token
            del hist[: len(hist) - 6]

    @property
    def configured(self) -> bool:
        # 空值或非 ASCII 占位符（如示例里的中文说明）都视为未配置
        return bool(self.api_key) and self.api_key.isascii()

    def _headers(self) -> dict:
        return {
            "Authorization": "Bearer " + self.api_key,
            "Content-Type": "application/json",
        }

    # ---- chat ----
    def chat(self, messages: list[dict], json_mode: bool = False,
             temperature: float = 0.1, max_tokens: int = 2000, model: str | None = None,
             session_id: str | None = None) -> str:
        """返回模型文本回复；json_mode=True 时返回解析后的 dict。

        model 可覆盖默认模型（例如用单独的多模态模型做「看图决策」）。
        session_id 不为空时，复用该 session 的历史上下文（一个公司一个 id，互相隔离）。
        """
        if not self.configured:
            raise LLMError("未配置 LLM api_key，请在 config.yaml 的 llm.api_key 填写")
        user_msg = messages[-1].get("content") if messages else ""
        messages = self._inject_session(messages, session_id)
        mt = self.max_tokens if self.max_tokens is not None else max_tokens
        body: dict[str, Any] = {
            "model": model or self.model,
            "messages": messages,
            "temperature": temperature,
            "stream": False,
        }
        if mt:
            body["max_tokens"] = mt
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        resp = requests.post(self.chat_url, headers=self._headers(), json=body, timeout=self.timeout)
        if resp.status_code != 200:
            raise LLMError("chat 失败 %s: %s" % (resp.status_code, resp.text[:300]))
        msg = resp.json()["choices"][0].get("message") or {}
        content = msg.get("content") or ""
        reasoning = msg.get("reasoning_content") or ""
        if self.debug:
            tag = model or self.model
            if reasoning:
                print("[LLM %s][reasoning] %s" % (tag, reasoning[:1200]), flush=True)
            print("[LLM %s][content] %s" % (tag, content[:1500]), flush=True)
        self._record_session(session_id, user_msg, content)
        if json_mode:
            return parse_json_loose(content)
        return content

    def chat_json(self, messages: list[dict], temperature: float = 0.1,
                  max_tokens: int = 2000, model: str | None = None,
                  session_id: str | None = None) -> dict:
        return self.chat(messages, json_mode=True, temperature=temperature,
                         max_tokens=max_tokens, model=model, session_id=session_id)

    # ---- 协程版本（用 to_thread 包装阻塞请求）----
    async def achat(self, messages: list[dict], json_mode: bool = False,
                    temperature: float = 0.1, max_tokens: int = 2000,
                    model: str | None = None, session_id: str | None = None):
        import asyncio
        return await asyncio.to_thread(self.chat, messages, json_mode, temperature,
                                       max_tokens, model, session_id)

    async def achat_json(self, messages: list[dict], temperature: float = 0.1,
                         max_tokens: int = 2000, model: str | None = None,
                         session_id: str | None = None) -> dict:
        import asyncio
        return await asyncio.to_thread(self.chat_json, messages, temperature,
                                       max_tokens, model, session_id)


def build_client(cfg) -> DeepSeekClient:
    llm = cfg.get("llm", {})
    # max_tokens: 0 = 不限制（不传该字段，由服务端默认）
    mt = int(llm.get("max_tokens", 0) or 0)
    return DeepSeekClient(
        api_key=llm.get("api_key", "") or "",
        base_url=llm.get("base_url", "https://api.deepseek.com"),
        model=llm.get("model", "deepseek-chat"),
        timeout=int(llm.get("timeout", 60)),
        debug=bool(llm.get("debug", False)),
        max_tokens=mt,
    )
