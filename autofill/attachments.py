# -*- coding: utf-8 -*-
"""投递附件：列出证明材料目录中的可上传文件。"""
from __future__ import annotations

import os

ATTACH_EXTS = (".pdf", ".jpg", ".jpeg", ".png", ".doc", ".docx")


def attachment_files(attachments_dir: str) -> list[str]:
    """列出附件目录里的证明材料文件路径（证书/证明等）。"""
    if not os.path.isdir(attachments_dir):
        return []
    return sorted(
        os.path.join(attachments_dir, f)
        for f in os.listdir(attachments_dir)
        if f.lower().endswith(ATTACH_EXTS)
    )
