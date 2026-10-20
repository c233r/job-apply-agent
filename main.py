#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""入口：默认执行端到端编排（等价于 `python run_pipeline.py run`）。"""
import sys

from run_pipeline import main

if __name__ == "__main__":
    argv = sys.argv[1:] if len(sys.argv) > 1 else ["run"]
    raise SystemExit(main(argv))
