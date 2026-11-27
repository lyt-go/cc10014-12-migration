# 古建筑修缮管理平台

文保工程师、工匠和监理共同管理古建筑修缮工序，审核修缮方案，签收变更后按确认版施工。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/WorkbenchView.vue|tsx  全部模块共享工作台
│   ├── src/data/modules.ts              18个业务模块配置
│   ├── src/api/              统一请求封装
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/resource.py  通用查询/导出/登记/动作接口
│   ├── app/routers/persistence.py 交接清单与动作状态核对接口
│   ├── app/services/<key>.py    每个模块独立领域逻辑
│   ├── app/services/base.py     共享服务骨架（事务化动作执行）
│   ├── app/db.py             SQLite 连接与建表脚本
│   ├── app/store.py          数据仓库：SQLite 持久化 + 内存工作副本
│   └── backend/data/*.sqlite3   运行期数据库（已在 .gitignore，不入库）
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

#### SQLite 持久化与升级恢复

业务数据不再只存进程内存，而是落到 SQLite（默认 `backend/data/app.sqlite3`，
可用环境变量 `APP_DB_PATH` 指定位置；`*.sqlite3` 已被 `.gitignore` 忽略）。

- **表结构**：`records` 保存各模块条目（整行 JSON + id/status），`action_log`
  持久化每次登记/发布/开始施工/完成工序的动作流水（processing/done/failed），
  `schedule_handover` 是排期交接清单，`meta` 记录示例数据初始化版本。
- **幂等初始化**：示例数据只在首次启动写入；重复启动或迁移重试都不会覆盖
  用户补录的工序，也不会把已登记排期退回空白示例（meta 标记缺失但已有数据时
  会自动补回标记）。
- **事务化动作**：状态变更、跨模块联动（里程碑、日志归档、交接清单）在同一个
  SQLite 事务内提交，任一失败整体回滚。同一工序卡完工只生成一条交接，
  重发相同完成动作被动作流水幂等拦截，不会产生第二次交接。
- **崩溃恢复**：进程启动时核对停在 `processing` 的动作——条目已达目标状态则
  确认 `done` 并补齐交接，否则标记 `failed`。工匠可凭原请求经
  `GET /api/action-status/{module}/{id}/{action}` 核对当时实际生效状态，
  不会出现一边已完成、一边仍待发布的分裂。
- **同名多卡**：排期发布时按工序卡 id 建立精确关联（`关联工序ID`），
  同名但不同施工区域的两张工序卡是两条独立记录，里程碑与交接各归各单。
- 运维测试辅助：`Store.restart()` 模拟后台重启；`Store.reset_to_seed()`
  仅用于测试夹具，把数据库重置回示例数据。

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 建筑勘查 | `building_survey` | 勘查记录 | 建筑名称、勘查部位、病害类型 |
| 修缮方案 | `restoration_plan` | 修缮方案 | 方案编号、对应勘查、修缮工艺 |
| 材料核验 | `material_verify` | 材料核验单 | 材料名称、规格型号、供应商 |
| 工匠分配 | `craftsman_assign` | 工匠分配单 | 工匠姓名、技艺类别、资质等级 |
| 工序编排 | `work_sequence` | 工序卡 | 工序名称、前置工序、所需工匠 |
| 质量检查 | `quality_inspect` | 检查记录 | 检查工序、检查项目、技术标准 |
| 历史图纸 | `historical_drawing` | 历史图纸 | 图纸编号、绘制年代、绘制内容 |
| 构件登记 | `component_registry` | 构件档案 | 构件名称、所属建筑、材质类型 |
| 环境监测 | `env_monitor` | 监测数据 | 监测位置、温度读数、湿度读数 |
| 修缮日志 | `restoration_log` | 施工日志 | 施工日期、对应工序、施工内容 |
| 验收记录 | `acceptance_record` | 验收报告 | 验收工序、验收标准、验收结论 |
| 修缮支出 | `funding_use` | 支出记录 | 支出项目、支出类别、预算金额 |
| 档案归档 | `archive_mgmt` | 修缮档案 | 档案名称、档案类别、形成日期 |
| 专家评审 | `expert_review` | 评审意见 | 评审方案、专家姓名、专业领域 |
| 安全交底 | `safety_briefing` | 交底记录 | 交底工序、危险源、防护措施 |
| 施工排期 | `construction_schedule` | 排期计划 | 计划工序、开始日期、结束日期 |
| 变更签证 | `change_order` | 变更签证单 | 变更事项、变更原因、影响工序 |
| 竣工总结 | `completion_summary` | 竣工报告 | 修缮项目、开工日期、竣工日期 |

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

领域：应用开发
子方向：web前端/全栈开发
