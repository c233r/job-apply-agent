# -*- coding: utf-8 -*-
"""配置加载。默认读取仓库根目录的 config.yaml；没有则使用内置默认值。"""
from __future__ import annotations

import os

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DEFAULTS = {
    # 腾讯文档智能表分享链接：在 config.yaml 的 docs 里填写自己的表
    "docs": [],
    "applied_xlsx": "data/applied/applied_companies.xlsx",
    "applied_threshold": 0.85,
    "llm": {
        "provider": "deepseek",
        "base_url": "https://api.deepseek.com",
        "api_key": "",
        "model": "deepseek-chat",
    },
    "browser": {
        "channel": "chrome",
        "headless": False,
        "user_data_dir": "data/browser_profile",
        "timeout_ms": 30000,
    },
    "agent": {
        "max_rounds": 10,
    },
    "matching": {
        "min_score": 60,
    },
    "resume": {
        "path": "resume.pdf",
    },
    "paths": {
        "data_raw": "data/raw",
        "data_enriched": "data/enriched",
        "data_jobs": "data/jobs",
        "data_cache": "data/cache",
        "data_output": "data/output",
    },
}


class Config:
    """dict + 属性访问的配置对象。"""

    def __init__(self, data: dict):
        self._data = data

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        v = self._data.get(name)
        if isinstance(v, dict):
            return Config(v)
        return v

    def get(self, key, default=None):
        return self._data.get(key, default)


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: str | None = None) -> Config:
    if path is None:
        default = os.path.join(ROOT, "config.yaml")
        path = default if os.path.exists(default) else None

    data = dict(DEFAULTS)
    if path and os.path.exists(path):
        if yaml is None:
            raise RuntimeError("缺少 PyYAML，请先安装依赖（见 requirements.txt）")
        with open(path, "r", encoding="utf-8") as f:
            user = yaml.safe_load(f) or {}
        data = _deep_merge(data, user)
    return Config(data)


def abspath(rel: str) -> str:
    """相对路径转工作区绝对路径。"""
    if os.path.isabs(rel):
        return rel
    return os.path.join(ROOT, rel)
