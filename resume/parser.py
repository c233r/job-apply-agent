# -*- coding: utf-8 -*-
"""简历解析：提取文本 + 结构化画像（LLM 优先，正则兜底）。

PDF 解析优先用 pypdf（纯 Python），回退 pdfplumber；Word 用 python-docx。
"""
from __future__ import annotations

import os
import re
from typing import Any


def extract_text(path: str) -> str:
    """按扩展名提取简历纯文本。支持 txt/md/pdf/docx。"""
    if not os.path.exists(path):
        raise FileNotFoundError("简历文件不存在: %s" % path)
    ext = os.path.splitext(path)[1].lower()
    if ext in (".txt", ".md"):
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()
    if ext == ".pdf":
        return _extract_pdf(path)
    if ext == ".docx":
        try:
            import docx
        except ImportError:
            raise RuntimeError("解析 Word 需要 python-docx（见 requirements.txt）")
        d = docx.Document(path)
        return "\n".join(p.text for p in d.paragraphs)
    raise ValueError("不支持的简历格式: %s（支持 txt/pdf/docx）" % ext)


def _extract_pdf(path: str) -> str:
    # 优先 pypdf（纯 Python）
    try:
        from pypdf import PdfReader
        parts = []
        reader = PdfReader(path)
        for page in reader.pages:
            parts.append(page.extract_text() or "")
        txt = "\n".join(parts)
        if txt.strip():
            return txt
    except ImportError:
        pass
    # 回退 pdfplumber
    try:
        import pdfplumber
        parts = []
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                parts.append(page.extract_text() or "")
        return "\n".join(parts)
    except ImportError:
        raise RuntimeError("解析 PDF 需要 pypdf 或 pdfplumber（见 requirements.txt）")


# ---- 正则兜底：无需 LLM 提取基础字段 ----
def extract_basic_profile(text: str, filename: str = "") -> dict:
    """用正则从简历文本提取基础画像（姓名/电话/邮箱/学校/学历/专业/城市）。"""
    prof = {
        "name": "", "phone": "", "email": "", "city": "",
        "school": "", "degree": "", "major": "", "summary": "",
    }
    m = re.search(r"1[3-9]\d{9}", text)
    if m:
        prof["phone"] = m.group(0)
    m = re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", text)
    if m:
        prof["email"] = m.group(0)
    m = re.search(r"(博士|硕士|本科|大专)", text)
    if m:
        prof["degree"] = m.group(0)
    # 学校：第一个「中文+大学/学院」
    m = re.search(r"([\u4e00-\u9fa5]{2,12}(?:大学|学院))", text)
    if m:
        prof["school"] = m.group(1)
    # 专业：学校后跟随的专业，常见模式「计算机科学与技术」「人工智能」
    m = re.search(r"(计算机科学与技术|人工智能|软件工程|数据科学[与和]?大数据技术?|通信工程|电子信息工程)", text)
    if m:
        prof["major"] = m.group(1)
    # 姓名：文件名第一段（如「张三-示例大学-后端开发.pdf」）
    base = os.path.splitext(os.path.basename(filename))[0]
    name = base.split("-")[0].split("_")[0].strip()
    if name and not name[0].isdigit():
        prof["name"] = name
    return prof


PROFILE_PROMPT = """你是简历解析器。从下面简历文本中提取结构化信息，输出 JSON：
{
  "name": "姓名", "phone": "电话", "email": "邮箱", "city": "所在城市",
  "education": "学历与学校，如：硕士/示例大学/计算机科学与技术",
  "school": "最高学历学校", "degree": "最高学历（硕士/本科）", "major": "专业",
  "experiences": "工作与实习经历（按时间倒序简要）", "projects": "项目经历",
  "skills": "技能（逗号分隔）", "certificates": "证书/奖项", "summary": "一句话自我简介"
}
缺失字段填空字符串。只输出 JSON。"""


def parse_profile(text: str, llm: Any, filename: str = "") -> dict:
    """LLM 归一化画像；无 key 或失败时回退到正则基础画像。"""
    base = extract_basic_profile(text, filename)
    if llm is None or not getattr(llm, "configured", True):
        return base
    try:
        messages = [
            {"role": "system", "content": PROFILE_PROMPT},
            {"role": "user", "content": text[:8000]},
        ]
        data = llm.chat_json(messages, max_tokens=2000)
        merged = dict(base)
        merged.update({k: v for k, v in (data or {}).items() if v})
        return merged
    except Exception:
        return base
