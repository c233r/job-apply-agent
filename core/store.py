# -*- coding: utf-8 -*-
"""本地落盘：CSV / JSON / JSONL 输出，保证目录存在。"""
from __future__ import annotations

import csv
import json
import os
from typing import Iterable


def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def write_csv(records: Iterable[dict], path: str, fields: list[str] | None = None) -> str:
    """写 CSV（utf-8-sig 便于 Excel 直接打开）。fields 为空时取首行的键。"""
    records = list(records)
    ensure_dir(os.path.dirname(path))
    if fields is None:
        fields = list(records[0].keys()) if records else []
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(fields)
        for r in records:
            w.writerow([r.get(k, "") for k in fields])
    return path


def write_json(obj, path: str) -> str:
    ensure_dir(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    return path


def write_jsonl(records: Iterable[dict], path: str) -> str:
    ensure_dir(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return path
