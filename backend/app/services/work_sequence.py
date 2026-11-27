"""工序编排领域服务：编排修缮工序顺序，发布工序时检查前置工序完成状态、安全交底和工匠到岗。

完工联动（完成工序）在基类的单事务内执行：
- 关联施工排期写入与本次发布的工序状态对应的里程碑；
- 关联施工日志归档；
- 完工结果登记到排期交接清单（schedule_handover），同一工序卡只交接一次，
  重发相同完成动作不会生成第二次交接。
同名但不同施工区域的工序卡按卡片 id 独立联动，互不串单。
"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class WorkSequenceService(BaseModuleService):
    MODULE = "work_sequence"
    ENTITY = "工序卡"
    OPERATION_TERM = "发布工序"
    LIST_FIELDS = ["工序名称", "前置工序", "所需工匠", "施工区域", "预计工时", "编排人", "工序状态", "备注说明"]
    REQUIRED_FIELDS = ["工序名称", "前置工序", "所需工匠"]
    STATUS_ORDER = ["待排期", "已发布", "施工中", "已完成"]
    ACTION_RULES = {"发布工序": "已发布", "开始施工": "施工中", "完成工序": "已完成"}
    NEGATIVE_ACTIONS = []

    def cross_module_verify(
        self, entry_id: int, action: str, entry: dict[str, Any]
    ) -> tuple[bool, str]:
        if action == "发布工序":
            # 1. 检查前置工序是否已完成
            prereq = entry.get("前置工序", "")
            if prereq and prereq != "无":
                matched = False
                for row in store.rows("work_sequence"):
                    if row.get("工序名称", "") == prereq and row.get("id") != entry_id:
                        matched = True
                        if row.get("status") != "已完成":
                            return False, f"前置工序「{prereq}」尚未完成，当前状态「{row.get('status', '')}」"
                        break
                if not matched and prereq != "无":
                    return False, f"前置工序「{prereq}」在系统中不存在"
            # 2. 检查关联安全交底是否已签认
            process_name = entry.get("工序名称", "")
            safety_records = store.query_by_field("safety_briefing", "交底工序", process_name)
            if not safety_records:
                return False, f"工序「{process_name}」缺少安全交底记录"
            unsigned = [r for r in safety_records if r.get("status") != "已签认"]
            if unsigned:
                return False, f"安全交底未完全签认，尚有{len(unsigned)}条待签认"
        if action == "开始施工":
            process_name = entry.get("工序名称", "")
            # 检查工匠是否已到岗
            craftsmen = store.query_by_field("craftsman_assign", "分配工序", process_name)
            if craftsmen and any(c.get("status") != "已到岗" for c in craftsmen):
                return False, "该工序的工匠尚未全部到岗"
            # 检查关联排期是否已发布（同名多卡时按卡片 id 精确匹配）
            schedules = self._linked_schedules(entry)
            if schedules:
                unreleased = [s for s in schedules if s.get("status") == "编制中"]
                if unreleased:
                    return False, "关联施工排期还未发布"
        return True, ""

    @staticmethod
    def _linked_schedules(entry: dict[str, Any]) -> list[dict[str, Any]]:
        """返回工序卡关联的排期：优先按卡片 id，回退按工序名称（兼容历史数据）。"""
        work_id = int(entry.get("id", 0))
        process_name = entry.get("工序名称", "")
        schedules = store.rows("construction_schedule")
        linked = [
            s for s in schedules
            if int(s.get("关联工序ID", 0) or 0) == work_id
        ]
        if linked:
            return linked
        return [s for s in schedules if s.get("计划工序", "") == process_name]

    def cross_module_effect(
        self, entry_id: int, action: str, entry: dict[str, Any]
    ) -> list[dict]:
        effects = []
        if action == "完成工序":
            process_name = entry.get("工序名称", "")
            # 本次完工对应的里程碑与日志归档，与这次发布的工序状态（已完成）对应。
            linked_schedules = self._linked_schedules(entry)
            for sched in linked_schedules:
                if sched.get("status") == "执行中":
                    sched["里程碑节点"] = f"{process_name}已完成"
                    effects.append(dict(sched))
            # 联动归档修缮日志中关联日志（同名多卡按名称归档同工序日志）
            for log in store.rows("restoration_log"):
                if log.get("对应工序", "") == process_name:
                    if log.get("status") in ("已审核", "已填写"):
                        log["status"] = "已归档"
                        log["pending"] = False
                        effects.append(dict(log))
            # 完工结果同步到排期交接清单：同一工序卡仅一条，重发不重复生成。
            action_log_id = self._latest_action_log_id(entry_id, "完成工序")
            target_schedule = linked_schedules[0] if linked_schedules else None
            handover = store.record_handover(
                entry,
                target_schedule,
                f"{process_name}已完成",
                action_log_id,
            )
            effects.append({"交接清单": handover})
        return effects

    @staticmethod
    def _latest_action_log_id(entry_id: int, action: str) -> int | None:
        row = store._conn.execute(  # noqa: SLF001 - 同库读取动作流水主键
            "SELECT id FROM action_log WHERE module='work_sequence' AND entry_id=?"
            " AND action=? ORDER BY id DESC LIMIT 1",
            (entry_id, action),
        ).fetchone()
        return int(row["id"]) if row else None

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        # 返回工序依赖图
        all_rows = store.rows(self.MODULE)
        base["dependency_graph"] = [
            {"工序": r.get("工序名称"), "前置": r.get("前置工序"), "状态": r.get("status")}
            for r in all_rows
        ]
        base["completed"] = [r.get("工序名称") for r in all_rows if r.get("status") == "已完成"]
        return base
