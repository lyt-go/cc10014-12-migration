"""数据仓库：SQLite 持久化 + 内存工作副本，含事务、幂等追踪、动作流水与跨模块协调。

设计要点：
- 启动时从 SQLite 载入全部条目到内存（数据量为平台级小规模），服务层仍通过
  ``rows()`` 拿到 dict 工作副本直接改字段，兼容现有全部登记/动作接口。
- 所有写操作走 ``transaction()``：先在内存改，提交时做模块级 diff 并与动作流水、
  交接清单在同一个 SQLite 事务里落盘；任何一步失败整体回滚，内存副本同步还原。
- 示例数据仅在首次初始化写入（meta 标记 + 已有数据双保险），重复初始化以及
  迁移重试都不会覆盖用户补录的工序，也不会把已登记排期退回空白示例。
- 动作记录（action_log）与排期交接（schedule_handover）独立建表持久化：
  重启后监督人员可接着处理已登记工序，重发同一完成动作不会生成第二次交接；
  启动恢复会把崩溃时停在 processing 的动作按落库终态核对，杜绝一边已完成、
  一边仍待发布的分裂状态。
"""
from __future__ import annotations

import json
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from app import db as db_module
from app.seed import SEED_ROWS

SEED_VERSION = "seed-v1"


class Store:
    def __init__(self, db_path: str | Path | None = None) -> None:
        # 实例级可重入锁：不同 Store（不同数据库文件）互不干扰。
        self._lock = threading.RLock()
        self._db_path = Path(db_path) if db_path is not None else db_module.database_path()
        self._conn = db_module.connect(self._db_path)
        db_module.init_schema(self._conn)
        # 内存工作副本：{module: [row dict, ...]}
        self._tables: dict[str, list[dict[str, Any]]] = {}
        # 进程内资源锁（跨进程互斥由 SQLite 事务/写锁兜底）
        self._resource_locks: dict[str, int] = {}
        # 事务嵌套计数与模块快照
        self._txn_depth = 0
        self._txn_snapshots: dict[str, list[dict[str, Any]]] | None = None
        self._initialize()
        self._reload_tables()
        # 每次进程启动都核对崩溃时未确认的动作流水（升级窗口回执迟到场景）。
        self._recover_pending_actions()

    @property
    def db_path(self) -> Path:
        return self._db_path

    def reset_to_seed(self) -> None:
        """测试/运维辅助：清空数据库并重新写入示例数据（不影响正常启动的幂等初始化）。"""
        with self._lock:
            with self._conn:
                self._conn.executescript(
                    "DELETE FROM records; DELETE FROM action_log;"
                    " DELETE FROM schedule_handover; DELETE FROM meta;"
                )
            self._resource_locks.clear()
            self._initialize()
            self._reload_tables()

    # ──────────────────────────────────────────────────────────────────
    # 初始化与恢复
    # ──────────────────────────────────────────────────────────────────
    def _initialize(self) -> None:
        """首次启动写入示例数据；已存在用户数据或已初始化过则跳过。"""
        with self._lock:
            existing = self._conn.execute(
                "SELECT COUNT(*) AS c FROM records"
            ).fetchone()["c"]
            version_row = self._conn.execute(
                "SELECT value FROM meta WHERE key='seed_version'"
            ).fetchone()
            already_seeded = version_row is not None
            if already_seeded or existing > 0:
                # 重复初始化 / 迁移重试：保留用户补录，绝不回退到空白示例。
                # 若只是版本标记在中断中丢失，则补写标记，让后续重试走快速路径。
                if not already_seeded and existing > 0:
                    with self._conn:
                        self._conn.execute(
                            "INSERT OR IGNORE INTO meta(key, value) VALUES('seed_version', ?)",
                            (SEED_VERSION,),
                        )
                return
            try:
                now = time.time()
                with self._conn:  # 单事务写示例 + 版本标记，失败整体回滚
                    for module, rows in SEED_ROWS.items():
                        for row in rows:
                            self._persist_row(module, row, now, conn=self._conn)
                    self._conn.execute(
                        "INSERT INTO meta(key, value) VALUES('seed_version', ?)",
                        (SEED_VERSION,),
                    )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def restart(self) -> "Store":
        """模拟监督人员重启后台：关闭连接并基于同一数据库文件重新装载。"""
        with self._lock:
            self._conn.close()
            self._conn = db_module.connect(self._db_path)
            db_module.init_schema(self._conn)
            self._resource_locks.clear()
            self._txn_depth = 0
            self._txn_snapshots = None
            self._reload_tables()
            # 与全新进程启动保持一致：核对未确认的动作流水。
            self._recover_pending_actions()
            return self

    def _reload_tables(self) -> None:
        tables: dict[str, list[dict[str, Any]]] = {}
        result = self._conn.execute(
            "SELECT module, data FROM records ORDER BY module, entry_id"
        ).fetchall()
        for record in result:
            row = json.loads(record["data"])
            tables.setdefault(record["module"], []).append(row)
        self._tables = tables

    def _recover_pending_actions(self) -> None:
        """恢复崩溃时停在 processing 的动作，使动作记录与条目终态一致。

        规则：
        - 条目已达到动作目标状态：动作确认完成（done），并补齐缺失的交接；
        - 条目未达到目标状态：动作标记 failed，交由工匠凭原请求重新发起，
          此时原请求会按当前实际生效状态返回，而不会重复执行副作用。
        """
        pending = self._conn.execute(
            "SELECT * FROM action_log WHERE status='processing' ORDER BY id"
        ).fetchall()
        if not pending:
            return
        targets = {
            ("work_sequence", "发布工序"): "已发布",
            ("work_sequence", "开始施工"): "施工中",
            ("work_sequence", "完成工序"): "已完成",
        }
        with self._conn:
            for record in pending:
                row = self.find(record["module"], record["entry_id"])
                target = targets.get((record["module"], record["action"]))
                reached = bool(row and target and row.get("status") == target)
                if reached:
                    self._conn.execute(
                        "UPDATE action_log SET status='done', updated_at=? WHERE id=?",
                        (time.time(), record["id"]),
                    )
                    if record["module"] == "work_sequence" and record["action"] == "完成工序":
                        # 交接可能已随排期落库、也可能在崩溃前未及写入，按幂等补齐。
                        self._ensure_handover(row, record["id"], conn=self._conn)
                else:
                    self._conn.execute(
                        "UPDATE action_log SET status='failed', error=?, updated_at=? WHERE id=?",
                        ("后台重启恢复：终态回执迟到，动作未确认完成", time.time(), record["id"]),
                    )

    # ──────────────────────────────────────────────────────────────────
    # 事务边界
    # ──────────────────────────────────────────────────────────────────
    @contextmanager
    def transaction(self) -> Iterator[None]:
        """内存工作副本 + SQLite 单事务的提交/回滚边界（可重入）。"""
        with self._lock:
            if self._txn_depth == 0:
                self._txn_snapshots = {
                    module: [dict(row) for row in rows]
                    for module, rows in self._tables.items()
                }
                self._txn_depth = 1
                try:
                    yield
                    self._flush()
                    self._conn.commit()
                except Exception:
                    self._conn.rollback()
                    self._tables = self._txn_snapshots or {}
                    raise
                finally:
                    self._txn_depth = 0
                    self._txn_snapshots = None
            else:
                # 嵌套事务复用最外层边界
                self._txn_depth += 1
                try:
                    yield
                finally:
                    self._txn_depth -= 1

    def _flush(self) -> None:
        """把发生变化的模块整体与 SQLite 对齐（模块级 diff，规模小且可靠）。"""
        assert self._txn_snapshots is not None
        now = time.time()
        changed = set(self._tables) | set(self._txn_snapshots)
        for module in changed:
            before = self._txn_snapshots.get(module, [])
            after = self._tables.get(module, [])
            if [json.dumps(r, ensure_ascii=False, sort_keys=True) for r in before] == \
               [json.dumps(r, ensure_ascii=False, sort_keys=True) for r in after]:
                continue
            self._conn.execute("DELETE FROM records WHERE module=?", (module,))
            for row in after:
                self._persist_row(module, row, now, conn=self._conn)

    def _persist_row(self, module: str, row: dict[str, Any], now: float, *, conn) -> None:
        conn.execute(
            "INSERT OR REPLACE INTO records(module, entry_id, data, status, updated_at)"
            " VALUES(?, ?, ?, ?, ?)",
            (
                module,
                int(row.get("id", 0)),
                json.dumps(row, ensure_ascii=False),
                str(row.get("status", "")),
                now,
            ),
        )

    # ──────────────────────────────────────────────────────────────────
    # 基础访问（接口与历史内存版本保持一致）
    # ──────────────────────────────────────────────────────────────────
    def module_names(self) -> list[str]:
        with self._lock:
            return sorted(self._tables)

    def rows(self, module: str) -> list[dict[str, Any]]:
        with self._lock:
            return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        with self._lock:
            for row in self._tables.get(module, []):
                if int(row.get("id", 0)) == entry_id:
                    return row
            return None

    def overview(self) -> dict[str, object]:
        with self._lock:
            modules: list[dict[str, object]] = []
            for name in self.module_names():
                rows = self._tables.get(name, [])
                modules.append({
                    "name": name,
                    "created": len(rows),
                    "pending": sum(1 for row in rows if row.get("pending")),
                    "abnormal": sum(1 for row in rows if row.get("abnormal")),
                })
            cards = [
                {"label": "业务模块", "value": len(modules)},
                {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
                {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
                {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
            ]
            return {"cards": cards, "modules": modules}

    # ──────────────────────────────────────────────────────────────────
    # 动作流水与幂等性（持久化，重启后仍然生效）
    # ──────────────────────────────────────────────────────────────────
    def idempotent_key(self, module: str, entry_id: int, action: str) -> str:
        """生成幂等键：模块+记录+动作+小时窗口，避免同小时重复请求。"""
        hour_window = int(time.time() / 3600)
        return f"{module}:{entry_id}:{action}:{hour_window}"

    def check_idempotent(self, module: str, entry_id: int, action: str) -> bool:
        """该动作在当前小时窗口内是否已成功执行过。"""
        key = self.idempotent_key(module, entry_id, action)
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM action_log WHERE request_key=? AND status='done' LIMIT 1",
                (key,),
            ).fetchone()
            return row is not None

    def begin_action(
        self, module: str, entry_id: int, action: str
    ) -> tuple[str, bool]:
        """登记一条 processing 动作流水。

        返回 (request_key, reused)：同一小时窗口重复发起时复用既有流水，
        恢复后工匠凭原请求即可核对当时实际生效的状态。
        """
        key = self.idempotent_key(module, entry_id, action)
        now = time.time()
        with self._lock:
            existing = self._conn.execute(
                "SELECT id FROM action_log WHERE request_key=?", (key,)
            ).fetchone()
            if existing is None:
                # 独立提交：即使后续业务事务回滚或进程崩溃，processing 流水仍在，
                # 启动恢复可据此核对实际落库状态。INSERT OR IGNORE 兜底并发竞态。
                with self._conn:
                    self._conn.execute(
                        "INSERT OR IGNORE INTO action_log(request_key, module, entry_id,"
                        " action, status, created_at, updated_at)"
                        " VALUES(?, ?, ?, ?, 'processing', ?, ?)",
                        (key, module, entry_id, action, now, now),
                    )
                return key, existing is not None
            return key, True

    def mark_idempotent(self, module: str, entry_id: int, action: str) -> None:
        """动作成功后把流水置为 done，并保存条目快照。"""
        key = self.idempotent_key(module, entry_id, action)
        entry = self.find(module, entry_id)
        with self._lock:
            now = time.time()
            existing = self._conn.execute(
                "SELECT id FROM action_log WHERE request_key=?", (key,)
            ).fetchone()
            snapshot = json.dumps(entry or {}, ensure_ascii=False)
            if existing is None:
                self._conn.execute(
                    "INSERT INTO action_log(request_key, module, entry_id, action,"
                    " status, entry_snapshot, created_at, updated_at)"
                    " VALUES(?, ?, ?, ?, 'done', ?, ?, ?)",
                    (key, module, entry_id, action, snapshot, now, now),
                )
            else:
                self._conn.execute(
                    "UPDATE action_log SET status='done', entry_snapshot=?, updated_at=?"
                    " WHERE request_key=?",
                    (snapshot, now, key),
                )

    def fail_action(self, module: str, entry_id: int, action: str, reason: str) -> None:
        """动作执行失败时把流水置为 failed，允许稍后凭原请求重试。

        业务事务已回滚后调用，这里独立提交，保证失败原因可追溯。
        """
        key = self.idempotent_key(module, entry_id, action)
        with self._lock:
            with self._conn:
                self._conn.execute(
                    "UPDATE action_log SET status='failed', error=?, updated_at=?"
                    " WHERE request_key=?",
                    (reason, time.time(), key),
                )

    def action_status(self, module: str, entry_id: int, action: str) -> dict[str, Any] | None:
        """供工匠凭原请求核对当时实际生效的动作状态。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT request_key, status, entry_snapshot, error, created_at, updated_at"
                " FROM action_log WHERE module=? AND entry_id=? AND action=?"
                " ORDER BY id DESC LIMIT 1",
                (module, entry_id, action),
            ).fetchone()
            if row is None:
                return None
            return {
                "request_key": row["request_key"],
                "status": row["status"],
                "entry": json.loads(row["entry_snapshot"] or "{}"),
                "error": row["error"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }

    # ──────────────────────────────────────────────────────────────────
    # 排期交接清单（完工结果同步，幂等生成）
    # ──────────────────────────────────────────────────────────────────
    def record_handover(
        self,
        work_row: dict[str, Any],
        schedule: dict[str, Any] | None,
        milestone: str,
        action_log_id: int | None,
    ) -> dict[str, Any]:
        """工序完工时登记排期交接；同一工序卡只生成一条。"""
        with self._lock:
            return self._ensure_handover(
                work_row,
                action_log_id,
                conn=self._conn,
                schedule=schedule,
                milestone=milestone,
            )

    def _ensure_handover(
        self,
        work_row: dict[str, Any],
        action_log_id: int | None,
        *,
        conn,
        schedule: dict[str, Any] | None = None,
        milestone: str | None = None,
    ) -> dict[str, Any]:
        work_id = int(work_row.get("id", 0))
        process_name = str(work_row.get("工序名称", ""))
        existing = conn.execute(
            "SELECT * FROM schedule_handover WHERE work_sequence_id=?",
            (work_id,),
        ).fetchone()
        if existing is not None:
            return dict(existing)
        if schedule is None:
            schedule = self._find_schedule_for_work(work_row)
        if milestone is None:
            milestone = f"{process_name}已完成"
        now = time.time()
        cur = conn.execute(
            "INSERT INTO schedule_handover(work_sequence_id, schedule_id, process_name,"
            " work_status, milestone, action_log_id, created_at) VALUES(?, ?, ?, ?, ?, ?, ?)",
            (
                work_id,
                int(schedule.get("id", 0)) if schedule else 0,
                process_name,
                str(work_row.get("status", "已完成")),
                milestone,
                action_log_id,
                now,
            ),
        )
        return {
            "id": cur.lastrowid,
            "work_sequence_id": work_id,
            "schedule_id": int(schedule.get("id", 0)) if schedule else 0,
            "process_name": process_name,
            "work_status": work_row.get("status", "已完成"),
            "milestone": milestone,
            "action_log_id": action_log_id,
            "created_at": now,
        }

    def list_handovers(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM schedule_handover ORDER BY id"
            ).fetchall()
            return [dict(row) for row in rows]

    def _find_schedule_for_work(self, work_row: dict[str, Any]) -> dict[str, Any] | None:
        work_id = int(work_row.get("id", 0))
        process_name = work_row.get("工序名称", "")
        for sched in self._tables.get("construction_schedule", []):
            if int(sched.get("关联工序ID", 0) or 0) == work_id:
                return sched
        for sched in self._tables.get("construction_schedule", []):
            if sched.get("计划工序", "") == process_name:
                return sched
        return None

    # ──────────────────────────────────────────────────────────────────
    # 并发保护（进程内资源锁；跨进程由 SQLite 写事务兜底）
    # ──────────────────────────────────────────────────────────────────
    def acquire_resource(self, resource_key: str, wait_timeout: float = 5.0) -> bool:
        """获取资源锁，用于并发操作保护。"""
        deadline = time.time() + wait_timeout
        with self._lock:
            while self._resource_locks.get(resource_key) is not None:
                remaining = deadline - time.time()
                if remaining <= 0:
                    return False
                self._lock.release()
                time.sleep(0.05)
                self._lock.acquire()
            self._resource_locks[resource_key] = threading.get_ident()
            return True

    def release_resource(self, resource_key: str) -> None:
        """释放资源锁。"""
        with self._lock:
            self._resource_locks.pop(resource_key, None)

    # ──────────────────────────────────────────────────────────────────
    # 跨模块原子更新
    # ──────────────────────────────────────────────────────────────────
    def atomic_update(self, operations: list[dict]) -> list[dict] | None:
        """原子地执行一组操作，全部成功或全部回滚（SQLite 事务保证）。

        operations: [{"module": ..., "entry_id": ..., "changes": {...}}, ...]
        返回更新后的条目列表，失败返回 None。
        """
        if not operations:
            return []
        try:
            with self.transaction():
                updated = []
                for op in operations:
                    row = self.find(op["module"], op["entry_id"])
                    if row is None:
                        raise ValueError(f"条目 {op['module']}#{op['entry_id']} 不存在")
                    for key, value in op.get("changes", {}).items():
                        row[key] = value
                    updated.append(dict(row))
                return updated
        except Exception:
            return None

    # ──────────────────────────────────────────────────────────────────
    # 跨模块查询辅助
    # ──────────────────────────────────────────────────────────────────
    def query_by_field(self, module: str, field: str, value: Any) -> list[dict[str, Any]]:
        """跨模块查询：按字段值查找所有匹配行。"""
        with self._lock:
            return [
                dict(row)
                for row in self._tables.get(module, [])
                if str(row.get(field, "")) == str(value)
            ]

    def cross_module_snapshot(self, modules: list[str]) -> dict[str, Any]:
        """返回多个模块的状态快照，供前端跨模块面板使用。"""
        with self._lock:
            result: dict[str, Any] = {}
            for mod in modules:
                rows = self._tables.get(mod, [])
                status_counts: dict[str, int] = {}
                for row in rows:
                    s = row.get("status", "")
                    status_counts[s] = status_counts.get(s, 0) + 1
                result[mod] = {"total": len(rows), "by_status": status_counts}
            return result


store = Store()
