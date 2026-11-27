"""持久化恢复与排期交接查询接口。

这些固定路径必须在通用资源路由 ``/{module}`` 之前注册，否则会被动态路径抢先匹配。
- GET /api/schedule-handovers：排期交接清单，每张工序卡完工后最多一条；
- GET /api/action-status/{module}/{entry_id}/{action}：工匠凭原请求核对当时
  实际生效的动作状态（重启恢复后用于确认终态，避免重复发起）。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.services import service_for
from app.store import store

router = APIRouter(prefix="/api", tags=["持久化与交接"])


@router.get("/schedule-handovers")
def list_schedule_handovers() -> dict[str, object]:
    """返回排期交接清单及当前条目快照，供监督人员核对完工结果。"""
    handovers = store.list_handovers()
    for handover in handovers:
        work_row = store.find("work_sequence", int(handover["work_sequence_id"]))
        handover["work_sequence"] = work_row
        schedule_id = int(handover.get("schedule_id", 0) or 0)
        handover["schedule"] = (
            store.find("construction_schedule", schedule_id) if schedule_id else None
        )
    return {"items": handovers, "total": len(handovers)}


@router.get("/action-status/{module}/{entry_id}/{action}")
def get_action_status(module: str, entry_id: int, action: str) -> dict[str, object]:
    """核对某条动作记录的持久化状态：processing/done/failed 与生效条目快照。"""
    service = service_for(module)
    if service is None:
        raise HTTPException(status_code=404, detail=f"未知业务模块：{module}")
    record = store.action_status(module, entry_id, action)
    current = store.find(module, entry_id)
    if record is None and current is None:
        raise HTTPException(status_code=404, detail=f"条目 {entry_id} 在 {module} 中不存在")
    return {
        "module": module,
        "entry_id": entry_id,
        "action": action,
        "record": record,
        "current": current,
    }
