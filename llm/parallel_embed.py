# -*- coding: utf-8 -*-
"""自建多进程 embedding：按段拆分文本，多个 worker 各自加载模型并行处理。

不依赖 fastembed 的 `parallel`，只用标准库 multiprocessing：
- 主进程把 texts 切成 workers 段；
- 每个 worker 在初始化时加载一次模型（spawn 方式，Windows 可用）；
- 各 worker 处理自己那段，结果按顺序合并。

注意：multiprocessing 在部分受限环境（如某些沙箱）会被禁止，调用方应捕获异常并回退单进程。
"""
from __future__ import annotations

import multiprocessing as mp

_WORKER_MODEL = None


def _init_worker(model_name: str, device: str, model_path: str, threads: int | None):
    global _WORKER_MODEL
    from fastembed import TextEmbedding
    kw = {"model_name": model_name}
    if model_path:
        kw["specific_model_path"] = model_path
    if threads:
        kw["threads"] = threads
    _WORKER_MODEL = TextEmbedding(**kw)


def _embed_chunk(payload):
    texts, batch_size = payload
    global _WORKER_MODEL
    return [[float(x) for x in v] for v in _WORKER_MODEL.embed(texts, batch_size=batch_size)]


def parallel_embed(texts: list[str], model_name: str, model_path: str = "",
                   workers: int = 4, batch_size: int = 32,
                   threads: int | None = 1) -> list[list[float]]:
    """把 texts 分段交给 workers 个进程并行 embed，返回与原顺序一致的向量列表。"""
    if workers <= 1:
        raise ValueError("workers 必须 > 1")
    if not texts:
        return []
    n = len(texts)
    chunk_size = max(1, (n + workers - 1) // workers)
    chunks = [texts[i:i + chunk_size] for i in range(0, n, chunk_size)]
    ctx = mp.get_context("spawn")
    with ctx.Pool(len(chunks), initializer=_init_worker,
                  initargs=(model_name, "cpu", model_path, threads)) as pool:
        parts = pool.map(_embed_chunk, [(c, batch_size) for c in chunks], chunksize=1)
    out: list[list[float]] = []
    for part in parts:
        out.extend(part)
    return out
