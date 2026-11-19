"""理想 API 通道常量（livis / 理想同学客户端）。

配方钉住（方案 §13.1 红线）：8.25.4 / iPad UA / iOS 语义签名，
不随主 App 版本实验漂移；盯 ha-lixiang 上游作为前哨。
"""

from __future__ import annotations

API_APP = "https://api-app.lixiang.com"
API_ID = "https://id.lixiang.com"

# --- 理想同学（livis）客户端身份（与主 App「理想汽车」独立，方案 §3）---
LIVIS_CLIENT_ID = "40amUDKOdqQTaGDONZC1oY"
LIVIS_LOGIN_ENTRY = "https://account.lixiang.com/app-auth/livis"
SUBIDAAS_REDIRECT = "https://app.lixiang.com/login/subidaas/callback"

# --- 签名配方（2026-11-19 本项目实测通过，iOS 8.25.4 语义）---
SIGN_APP_VERSION = "8.25.4-10463"
UA_IPAD = f"m01/{SIGN_APP_VERSION} (iPad; iOS 16.7.12; Scale/2.00)"
UA_OKHTTP = "okhttp/4.12.0"

# --- scope / audience ---
AUD_VSS = "1j0vgTqagJUHuT6nLmbTGx"
SCOPE_VSS = "vss:get-batch"

# --- 端点 ---
EP_TOKEN = "/api/token"                        # refresh_token → LISA bearer
EP_AUTH = "/api/auth"                          # prompt=none → scope token（需 sso cookie）
EP_KEY_SUITE = "/ddes/v1-1/app/key-suite"       # hac_key 派生
EP_VSS_GET_BATCH = "/ssp-cloud-vss-service/mobile/vss/get-batch"
EP_TRAVEL_MONTHS = "/ssp-travel-x-service/v1-0/travel/months/{vin}"
EP_TRAVEL_MONTHLY = "/ssp-travel-x-service/v1-0/travel/monthly/{year}/{month}/{vin}"
EP_TRAVEL_DAILY = "/ssp-travel-x-service/v1-0/travel/daily/aggregate/{vin}"
EP_TRAVEL_ALL = "/ssp-travel-x-service/v1-0/travel/all/aggregate/{vin}"
EP_CHARGE_RECORDS = "/bsp-vcp-message/v1/app/vehicle/chargeRecords"
