# -*- coding: utf-8 -*-
"""岗位意向偏好：开发 / 数据 / 测试 优先，算法岗降权。"""
from __future__ import annotations

# 默认：开发/数据/测试方向关键词
DEV_KEYWORDS = [
    "开发", "研发", "后端", "前端", "服务端", "客户端", "全栈", "软件", "测试", "数据",
    "架构", "运维", "sre", "devops",
    "developer", "software", "backend", "frontend", "fullstack", "test", "qa", "data",
]

# 默认：算法方向关键词（命中则降权）
ALGO_KEYWORDS = [
    "算法", "机器学习", "深度学习", "大模型", "多模态", "计算机视觉", "机器视觉",
    "推荐", "搜索", "广告", "nlp", "cv", "aigc", "llm", "agent",
    "algorithm", "machine learning", "deep learning",
]


# 匹配度档位：低 0-35 / 中 35-65 / 高 65-100
TIER_LOW = 35.0
TIER_HIGH = 65.0


def tier(score: float) -> str:
    """把匹配分映射为「低/中/高」三档。"""
    s = float(score or 0)
    if s < TIER_LOW:
        return "低"
    if s < TIER_HIGH:
        return "中"
    return "高"


def adjust(job: dict, score: float, pref: dict | None = None) -> tuple[float, str]:
    """按岗位意向调整匹配分。返回 (调整后分数, 说明)。"""
    pref = pref or {}
    dev_keys = pref.get("dev_keywords") or DEV_KEYWORDS
    algo_keys = pref.get("algo_keywords") or ALGO_KEYWORDS
    text = ("%s %s %s" % (job.get("title", ""), job.get("responsibilities", ""),
                          job.get("requirements", "")))[:600].lower()

    algo_hit = [k for k in algo_keys if k.lower() in text]
    if algo_hit:
        pen = float(pref.get("algo_penalty", 30))
        if pen <= 0:  # 不降权：直接用真实 LLM 分
            return score, ""
        return max(0.0, score - pen), "算法岗降权(-%.0f)" % pen

    dev_hit = [k for k in dev_keys if k.lower() in text]
    if dev_hit:
        bonus = float(pref.get("dev_bonus", 0))
        if bonus:
            return min(100.0, score + bonus), "开发/数据/测试优先(+%.0f)" % bonus
        return score, "开发/数据/测试优先"
    return score, ""
