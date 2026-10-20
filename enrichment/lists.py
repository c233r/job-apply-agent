# -*- coding: utf-8 -*-
"""中国500强名单匹配。

用「本地名单」（data/lists/*.txt，一行一个公司名）做精确/包含/相似度匹配；
配置了 embedding 时优先用余弦语义相似度。
"""
from __future__ import annotations

import os
from difflib import SequenceMatcher


# ---------------- 本地名单 ----------------
_SUFFIXES = ["股份有限公司", "有限责任公司", "集团有限公司", "控股有限公司",
             "有限公司", "集团", "公司", "控股", "总公司", "有限"]


def _strip_suffix(s: str) -> str:
    """循环剥离常见公司后缀（集团/股份/有限/公司…），便于「集团/股份/简称」互通。"""
    changed = True
    while changed:
        changed = False
        for suf in _SUFFIXES:
            if s.endswith(suf) and len(s) > len(suf):
                s = s[:-len(suf)]
                changed = True
                break
    return s


def _norm(name: str) -> str:
    from core.dedup import normalize
    return _strip_suffix(normalize(name))


def load_local_list(path: str) -> list[tuple[str, str]]:
    """读取本地名单，返回 [(原名, 归一化名)]。一行一个公司名（支持 CSV 第一列）。"""
    if not path or not os.path.exists(path):
        return []
    out = []
    with open(path, "r", encoding="utf-8-sig", errors="ignore") as f:
        for line in f:
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            name = s.split(",")[0].split("\t")[0].strip()
            if name:
                out.append((name, _norm(name)))
    return out


def match_local(name: str, local_pairs: list[tuple[str, str]],
                threshold: float = 0.85) -> tuple[bool, str, float]:
    """在本地名单里匹配：精确 -> 包含 -> 相似度。返回 (found, 命中名, 相似度)。"""
    cn = _norm(name)
    if not cn or not local_pairs:
        return False, "", 0.0
    best, best_s = "", 0.0
    for orig, nn in local_pairs:
        if nn == cn:
            return True, orig, 1.0
        if len(cn) >= 3 and (cn in nn or nn in cn):
            return True, orig, 0.95
        s = SequenceMatcher(None, cn, nn).ratio()
        if s > best_s:
            best_s, best = s, orig
    return (best_s >= threshold), (best if best_s >= threshold else ""), round(best_s, 3)


# ---- 名单匹配器（embedding 语义优先，回退字符）----
class ListMatcher:
    """公司名 → 本地名单语义匹配。

    配了 embedding 客户端时：把名单向量化（缓存），查询名求向量后算余弦相似度；
    否则回退到 match_local 的精确/包含/字符相似度。
    """

    def __init__(self, pairs, embed_client=None, threshold: float = 0.85, store=None,
                 query_names=None):
        self.pairs = pairs or []
        self.names = [p[0] for p in self.pairs]
        self.norms = [p[1] for p in self.pairs]
        self.embed_client = embed_client
        self.store = store
        self.threshold = threshold
        self._matrix = None
        self._qvecs = None
        if self.pairs and embed_client is not None and getattr(embed_client, "configured", False):
            try:
                # 1) 先把名单 500 个向量一次算完
                self._build_matrix()
                # 2) 再把所有待判公司名的向量一次算完（避免边匹配边算）
                if query_names:
                    self._precompute_queries(query_names)
            except Exception as e:  # noqa: BLE001
                print("[lists] embedding 名单匹配不可用，回退字符匹配：%s" % e, flush=True)
                self._matrix = None
                self._qvecs = None

    def backend(self) -> str:
        return "embedding" if self._matrix is not None else "char"

    def _build_matrix(self) -> None:
        import numpy as np
        from core.dedup import embed_texts
        print("[lists] 预算中国500强名单向量：%d 个..." % len(self.norms), flush=True)
        vec_map = embed_texts(self.embed_client, self.norms, self.store)
        arr = np.asarray([vec_map[n] for n in self.norms], dtype="float32")
        arr /= (np.linalg.norm(arr, axis=1, keepdims=True) + 1e-12)
        self._matrix = arr

    def _precompute_queries(self, names) -> None:
        from core.dedup import embed_texts
        norms = list(dict.fromkeys(_norm(n) for n in names if n))
        print("[lists] 预算待判公司名向量：%d 个..." % len(norms), flush=True)
        self._qvecs = embed_texts(self.embed_client, norms, self.store)

    def match(self, name: str) -> tuple[bool, str, float]:
        if self._matrix is not None:
            return self._match_embedding(name)
        return match_local(name, self.pairs, self.threshold)

    def _match_embedding(self, name: str) -> tuple[bool, str, float]:
        import numpy as np
        from core.dedup import embed_texts
        cn = _norm(name)
        if not cn:
            return False, "", 0.0
        vec = None
        if self._qvecs is not None:
            vec = self._qvecs.get(cn)
        if vec is None:  # 未预算到的名字，兜底现算
            vec = embed_texts(self.embed_client, [cn], self.store)[cn]
        v = np.asarray([vec], dtype="float32")
        v /= (np.linalg.norm(v, axis=1, keepdims=True) + 1e-12)
        sims = (self._matrix @ v.T).reshape(-1)
        j = int(sims.argmax())
        s = float(sims[j])
        return (s >= self.threshold), self.names[j], round(s, 3)
