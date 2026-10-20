# -*- coding: utf-8 -*-
"""已投递去重：读取已投递清单（xlsx/csv/txt），按公司名相似度判定是否已投递。

- xlsx 解析仅用标准库（zipfile + xml.etree），不新增依赖；
- 公司名先清洗（去括号、去后缀、去连字符项目名）再做 difflib 相似度比较；
- 相似度 >= 阈值（默认 0.85）判定为「已投递」，跳过岗位筛选。
"""
from __future__ import annotations

import csv
import os
import re
import zipfile
from difflib import SequenceMatcher

_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


# ---------- 公司名清洗 ----------
def clean_company_name(name: str) -> str:
    """去掉括号内容、常见后缀（开启时间/热门优先投等）与连字符后的项目名。"""
    n = (name or "").strip()
    n = re.sub(r"[（(][^（）()]*[)）]", "", n)
    n = re.sub(r"(热门优先投|优先投|开启$|开启中|内推|官宣)$", "", n)
    n = n.split("-")[0].split("—")[0].split("｜")[0].strip()
    return n or (name or "").strip()


def normalize(name: str) -> str:
    """归一化：去空格与常见标点、转小写，便于相似度比较。"""
    s = clean_company_name(name).lower()
    s = re.sub(r"[\s\-_（）()【】\[\]·•,，.。/、|｜]", "", s)
    return s


# ---------- 读取已投递清单 ----------
def _read_xlsx_rows(path: str) -> list[list[str]]:
    """用标准库读取 xlsx 第一个工作表的所有行。"""
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        sst: list[str] = []
        if "xl/sharedStrings.xml" in names:
            import xml.etree.ElementTree as ET
            root = ET.fromstring(z.read("xl/sharedStrings.xml"))
            for si in root.findall(_NS + "si"):
                sst.append("".join(t.text or "" for t in si.iter(_NS + "t")))
        sheet_files = sorted(
            n for n in names if re.match(r"xl/worksheets/sheet\d+\.xml$", n))
        if not sheet_files:
            return []
        import xml.etree.ElementTree as ET
        root = ET.fromstring(z.read(sheet_files[0]))

        def col_idx(ref: str) -> int:
            m = re.match(r"([A-Z]+)", ref or "")
            if not m:
                return 0
            n = 0
            for ch in m.group(1):
                n = n * 26 + (ord(ch) - 64)
            return n - 1

        rows: list[list[str]] = []
        for row in root.iter(_NS + "row"):
            cells: dict[int, str] = {}
            for c in row.iter(_NS + "c"):
                idx = col_idx(c.get("r", ""))
                t = c.get("t")
                v = c.find(_NS + "v")
                val = v.text if v is not None else ""
                if t == "s" and val:
                    try:
                        val = sst[int(val)]
                    except (ValueError, IndexError):
                        pass
                elif t == "inlineStr":
                    is_ = c.find(_NS + "is")
                    if is_ is not None:
                        val = "".join(tt.text or "" for tt in is_.iter(_NS + "t"))
                cells[idx] = val or ""
            if cells:
                width = max(cells) + 1
                rows.append([cells.get(i, "") for i in range(width)])
        return rows


def load_applied_companies(path: str) -> list[str]:
    """读取已投递公司名列表。支持 xlsx / csv / txt。"""
    if not path or not os.path.exists(path):
        return []
    ext = os.path.splitext(path)[1].lower()

    if ext == ".xlsx":
        rows = _read_xlsx_rows(path)
        if not rows:
            return []
        header = [str(x).strip() for x in rows[0]]
        col = None
        for i, h in enumerate(header):
            if "公司" in h:
                col = i
                break
        if col is None:
            col = 1  # 默认第 2 列（B 列）
        names = []
        for r in rows[1:]:
            if col < len(r) and str(r[col]).strip():
                names.append(str(r[col]).strip())
        # 去重（保持顺序）
        seen, out = set(), []
        for n in names:
            if n not in seen:
                seen.add(n)
                out.append(n)
        return out

    if ext == ".csv":
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.reader(f)
            rows = list(reader)
        if not rows:
            return []
        header = [h.strip() for h in rows[0]]
        col = next((i for i, h in enumerate(header) if "公司" in h), 1)
        return [str(r[col]).strip() for r in rows[1:] if col < len(r) and str(r[col]).strip()]

    if ext in (".txt", ".md"):
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return [l.strip() for l in f if l.strip()]

    return []


# ---------- 相似度判定 ----------
def similarity(a: str, b: str) -> float:
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return 0.0
    return SequenceMatcher(None, na, nb).ratio()


def best_match(name: str, applied: list[str]) -> tuple[str, float]:
    """返回 (最相似的公司名, 相似度)。"""
    best, best_r = "", 0.0
    for an in applied:
        r = similarity(name, an)
        if r > best_r:
            best_r, best = r, an
    return best, round(best_r, 3)


def is_applied(name: str, applied: list[str], threshold: float = 0.85) -> tuple[bool, str, float]:
    """判定公司是否已投递（字符相似度兜底）。返回 (是否已投递, 最相似名字, 相似度)。"""
    if not name or not applied:
        return False, "", 0.0
    best, r = best_match(name, applied)
    return (r >= threshold), best, r


# ---------- 余弦相似度 + embedding ----------
def cosine(u: list[float], v: list[float]) -> float:
    """余弦相似度。"""
    if not u or not v or len(u) != len(v):
        return 0.0
    dot = sum(a * b for a, b in zip(u, v))
    nu = sum(a * a for a in u) ** 0.5
    nv = sum(b * b for b in v) ** 0.5
    if nu == 0 or nv == 0:
        return 0.0
    return dot / (nu * nv)


class EmbeddingStore:
    """SQLite 向量缓存：避免重复调用 embedding 接口。"""

    def __init__(self, path: str):
        import sqlite3
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS embeddings ("
            "model TEXT, text TEXT, vec BLOB, PRIMARY KEY(model, text))")
        self.conn.commit()

    def get_many(self, model: str, texts: list[str]) -> dict[str, list[float]]:
        """批量取向量：小批量走索引查询，大批量走全表扫描（避免 N² 往返）。"""
        import struct
        if not texts:
            return {}
        out: dict[str, list[float]] = {}
        if len(texts) <= 128:
            for t in texts:
                row = self.conn.execute(
                    "SELECT vec FROM embeddings WHERE model=? AND text=?", (model, t)).fetchone()
                if row and row[0]:
                    b = row[0]
                    out[t] = list(struct.unpack("<%df" % (len(b) // 4), b))
            return out
        want = set(texts)
        rows = self.conn.execute(
            "SELECT text, vec FROM embeddings WHERE model=?", (model,)).fetchall()
        for text, blob in rows:
            if text in want and blob:
                out[text] = list(struct.unpack("<%df" % (len(blob) // 4), blob))
        return out

    def put_many(self, model: str, d: dict[str, list[float]]) -> None:
        import struct
        for t, v in d.items():
            blob = struct.pack("<%df" % len(v), *v)
            self.conn.execute(
                "INSERT OR REPLACE INTO embeddings(model,text,vec) VALUES(?,?,?)", (model, t, blob))
        self.conn.commit()


def embed_texts(client, texts: list[str], store: EmbeddingStore | None) -> dict[str, list[float]]:
    """批量求向量（带缓存）。"""
    model = client.model
    cached = store.get_many(model, texts) if store else {}
    missing = [t for t in texts if t not in cached]
    if missing:
        print("[embedding] 新计算 %d 个向量（%d 个命中缓存）..." % (
            len(missing), len(texts) - len(missing)), flush=True)
        vecs = client.embed(missing)
        new = dict(zip(missing, vecs))
        if store:
            store.put_many(model, new)
        cached.update(new)
    return cached


def classify_embeddings(fetched_names: list[str], applied_names: list[str],
                        client, threshold: float = 0.85,
                        store: EmbeddingStore | None = None) -> dict[str, tuple[bool, str, float]]:
    """用 embedding 余弦相似度判定：返回 {原名: (是否已投递, 最相似已投递名, 相似度)}。

    相似度用 numpy 向量化计算（纯 Python 在 ~7000×300×512 规模下要几分钟）。
    """
    applied_pairs = [(orig, clean_company_name(orig)) for orig in applied_names]
    fetched_clean = {n: clean_company_name(n) for n in fetched_names}
    all_texts = list(dict.fromkeys([c for _, c in applied_pairs] + list(fetched_clean.values())))
    print("[embedding] 计算 %d 个公司名的向量（模型 %s）..." % (len(all_texts), client.model), flush=True)
    vecs = embed_texts(client, all_texts, store)

    # numpy 向量化
    try:
        import numpy as np
        applied_clean = [c for _, c in applied_pairs if c in vecs]
        applied_origs = [orig for orig, c in applied_pairs if c in vecs]
        fetched_ok = [n for n in fetched_names if fetched_clean[n] in vecs]
        out: dict[str, tuple[bool, str, float]] = {}
        if applied_clean and fetched_ok:
            A = np.asarray([vecs[c] for c in applied_clean], dtype="float32")
            F = np.asarray([vecs[fetched_clean[n]] for n in fetched_ok], dtype="float32")
            A /= np.linalg.norm(A, axis=1, keepdims=True) + 1e-12
            F /= np.linalg.norm(F, axis=1, keepdims=True) + 1e-12
            sims = F @ A.T                      # (M, N)
            idx = sims.argmax(axis=1)
            best_s = sims[np.arange(len(fetched_ok)), idx]
            for i, n in enumerate(fetched_ok):
                j = int(idx[i])
                s = float(best_s[i])
                out[n] = (s >= threshold, applied_origs[j], round(s, 3))
        for n in fetched_names:
            if n not in out:
                out[n] = (False, "", 0.0)
        return out
    except ImportError:
        pass

    # 纯 Python 回退（小规模可用）
    applied_vecs = [(orig, vecs[c]) for orig, c in applied_pairs if c in vecs]
    out = {}
    for n in fetched_names:
        v = vecs.get(fetched_clean[n])
        best, best_s = "", 0.0
        if v is not None:
            for orig, av in applied_vecs:
                s = cosine(v, av)
                if s > best_s:
                    best_s, best = s, orig
        out[n] = (best_s >= threshold, best, round(best_s, 3))
    return out


def classify_char(fetched_names: list[str], applied_names: list[str],
                  threshold: float = 0.85) -> dict[str, tuple[bool, str, float]]:
    """字符相似度批量判定（无 embedding 时兜底）。"""
    return {n: is_applied(n, applied_names, threshold) for n in fetched_names}


def classify_company_names(fetched_names: list[str], applied_names: list[str],
                           cfg=None, threshold: float = 0.85,
                           store_path: str | None = None) -> tuple[dict, str]:
    """按配置选择后端：有 embedding 配置则用余弦相似度，否则字符相似度兜底。

    返回 (分类结果 dict, 使用的后端名 'local'|'api'|'char'|'none')。
    """
    fetched_names = list(dict.fromkeys(fetched_names))
    applied_names = list(dict.fromkeys(applied_names))
    if not fetched_names or not applied_names:
        return {}, "none"
    try:
        from llm.embeddings import LocalEmbeddingClient, build_embedding_client
        client = build_embedding_client(cfg) if cfg is not None else None
        if client is not None and client.configured:
            store = EmbeddingStore(store_path) if store_path else None
            result = classify_embeddings(fetched_names, applied_names, client, threshold, store)
            backend = "local" if isinstance(client, LocalEmbeddingClient) else "api"
            return result, backend
    except Exception as e:
        print("[warn] embedding 判定不可用，回退字符相似度：%s" % e)
    return classify_char(fetched_names, applied_names, threshold), "char"
