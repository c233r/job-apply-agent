# -*- coding: utf-8 -*-
"""简历 × 岗位 JD 匹配度检测（LLM 结构化输出）。"""
from __future__ import annotations

from typing import Any

from core.models import MatchResult

SYSTEM = """你是招聘匹配评估助手。对比「简历」与「岗位 JD」，给出匹配度评分与理由。
只输出 JSON：{"score": 0-100 整数, "matched": "命中的要点(分号分隔)",
"gaps": "差距/不足(分号分隔)", "advice": "给求职者的一句话建议"}"""


def _build_messages(resume: str, job: dict) -> list[dict]:
    jd = (
        "岗位名称：%s\n岗位职责：%s\n任职要求：%s\n工作地点：%s"
        % (job.get("title", ""), job.get("responsibilities", ""),
           job.get("requirements", ""), job.get("location", ""))
    )
    return [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": "【简历】\n%s\n\n【岗位 JD】\n%s" % (resume[:6000], jd[:4000])},
    ]


def _to_result(data: dict, job: dict) -> MatchResult:
    try:
        score = float(data.get("score", 0))
    except (TypeError, ValueError):
        score = 0.0
    return MatchResult(
        company_name=job.get("company_name", ""),
        job_title=job.get("title", ""),
        score=score,
        matched=str(data.get("matched", "")),
        gaps=str(data.get("gaps", "")),
        advice=str(data.get("advice", "")),
    )


def _apply_pref(result: MatchResult, job: dict, pref: dict | None) -> MatchResult:
    """按岗位意向调整分数：算法降权，开发/数据/测试优先。"""
    if not pref:
        return result
    from .preference import adjust
    new_score, note = adjust(job, result.score, pref)
    result.score = new_score
    if note:
        result.advice = ("[%s] %s" % (note, result.advice)).strip()
    return result


async def match_async(resume: str, job: dict, llm: Any,
                      pref: dict | None = None) -> MatchResult:
    """对单个岗位做匹配（协程）。"""
    data = await llm.achat_json(_build_messages(resume, job), max_tokens=1200)
    return _apply_pref(_to_result(data, job), job, pref)
