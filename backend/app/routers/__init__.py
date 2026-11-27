"""业务路由汇总：所有模块共用一套资源路由，由领域服务完成分派。"""
from __future__ import annotations

from app.routers import persistence, resource

# persistence 含固定路径（交接清单、动作状态），必须排在动态 /{module} 资源路由之前。
ROUTERS = [persistence, resource]
