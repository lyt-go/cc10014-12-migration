"""SQLite 持久化与升级恢复测试。

覆盖需求：
1. 工序动作记录与排期里程碑持久化，重启后接着处理已登记工序；
2. 示例数据可初始化，重复初始化不覆盖用户补录；
3. 完工联动里程碑/日志归档/交接清单与发布状态对应，重发不产生第二次交接；
4. 终态回执迟到 + 后台重启：凭原请求核对实际生效状态，不出现状态分裂；
5. 同名不同施工区域两张工序卡升级后是两条独立记录；
6. 迁移失败重试：已登记排期仍可打开，不退回空白示例。
"""
from __future__ import annotations

import pytest

from app.store import Store, store
from app.services.work_sequence import WorkSequenceService
from app.services.construction_schedule import ConstructionScheduleService
from app.services.safety_briefing import SafetyBriefingService
from app.services.restoration_log import RestorationLogService


work_service = WorkSequenceService()
schedule_service = ConstructionScheduleService()
safety_service = SafetyBriefingService()
log_service = RestorationLogService()


@pytest.fixture(autouse=True)
def reset_store():
    store.reset_to_seed()
    yield


def _publish_ready_process(process: str, area: str) -> dict:
    """登记工序并补齐发布所需的安全交底，返回已发布的工序卡。"""
    safety, missing = safety_service.create_entry({
        "交底工序": process,
        "危险源": "测试风险",
        "防护措施": "测试防护",
    })
    assert missing == []
    safety_service.run_action(safety["id"], "执行交底")
    safety_service.run_action(safety["id"], "签认确认")

    ws, missing = work_service.create_entry({
        "工序名称": process,
        "前置工序": "无",
        "所需工匠": "测试工匠",
        "施工区域": area,
        "预计工时": "10小时",
    })
    assert missing == []
    published, msg = work_service.run_action(ws["id"], "发布工序")
    assert published is not None, msg
    return published


# ═══════════════════════════════════════════════════════════════════
# 1. 持久化与重启：登记、发布、开始施工在重启后仍然有效
# ═══════════════════════════════════════════════════════════════════

def test_registered_work_sequence_survives_restart():
    ws = _publish_ready_process("梁架修补", "正殿")
    started, msg = work_service.run_action(ws["id"], "开始施工")
    assert started is not None, msg
    assert started["status"] == "施工中"

    # 监督人员重启后台：内存全部丢弃，仅靠 SQLite 恢复
    store.restart()

    recovered = store.find("work_sequence", ws["id"])
    assert recovered is not None
    assert recovered["工序名称"] == "梁架修补"
    assert recovered["施工区域"] == "正殿"
    assert recovered["status"] == "施工中"

    # 重启后可接着处理：完成工序应正常执行
    finished, msg = work_service.run_action(ws["id"], "完成工序")
    assert finished is not None, msg
    assert finished["status"] == "已完成"


def test_seed_initialized_once_and_never_overwrites_user_data(tmp_path):
    db_file = tmp_path / "fresh.sqlite3"
    fresh = Store(db_file)
    seed_count = len(fresh.rows("work_sequence"))
    assert seed_count == 3  # 示例数据完成初始化

    # 用户补录一张工序卡（直接走 Store 事务，模拟登记接口落盘）
    with fresh.transaction():
        rows = fresh.rows("work_sequence")
        user_id = max(int(r["id"]) for r in rows) + 1
        rows.append({
            "id": user_id,
            "工序名称": "用户补录工序",
            "前置工序": "无",
            "所需工匠": "工匠甲",
            "施工区域": "后山门",
            "status": "待排期",
            "pending": True,
            "abnormal": False,
        })

    # 模拟再次启动（重复初始化）：示例数据不重写，用户补录仍在
    again = Store(db_file)
    assert len(again.rows("work_sequence")) == seed_count + 1
    assert again.find("work_sequence", user_id)["工序名称"] == "用户补录工序"
    # 示例行内容保持首版，不被二次初始化覆盖
    assert again.find("work_sequence", 1)["工序名称"] == "工序编排样例1"


# ═══════════════════════════════════════════════════════════════════
# 2. 完工联动：里程碑、日志归档、交接清单与发布状态对应且幂等
# ═══════════════════════════════════════════════════════════════════

def test_completion_creates_milestone_archive_and_single_handover():
    ws = _publish_ready_process("油漆涂装", "门窗")
    # 排期按卡片 id 精确关联，发布并启动执行
    sched, missing = schedule_service.create_entry({
        "计划工序": "油漆涂装",
        "开始日期": "2027-04-01",
        "结束日期": "2027-04-15",
        "关联工序ID": ws["id"],
    })
    assert missing == []
    schedule_service.run_action(sched["id"], "发布排期")
    work_service.run_action(ws["id"], "开始施工")
    schedule_service.run_action(sched["id"], "启动执行")

    # 一条已审核的施工日志
    log, missing = log_service.create_entry({
        "施工日期": "2027-04-02",
        "对应工序": "油漆涂装",
        "施工内容": "刷底漆两遍",
    })
    assert missing == []
    log_service.run_action(log["id"], "提交日志")
    log_service.run_action(log["id"], "审核通过")

    finished, msg = work_service.run_action(ws["id"], "完成工序")
    assert finished is not None, msg
    assert finished["status"] == "已完成"

    # 里程碑与这次发布的完工状态对应
    sched_row = store.find("construction_schedule", sched["id"])
    assert sched_row["里程碑节点"] == "油漆涂装已完成"
    # 施工日志已归档
    assert store.find("restoration_log", log["id"])["status"] == "已归档"

    # 交接清单恰好一条，完工结果同步到排期
    handovers = store.list_handovers()
    assert len(handovers) == 1
    handover = handovers[0]
    assert handover["work_sequence_id"] == ws["id"]
    assert handover["schedule_id"] == sched["id"]
    assert handover["process_name"] == "油漆涂装"
    assert handover["work_status"] == "已完成"

    # 重发相同完成动作：幂等拒绝，不生成第二次交接
    again, msg = work_service.run_action(ws["id"], "完成工序")
    assert again is not None
    assert "已执行过" in msg
    assert len(store.list_handovers()) == 1

    # 重启后交接清单仍然只有一条
    store.restart()
    assert len(store.list_handovers()) == 1
    assert store.find("work_sequence", ws["id"])["status"] == "已完成"


# ═══════════════════════════════════════════════════════════════════
# 3. 升级期崩溃恢复：交接已落库但终态回执迟到，重启后状态不分裂
# ═══════════════════════════════════════════════════════════════════

def test_restart_reconciles_processing_action_that_reached_final_state():
    ws = _publish_ready_process("瓦面更换", "主殿屋面")
    sched, _ = schedule_service.create_entry({
        "计划工序": "瓦面更换",
        "开始日期": "2027-05-01",
        "结束日期": "2027-05-10",
        "关联工序ID": ws["id"],
    })
    schedule_service.run_action(sched["id"], "发布排期")
    work_service.run_action(ws["id"], "开始施工")
    schedule_service.run_action(sched["id"], "启动执行")
    work_service.run_action(ws["id"], "完成工序")

    # 模拟升级窗口：交接与完工状态已落库，但动作流水停在 processing（回执迟到）
    row = store._conn.execute(  # noqa: SLF001
        "SELECT id FROM action_log WHERE module='work_sequence' AND entry_id=?"
        " AND action='完成工序' ORDER BY id DESC LIMIT 1",
        (ws["id"],),
    ).fetchone()
    with store._conn:  # noqa: SLF001
        store._conn.execute(  # noqa: SLF001
            "UPDATE action_log SET status='processing' WHERE id=?", (row["id"],)
        )
    assert store.action_status("work_sequence", ws["id"], "完成工序")["status"] == "processing"

    # 后台在此时重启
    store.restart()

    # 恢复后：动作按实际落库的完工状态核对为 done，而不是回退成待发布
    record = store.action_status("work_sequence", ws["id"], "完成工序")
    assert record["status"] == "done"
    current = store.find("work_sequence", ws["id"])
    assert current["status"] == "已完成"
    # 交接不重复生成
    assert len(store.list_handovers()) == 1

    # 工匠凭原请求核对：接口返回的当前状态与流水一致，可继续后续施工安排
    status, msg = work_service.run_action(ws["id"], "完成工序")
    assert status["status"] == "已完成"
    assert "已执行过" in msg
    assert len(store.list_handovers()) == 1


def test_restart_marks_unfinished_processing_action_failed_and_retry_succeeds():
    ws = _publish_ready_process("砖墙剔补", "北山墙")
    work_service.run_action(ws["id"], "开始施工")

    # 插入一条 processing 流水，模拟开始施工后的完工动作在落库前崩溃
    key, _ = store.begin_action("work_sequence", ws["id"], "完成工序")
    assert key == store.idempotent_key("work_sequence", ws["id"], "完成工序")
    # 条目仍是施工中，未达到完工目标状态
    assert store.find("work_sequence", ws["id"])["status"] == "施工中"

    store.restart()

    record = store.action_status("work_sequence", ws["id"], "完成工序")
    assert record["status"] == "failed"
    assert "回执迟到" in record["error"]
    # 没有产生交接
    assert store.list_handovers() == []
    # 条目停在实际生效的施工中，而不是错误地显示已完成或待发布
    assert store.find("work_sequence", ws["id"])["status"] == "施工中"

    # 工匠凭原请求重新发起：动作可正常完成，且只生成一条交接
    sched, _ = schedule_service.create_entry({
        "计划工序": "砖墙剔补",
        "开始日期": "2027-06-01",
        "结束日期": "2027-06-05",
        "关联工序ID": ws["id"],
    })
    schedule_service.run_action(sched["id"], "发布排期")
    schedule_service.run_action(sched["id"], "启动执行")
    finished, msg = work_service.run_action(ws["id"], "完成工序")
    assert finished is not None, msg
    assert finished["status"] == "已完成"
    assert len(store.list_handovers()) == 1


# ═══════════════════════════════════════════════════════════════════
# 4. 同名不同施工区域的两张工序卡：升级后两条独立记录、各自交接
# ═══════════════════════════════════════════════════════════════════

def test_same_name_different_area_cards_are_independent_after_upgrade():
    """同名不同施工区域的两张工序卡：走完整登记/发布/施工/完工后重启升级。"""
    cards = []
    for area in ("东配殿", "西配殿"):
        # 安全交底按工序名称签认，一次即可覆盖同名卡片
        if not cards:
            safety, _ = safety_service.create_entry({
                "交底工序": "斗拱修补",
                "危险源": "高处作业",
                "防护措施": "脚手架",
            })
            safety_service.run_action(safety["id"], "执行交底")
            safety_service.run_action(safety["id"], "签认确认")
        ws, missing = work_service.create_entry({
            "工序名称": "斗拱修补",
            "前置工序": "无",
            "所需工匠": "木作工匠",
            "施工区域": area,
            "预计工时": "20小时",
        })
        assert missing == []
        published, msg = work_service.run_action(ws["id"], "发布工序")
        assert published is not None, msg

        sched, _ = schedule_service.create_entry({
            "计划工序": "斗拱修补",
            "开始日期": "2027-07-01",
            "结束日期": "2027-07-10",
            "关联工序ID": ws["id"],
        })
        schedule_service.run_action(sched["id"], "发布排期")
        work_service.run_action(ws["id"], "开始施工")
        schedule_service.run_action(sched["id"], "启动执行")
        cards.append((ws, sched))

    # 两张卡分别完工
    for ws, _ in cards:
        done, msg = work_service.run_action(ws["id"], "完成工序")
        assert done is not None, msg
        assert done["status"] == "已完成"

    # 升级重启（内存丢弃，仅从 SQLite 恢复）：分别读出两条独立记录
    store.restart()
    east = store.find("work_sequence", cards[0][0]["id"])
    west = store.find("work_sequence", cards[1][0]["id"])
    assert east is not None and west is not None
    assert east["id"] != west["id"]
    assert east["工序名称"] == west["工序名称"] == "斗拱修补"
    assert east["施工区域"] == "东配殿"
    assert west["施工区域"] == "西配殿"

    # 各自的交接清单独立存在，里程碑各归各的排期
    handovers = store.list_handovers()
    assert {h["work_sequence_id"] for h in handovers} == {ws["id"] for ws, _ in cards}
    assert len(handovers) == 2
    for ws, sched in cards:
        assert store.find("construction_schedule", sched["id"])["里程碑节点"] == "斗拱修补已完成"
        matching = [h for h in handovers if h["work_sequence_id"] == ws["id"]]
        assert len(matching) == 1
        assert matching[0]["schedule_id"] == sched["id"]


# ═══════════════════════════════════════════════════════════════════
# 5. 迁移失败重试：已登记排期不丢失、不回退空白示例
# ═══════════════════════════════════════════════════════════════════

def test_migration_retry_keeps_registered_schedule(tmp_path):
    db_file = tmp_path / "migration.sqlite3"
    migrating = Store(db_file)

    # 迁移过程中已登记一条排期
    with migrating.transaction():
        rows = migrating.rows("construction_schedule")
        new_id = max(int(r["id"]) for r in rows) + 1
        rows.append({
            "id": new_id,
            "计划工序": "迁移期排期",
            "开始日期": "2027-08-01",
            "结束日期": "2027-08-20",
            "status": "编制中",
            "pending": True,
            "abnormal": False,
        })
    before_rows = len(migrating.rows("construction_schedule"))

    # 模拟首次迁移在写版本标记前中断：records 已有数据，meta 标记缺失
    with migrating._conn:  # noqa: SLF001
        migrating._conn.execute("DELETE FROM meta")  # noqa: SLF001

    # 迁移重试（重新打开同一数据库）：不能退回空白示例，已登记排期仍可打开
    retried = Store(db_file)
    assert len(retried.rows("construction_schedule")) == before_rows
    recovered_sched = retried.find("construction_schedule", new_id)
    assert recovered_sched is not None
    assert recovered_sched["计划工序"] == "迁移期排期"
    # 示例排期与用户排期同时保留，没有被重置成只剩 3 条示例
    assert before_rows > 3

    # 再重试一次结果稳定（幂等）
    third = Store(db_file)
    assert third.find("construction_schedule", new_id)["计划工序"] == "迁移期排期"
    assert len(third.rows("construction_schedule")) == before_rows


def test_migration_marker_backfilled_on_retry(tmp_path):
    """迁移中断导致 meta 标记丢失时，重试不重灌示例且自动补回标记。"""
    db_file = tmp_path / "marker.sqlite3"
    first = Store(db_file)
    with first._conn:  # noqa: SLF001
        first._conn.execute("DELETE FROM meta")  # noqa: SLF001
    work_total = len(first.rows("work_sequence"))

    retried = Store(db_file)
    marker = retried._conn.execute(  # noqa: SLF001
        "SELECT value FROM meta WHERE key='seed_version'"
    ).fetchone()
    assert marker is not None and marker[0] == "seed-v1"
    assert len(retried.rows("work_sequence")) == work_total
    # 再次启动走标记快速路径，数据依旧不变
    again = Store(db_file)
    assert len(again.rows("work_sequence")) == work_total


# ═══════════════════════════════════════════════════════════════════
# 6. 事务回滚：联动失败时不留下半截写入
# ═══════════════════════════════════════════════════════════════════

def test_transaction_rollback_restores_memory_and_database():
    ws_rows_before = len(store.rows("work_sequence"))
    with pytest.raises(RuntimeError):
        with store.transaction():
            work_service.create_entry({
                "工序名称": "应当回滚的工序",
                "前置工序": "无",
                "所需工匠": "工匠乙",
            })
            raise RuntimeError("模拟联动失败")
    assert len(store.rows("work_sequence")) == ws_rows_before

    # 数据库里同样查不到这条半成品
    store.restart()
    assert len(store.rows("work_sequence")) == ws_rows_before
    assert all(r["工序名称"] != "应当回滚的工序" for r in store.rows("work_sequence"))


def test_atomic_update_returns_none_without_partial_write():
    target = store.rows("work_sequence")[0]
    original_status = target["status"]
    result = store.atomic_update([
        {"module": "work_sequence", "entry_id": target["id"], "changes": {"status": "施工中"}},
        {"module": "work_sequence", "entry_id": 999999, "changes": {"status": "已完成"}},
    ])
    assert result is None
    assert store.find("work_sequence", target["id"])["status"] == original_status
