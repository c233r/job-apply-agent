# -*- coding: utf-8 -*-
"""异步工具：在同步代码里运行协程。"""
from __future__ import annotations

import asyncio


def run_async(coro):
    """在同步代码里运行协程；若已在事件循环中则放到独立线程跑。"""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(asyncio.run, coro).result()
