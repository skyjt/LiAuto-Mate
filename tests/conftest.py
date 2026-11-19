"""测试夹具：临时数据目录 + 依赖注入桩。

红线（方案 §13.1）：所有测试 fixture 回放，永不打真实 API。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.fixture()
def tmp_data(tmp_path, monkeypatch):
    """隔离的 data 目录 + 清空缓存的 settings。"""
    from liam.config import get_settings
    get_settings.cache_clear()
    monkeypatch.setenv("LIAM_DATA_DIR", str(tmp_path / "data"))
    yield tmp_path / "data"
    get_settings.cache_clear()
