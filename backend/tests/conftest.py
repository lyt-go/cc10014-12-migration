"""pytest 全局配置：把测试运行指向独立的临时 SQLite 数据库，不污染开发数据。

必须在任何 app.* 导入之前设置 APP_DB_PATH——conftest.py 由 pytest 最先加载。
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

_TEST_DIR = Path(tempfile.gettempdir()) / "gujian_test_db"
_TEST_DIR.mkdir(parents=True, exist_ok=True)
# 每个工作进程一个数据库文件；测试夹具会在每条用例前重置为示例数据。
os.environ.setdefault("APP_DB_PATH", str(_TEST_DIR / "pytest.sqlite3"))
