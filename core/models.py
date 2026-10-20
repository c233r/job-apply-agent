# -*- coding: utf-8 -*-
"""数据模型定义。"""
from __future__ import annotations

from dataclasses import dataclass, field, fields

# 归一化后的公司/岗位记录字段（顺序即 CSV 列顺序）
CANONICAL_FIELDS = [
    "source_doc", "sheet_name", "company_name", "position", "location",
    "apply_link", "official_link", "referral_code", "target", "deadline",
    "start_date", "industry", "batch", "company_desc", "contact", "note",
    "record_id", "already_applied", "applied_match",
]


@dataclass
class CompanyRecord:
    """一条归一化的公司/岗位投递记录。"""

    source_doc: str = ""
    sheet_name: str = ""
    company_name: str = ""
    position: str = ""
    location: str = ""
    apply_link: str = ""
    official_link: str = ""
    referral_code: str = ""
    target: str = ""
    deadline: str = ""
    start_date: str = ""
    industry: str = ""
    batch: str = ""
    company_desc: str = ""
    contact: str = ""
    note: str = ""
    record_id: str = ""
    sheet_id: str = ""
    already_applied: bool = False      # 是否已投递（公司名相似度 >= 阈值）
    applied_match: str = ""            # 命中「已投递」清单的名字
    extra: dict = field(default_factory=dict)  # 未映射到标准字段的原始列

    def to_dict(self) -> dict:
        d = {f.name: getattr(self, f.name) for f in fields(self)}
        return d


@dataclass
class MatchResult:
    """简历 × 岗位 匹配结果（阶段4产出）。"""

    company_name: str = ""
    job_title: str = ""
    score: float = 0.0
    matched: str = ""   # 命中点
    gaps: str = ""      # 差距点
    advice: str = ""    # 建议
