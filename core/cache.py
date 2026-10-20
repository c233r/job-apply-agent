# -*- coding: utf-8 -*-
"""SQLite 缓存：公司补全信息 + 表单字段人工回复记录（幂等、断点续跑）。"""
from __future__ import annotations

import os
import sqlite3
import threading

_lock = threading.Lock()
_conns: dict[str, sqlite3.Connection] = {}


def _connect(path: str) -> sqlite3.Connection:
    if path not in _conns:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        conn = sqlite3.connect(path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        _init_schema(conn)
        _conns[path] = conn
    return _conns[path]


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS company_info (
            name TEXT PRIMARY KEY,
            listed INTEGER,            -- 1 上市 / 0 未上市 / -1 未知
            market_cap_yi REAL,        -- 总市值(亿)
            float_market_cap_yi REAL,  -- 流通市值(亿)
            board TEXT,                -- 板块/市场（沪A/深A/港股/美股/未上市）
            stock_code TEXT,
            security_name TEXT,        -- 匹配到的证券名称
            china500 INTEGER,          -- 1 是 / 0 否 / -1 未知
            china500_evidence TEXT,
            updated_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS applied (
            key TEXT PRIMARY KEY,      -- 公司+岗位 唯一键
            status TEXT,               -- filled / confirmed / submitted / skipped
            detail TEXT,
            updated_at TEXT
        )
    """)
    # 已查找（抓过岗位）的公司：打标签，下次默认跳过
    conn.execute("""
        CREATE TABLE IF NOT EXISTS scraped (
            name TEXT PRIMARY KEY,     -- 公司名
            url TEXT,
            status TEXT,               -- done/failed/skipped/dead/error
            reason TEXT,
            jobs_count INTEGER,
            updated_at TEXT
        )
    """)
    conn.commit()


class KV:
    """company_info 缓存读写。"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self.conn = _connect(db_path)

    def get_company(self, name: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM company_info WHERE name=?", (name,)).fetchone()
        return dict(row) if row else None

    def set_company(self, name: str, **fields) -> None:
        fields.setdefault("name", name)
        cols = list(fields.keys())
        ph = ",".join(["?"] * len(cols))
        upd = ",".join(["%s=excluded.%s" % (c, c) for c in cols if c != "name"])
        sql = ("INSERT INTO company_info (%s) VALUES (%s) "
               "ON CONFLICT(name) DO UPDATE SET %s" % (",".join(cols), ph, upd))
        with _lock:
            self.conn.execute(sql, [fields[c] for c in cols])
            self.conn.commit()

    # ---- 已查找公司记录（打标签） ----
    def get_scraped(self, name: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM scraped WHERE name=?", (name,)).fetchone()
        return dict(row) if row else None

    def set_scraped(self, name: str, url: str = "", status: str = "done",
                    reason: str = "", jobs_count: int = 0) -> None:
        with _lock:
            self.conn.execute(
                "INSERT INTO scraped(name,url,status,reason,jobs_count,updated_at) "
                "VALUES(?,?,?,?,?,datetime('now','localtime')) "
                "ON CONFLICT(name) DO UPDATE SET url=excluded.url, status=excluded.status, "
                "reason=excluded.reason, jobs_count=excluded.jobs_count, updated_at=excluded.updated_at",
                (name, url, status, reason, jobs_count))
            self.conn.commit()

    def all_scraped(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM scraped").fetchall()
        return [dict(r) for r in rows]

    # ---- 投递状态 ----
    def set_applied(self, key: str, status: str, detail: str = "") -> None:
        with _lock:
            self.conn.execute(
                "INSERT INTO applied(key,status,detail,updated_at) VALUES(?,?,?,datetime('now','localtime')) "
                "ON CONFLICT(key) DO UPDATE SET status=excluded.status, detail=excluded.detail, updated_at=excluded.updated_at",
                (key, status, detail))
            self.conn.commit()
