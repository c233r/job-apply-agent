# -*- coding: utf-8 -*-
"""报告导出：把匹配结果与投递记录输出为 Markdown。"""
from __future__ import annotations

import json
import os
from typing import Any


def load_json(path: str) -> Any:
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def export_markdown(out_dir: str) -> str:
    """汇总 data/output 下的匹配报告与投递记录为 Markdown。"""
    match_report = load_json(os.path.join(out_dir, "match_report.json")) or []
    fill_summary = load_json(os.path.join(out_dir, "fill_summary.json")) or {}

    lines = ["# 简历投递报告", ""]
    if fill_summary:
        lines.append("## 投递概况")
        lines.append("- 选中公司：%s" % fill_summary.get("selected", 0))
        lines.append("- 已填写：%s" % fill_summary.get("applied", 0))
        if fill_summary.get("companies"):
            lines.append("- 名单：%s" % "、".join(fill_summary["companies"]))
        lines.append("")

    lines.append("## 岗位匹配度（按分数降序）")
    lines.append("| 排名 | 公司 | 岗位 | 匹配分 | 命中要点 | 差距 | 建议 |")
    lines.append("|---|---|---|---|---|---|---|")
    for i, r in enumerate(match_report, 1):
        lines.append("| %d | %s | %s | %s | %s | %s | %s |" % (
            i, r.get("company_name", ""), r.get("job_title", ""),
            r.get("score", ""), r.get("matched", ""), r.get("gaps", ""), r.get("advice", "")))
    lines.append("")

    path = os.path.join(out_dir, "report.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return path
