"""配置：环境变量 → Settings（pydantic-settings）。

所有默认值与 docs（方案 §6.3/§8/§9）一致；容器场景数据目录 /data。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="LIAM_", env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- 服务 ---
    host: str = "0.0.0.0"
    port: int = 8080
    ui_password: str = ""
    secret_key: str = ""
    data_dir: Path = Path("./data")

    # --- 账号（仅掩码展示/预填，不参与签名）---
    phone: str = ""
    login_headless: bool = True      # 网页登录的内置浏览器是否无头；排查时可设 0

    # --- 采集节奏（秒）---
    poll_realtime: int = 60          # T1 全量批（awake）
    poll_drive_loc: int = 10         # T1++ 行驶定位微轮询
    poll_charge: int = 30            # T1+ 充电采样
    poll_config: int = 3600          # T2 配置档
    poll_sleep_probe: int = 60       # T0 休眠轻探测

    # --- 存储 ---
    retention_days: int = 90         # state_history 原始 change 保留天数（0=永久）
    battery_kwh: float = 42.8        # 电池容量（L7 Air 官方值；能耗推导用，可配）

    # --- MCP ---
    mcp_token: str = ""              # 空 = /mcp 整体关闭

    # --- 地图 ---
    amap_key: str = ""               # 空 → 高德栅格瓦片直连
    map_provider: str = "amap"       # amap | osm


@lru_cache
def get_settings() -> Settings:
    return Settings()
