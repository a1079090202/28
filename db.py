"""数据库连接与建表。只负责连接和 schema，不含业务 SQL。"""
import sqlite3
from pathlib import Path

DEFAULT_DB_PATH = str(Path(__file__).resolve().parent / "store.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    phone TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS properties (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    community TEXT NOT NULL,          -- 小区
    layout TEXT NOT NULL,             -- 户型
    list_price REAL NOT NULL,         -- 挂牌价（万）
    list_date TEXT NOT NULL,          -- 挂牌日期 YYYY-MM-DD
    status TEXT NOT NULL DEFAULT '在售',
    created_by TEXT NOT NULL,         -- 操作人
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS customers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    phone TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT '新客',
    agent_id INTEGER NOT NULL REFERENCES agents(id),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS viewings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    property_id INTEGER NOT NULL REFERENCES properties(id),
    agent_id INTEGER NOT NULL REFERENCES agents(id),
    viewing_time TEXT NOT NULL,       -- 带看时间，本地时间
    feedback TEXT,                    -- 客户反馈，24 小时内补录
    feedback_at TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS deals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    property_id INTEGER NOT NULL REFERENCES properties(id),
    agent_id INTEGER NOT NULL REFERENCES agents(id),
    deal_price REAL NOT NULL,         -- 成交价（万）
    deal_date TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS status_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    from_status TEXT,
    to_status TEXT NOT NULL,
    operator TEXT NOT NULL,           -- 操作人
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_viewings_time ON viewings(viewing_time);
CREATE INDEX IF NOT EXISTS idx_viewings_property ON viewings(property_id);
CREATE INDEX IF NOT EXISTS idx_history_customer ON status_history(customer_id);
CREATE INDEX IF NOT EXISTS idx_history_to ON status_history(to_status, created_at);
"""


def connect(db_path=DEFAULT_DB_PATH):
    # check_same_thread=False：Streamlit 多线程复用同一个连接
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn):
    conn.executescript(SCHEMA)
    conn.commit()
