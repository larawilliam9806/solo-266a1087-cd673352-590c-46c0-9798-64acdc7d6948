"""SQLite 持久层：连接管理、WAL 配置与建表脚本。"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from collections.abc import Iterator
from pathlib import Path

DEFAULT_DB_PATH = os.environ.get("ARCH_DB_PATH", "/data/archaeology.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS groups (
    group_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS strata (
    stratum_no  TEXT PRIMARY KEY,
    group_id    INTEGER,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS records (
    record_no    TEXT PRIMARY KEY,
    before_no    TEXT NOT NULL,
    after_no     TEXT NOT NULL,
    active       INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    retracted_at TEXT,
    FOREIGN KEY (before_no) REFERENCES strata (stratum_no),
    FOREIGN KEY (after_no)  REFERENCES strata (stratum_no)
);

-- 已生效的关系图（传递归约后的边），一条边属于一个层位组
CREATE TABLE IF NOT EXISTS graph_edges (
    group_id   INTEGER NOT NULL,
    before_no  TEXT NOT NULL,
    after_no   TEXT NOT NULL,
    PRIMARY KEY (before_no, after_no),
    FOREIGN KEY (group_id) REFERENCES groups (group_id) ON DELETE CASCADE
);

-- 每条归约边的全部支持记录号（一份记录至多支撑一条边）
CREATE TABLE IF NOT EXISTS edge_support (
    before_no  TEXT NOT NULL,
    after_no   TEXT NOT NULL,
    record_no  TEXT NOT NULL PRIMARY KEY,
    FOREIGN KEY (before_no, after_no)
        REFERENCES graph_edges (before_no, after_no) ON DELETE CASCADE,
    FOREIGN KEY (record_no) REFERENCES records (record_no)
);

CREATE INDEX IF NOT EXISTS idx_records_active ON records (active);
CREATE INDEX IF NOT EXISTS idx_strata_group ON strata (group_id);
"""


def get_db_path() -> str:
    return os.environ.get("ARCH_DB_PATH", DEFAULT_DB_PATH)


def connect(db_path: str | None = None) -> sqlite3.Connection:
    """打开一个手动事务（isolation_level=None）的连接。"""
    path = db_path or get_db_path()
    # 仅对文件型库创建父目录；":memory:" 等特殊路径跳过
    if path != ":memory:":
        Path(path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    # WAL 让写事务进行时，其他连接仍可读取已生效的图
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.isolation_level = None
    return conn


def init_db(db_path: str | None = None) -> None:
    """建库建表；幂等，可在应用启动时重复执行。"""
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


@contextmanager
def db_session(db_path: str | None = None) -> Iterator[sqlite3.Connection]:
    conn = connect(db_path)
    try:
        yield conn
    finally:
        conn.close()
