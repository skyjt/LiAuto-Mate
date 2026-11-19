"""VSS 值解析（M1 补充）：车端 ts 不是统一时钟——按路径分层。

观察期首个数据发现（11-19 晚）：
  · ConnectStatus.5G      ts 新鲜（分钟级）——车真的在线时它持续刷新
  · ChargeStatus          ts 落后 7312 分钟（5 天）——值没变就不带新 ts
  · TravelStatus/Location ts 落后 ~620 分钟——同上，静止时停更
  · Trip.Total            ts 分钟级新鲜——行驶计数器持续刷新
推论：**ts 新鲜度只能对「持续上报型」路径有意义**（5G/Trip.Total），
「变化才上报型」路径（ChargeStatus/Location）ts 停更 ≠ 休眠。
休眠判定改为：5G online=false + Trip.Total ts 停更（双确认）。
"""

from __future__ import annotations

import json
from typing import Any

# 持续上报型（ts 新鲜度判定可用）
P_ALWAYS_FRESH = {
    "Vehicle.ConnectManager.ConnectStatus.5G",
    "Vehicle.ConnectManager.ConnectStatus.xcu",
    "Vehicle.ConnectManager.ConnectStatus.hu-f",
    "Vehicle.Carcenter.Trip.Total",
}
