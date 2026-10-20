# -*- coding: utf-8 -*-
"""Embedding 客户端（双后端）：

1. 本地小模型（CPU）：`backend: local`，默认模型 `BAAI/bge-small-zh-v1.5`（24M 参数、512 维），
   通过 fastembed（ONNX，轻量，推荐）或 sentence-transformers 加载，进程内只加载一次。
2. OpenAI 兼容 API：`backend: api`，POST {base_url}/embeddings。

DeepSeek 本身不提供 embedding 接口，因此本地模型是最省事的选择。
"""
from __future__ import annotations

import requests

# 多进程不可用时置为 True，避免每次重复尝试
_MP_FAILED = False


class EmbeddingError(Exception):
    pass


# ---------------- OpenAI 兼容 API ----------------
class EmbeddingClient:
    def __init__(self, base_url: str = "", api_key: str = "", model: str = "",
                 timeout: int = 60, batch_size: int = 64):
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.model = model or ""
        self.timeout = timeout
        self.batch_size = batch_size

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_key and self.model)

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not self.configured:
            raise EmbeddingError("未配置 embedding API（base_url/api_key/model）")
        if not texts:
            return []
        url = self.base_url + "/embeddings"
        headers = {"Authorization": "Bearer " + self.api_key, "Content-Type": "application/json"}
        out: list[list[float]] = []
        for i in range(0, len(texts), self.batch_size):
            batch = texts[i:i + self.batch_size]
            resp = requests.post(url, headers=headers,
                                 json={"model": self.model, "input": batch}, timeout=self.timeout)
            if resp.status_code != 200:
                raise EmbeddingError("embedding 失败 %s: %s" % (resp.status_code, resp.text[:200]))
            data = resp.json().get("data", [])
            data.sort(key=lambda d: d.get("index", 0))
            out.extend([d["embedding"] for d in data])
        return out


# ---------------- 本地小模型（CPU） ----------------
class LocalEmbeddingClient:
    """本地 embedding 模型，CPU 运行；进程内只加载一次（类级单例缓存）。"""

    _instances: dict = {}

    def __init__(self, model: str = "BAAI/bge-small-zh-v1.5", device: str = "cpu",
                 batch_size: int = 64, model_path: str = "", workers: int = 0,
                 threads: int = 0):
        self.model = model
        self.device = device
        self.batch_size = batch_size
        self.model_path = model_path or ""
        self.workers = workers
        self.threads = threads
        self._impl = None
        self._kind = None

    @property
    def configured(self) -> bool:
        return bool(self.model)

    def _load(self):
        key = (self.model, self.device, self.model_path)
        if key in LocalEmbeddingClient._instances:
            self._impl, self._kind = LocalEmbeddingClient._instances[key]
            return self
        print("[embedding] 正在加载本地模型 %s（首次下载/加载可能需几十秒）..." % self.model, flush=True)
        # 1) fastembed（ONNX 运行时，轻量，推荐）
        if self._impl is None:
            try:
                from fastembed import TextEmbedding
                kw = {"model_name": self.model}
                if self.model_path:
                    kw["specific_model_path"] = self.model_path
                if self.threads:
                    kw["threads"] = self.threads
                self._impl = TextEmbedding(**kw)
                self._kind = "fastembed"
            except ImportError:
                pass
        # 2) sentence-transformers（依赖 torch，较重）
        if self._impl is None:
            try:
                from sentence_transformers import SentenceTransformer
                self._impl = SentenceTransformer(self.model, device=self.device)
                self._kind = "sentence_transformers"
            except ImportError as e:
                raise EmbeddingError(
                    "本地 embedding 需要 fastembed 或 sentence-transformers，"
                    "请先 pip install fastembed（或 sentence-transformers），见 requirements.txt") from e
        LocalEmbeddingClient._instances[key] = (self._impl, self._kind)
        print("[embedding] 模型加载完成（%s）" % self._kind, flush=True)
        return self

    def warmup(self) -> str:
        """启动时预加载模型（可选）。返回使用的后端实现名。"""
        self._load()
        return self._kind

    def embed(self, texts: list[str]) -> list[list[float]]:
        global _MP_FAILED
        if not texts:
            return []
        # 先确认模型可加载（fastembed 缺失会在此抛错，由上层回退到字符相似度），
        # 只有在确认可用后才尝试多进程，避免在受限环境里无谓地 spawn。
        self._load()
        # 多进程：文本足够多时才值得（每 worker 至少约 64 条，摊薄模型加载开销）
        if (self.workers > 1 and len(texts) >= self.workers * 64
                and not _MP_FAILED and self._kind == "fastembed"):
            try:
                from llm.parallel_embed import parallel_embed
                print("[embedding] 多进程并行：%d 个 worker 处理 %d 条..." % (
                    self.workers, len(texts)), flush=True)
                res = parallel_embed(texts, self.model, self.model_path,
                                     workers=self.workers, batch_size=self.batch_size, threads=1)
                if res:
                    return res
            except Exception as e:
                _MP_FAILED = True
                print("[embedding] 多进程不可用，回退单进程：%s" % e, flush=True)
        if self._kind == "fastembed":
            vecs = list(self._impl.embed(texts, batch_size=self.batch_size))
            return [[float(x) for x in v] for v in vecs]
        arr = self._impl.encode(texts, normalize_embeddings=True,
                                batch_size=self.batch_size, show_progress_bar=False)
        return [[float(x) for x in v] for v in arr]


def build_embedding_client(cfg):
    """按配置返回本地或 API 客户端。"""
    emb = cfg.get("embedding", {}) if cfg is not None else {}
    backend = (emb.get("backend") or "").strip().lower()
    if backend == "local" or (not backend and emb.get("model") and not emb.get("api_key")):
        return LocalEmbeddingClient(
            model=emb.get("model") or "BAAI/bge-small-zh-v1.5",
            device=emb.get("device") or "cpu",
            batch_size=int(emb.get("batch_size", 64)),
            model_path=emb.get("model_path", ""),
            workers=int(emb.get("workers", 0) or 0),
            threads=int(emb.get("threads", 0) or 0),
        )
    return EmbeddingClient(
        base_url=emb.get("base_url", ""),
        api_key=emb.get("api_key", ""),
        model=emb.get("model", ""),
        batch_size=int(emb.get("batch_size", 64)),
    )
