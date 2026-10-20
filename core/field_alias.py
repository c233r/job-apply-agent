# -*- coding: utf-8 -*-
"""字段别名映射：把不同子表的列名归一化到标准字段。

不同子表列名不一致（如公司名可能是「企业名称」「公司名称」或「公司」），
这里用「精确匹配优先、子串匹配兜底」做归一化。
"""
from __future__ import annotations

from .models import CompanyRecord

# 标准字段 -> 候选列名关键词（按优先级）。
# 关键词以 "=" 开头表示「精确匹配列名」（避免如「公司」误匹配「公司介绍」），否则为子串匹配。
FIELD_ALIASES: dict[str, list[str]] = {
    "company_name": ["企业名称", "公司名称", "=公司"],
    "position": ["招聘岗位", "岗位名称", "详细职位", "岗位", "职位"],
    "location": ["工作地点", "工作地", "=地点", "=城市"],
    "apply_link": ["投递链接"],
    "official_link": ["官方公告", "官方投递", "内推链接/官网", "内推链接", "官网", "链接"],
    "referral_code": ["内推码"],
    "target": ["招聘对象", "毕业时间要求", "毕业时间"],
    "deadline": ["截止时间", "截止日期"],
    "start_date": ["开启时间", "开启日期", "开始时间"],
    "industry": ["所在行业", "行业大类", "企业性质", "企业类型", "行业"],
    "batch": ["批次", "内推类型"],
    "company_desc": ["公司介绍", "企业介绍", "公司简介"],
    "contact": ["对接人", "内推联系人", "联系人"],
    "note": ["投递注意事项", "备注", "注意事项"],
}


def _match(row: dict, keywords: list[str], used_cols: set) -> tuple[str | None, str | None]:
    """在 row 中找第一个命中关键词且未被占用的非空列，返回 (列名, 值)。"""
    for kw in keywords:
        exact = kw.startswith("=")
        key = kw[1:] if exact else kw
        for col, val in row.items():
            if col == "_record_id" or col in used_cols or not val:
                continue
            hit = (col.strip() == key) if exact else (key in col)
            if hit:
                return col, str(val).strip()
    return None, None


def normalize_row(
    row: dict,
    source_doc: str,
    sheet_name: str,
    sheet_id: str,
) -> CompanyRecord:
    """把一行原始数据归一化为 CompanyRecord。"""
    rec = CompanyRecord(source_doc=source_doc, sheet_name=sheet_name, sheet_id=sheet_id)
    used: set[str] = set()
    for canon, keywords in FIELD_ALIASES.items():
        col, val = _match(row, keywords, used)
        if col is not None:
            setattr(rec, canon, val)
            used.add(col)
    rec.record_id = row.get("_record_id", "")
    rec.extra = {k: v for k, v in row.items() if k != "_record_id" and k not in used}
    return rec
