"""SQLite 持久层：连接管理与建表脚本。

进程重启后，工序动作记录、排期里程碑与交接清单都从这里恢复。
仅依赖标准库 sqlite3，不引入新的第三方依赖。
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

# 数据库文件位置：优先取环境变量，默认落在 backend/data/app.sqlite3。
# *.sqlite3 / *.db 已在 .gitignore 中，运行期数据不会进仓库。
DEFAULT_DB_DIR = Path(__file__).resolve().parent.parent / "data"


def database_path() -> Path:
    override = os.environ.get("APP_DB_PATH", "").strip()
    if override:
        return Path(override)
    return DEFAULT_DB_DIR / "app.sqlite3"


SCHEMA_SQL = """
-- 通用业务记录表：每个模块的条目以 JSON 整行保存，字段随领域服务演进，
-- 同时保留 id/status 便于检索与恢复。
CREATE TABLE IF NOT EXISTS records (
    module      TEXT NOT NULL,
    entry_id    INTEGER NOT NULL,
    data        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT '',
    updated_at  REAL NOT NULL,
    PRIMARY KEY (module, entry_id)
);
CREATE INDEX IF NOT EXISTS idx_records_module_status ON records(module, status);

-- 动作记录：登记/发布/开始施工/完成工序等每一次领域动作的流水。
-- request_key 与 (module, entry_id, action) 均建唯一约束，
-- 既支撑小时窗口幂等，也保证同一动作重放不会产生第二条交接。
CREATE TABLE IF NOT EXISTS action_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    request_key   TEXT NOT NULL UNIQUE,
    module        TEXT NOT NULL,
    entry_id      INTEGER NOT NULL,
    action        TEXT NOT NULL,
    status        TEXT NOT NULL,             -- processing / done / failed
    entry_snapshot TEXT NOT NULL DEFAULT '{}',
    error         TEXT NOT NULL DEFAULT '',
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL,
    UNIQUE(module, entry_id, action, request_key)
);
CREATE INDEX IF NOT EXISTS idx_action_target ON action_log(module, entry_id, action);

-- 排期交接清单：工序完工时生成，结果同步给关联排期。
-- 每张工序卡最多一条交接（work_sequence_id 唯一），重发完成动作不重复生成。
CREATE TABLE IF NOT EXISTS schedule_handover (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    work_sequence_id  INTEGER NOT NULL UNIQUE,
    schedule_id       INTEGER NOT NULL DEFAULT 0,
    process_name      TEXT NOT NULL DEFAULT '',
    work_status       TEXT NOT NULL DEFAULT '已完成',
    milestone         TEXT NOT NULL DEFAULT '',
    action_log_id     INTEGER,
    created_at        REAL NOT NULL
);

-- 迁移/初始化状态：示例数据只在首次初始化写入，重复初始化不覆盖用户补录。
CREATE TABLE IF NOT EXISTS meta (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);
"""


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    path = db_path or database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
    conn.commit()
