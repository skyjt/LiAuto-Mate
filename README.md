# LiAuto-Mate

自托管理想汽车数据记录器（TeslaMate 形态）——走「理想同学」（livis）通道，独立会话，**不与手机「理想汽车」App 冲突**；四态感知轮询（绝不唤醒车辆）、SQLite 单文件存储、自建 Web 看板、MCP `/mcp` 供 Agent 只读查车、单容器 Docker 部署。

> 设计文档：`../lixiangtongxue/docs/design/06-liauto-mate-可行性与设计方案.md`（v1.6）
> 协议与证据仓库：`../lixiangtongxue`（spike 实测、逆向报告、参考实现）

## 当前状态：M0–M5 全部完成并真实验证（2026-11-20）

| 里程碑 | 内容 | 状态 |
|---|---|---|
| **M0 骨架+认证核心** | SQLite(WAL)/AES-GCM 凭据/key_suite+signer(8.25.4)/LiApi/SessionKeeper；smoke 全链路验收 | ✅ |
| **M1 采集器+四态状态机** | 76 实测路径分档、滞回状态机、ts 分层修正 | ✅ |
| **M2 会话推导** | 行车/充电会话+positions+geofence；真实行车会话落库验证 | ✅ |
| **M3 云端历史同步** | travel+chargeRecords 幂等入库（实测 40+20 条） | ✅ |
| **M4 Web UI + MCP** | 6 页+登录/UI 密码 + `/mcp` 只读 12 工具真实验证 | ✅ |
| **M5 部署闭环** | Dockerfile(2.2GB 含 Chromium，libatomic1 实测修复)+compose+**网页重登面板**（滑块自动解全流程移植）+**干净容器验收通过** | ✅ |
| **M6-info 信息深化** | T4 云端同步+retention 自动跑；行程能耗推导（SOC×42.8kWh 可配）+效率列+24h 时间线；充电月度统计图；电池充电深度直方图；geofence 管理 UI+数据导出（整库/CSV）；趋势域分组 | ✅ |

> 车控（M6-control）明确不在当前范围——只做信息获取（TeslaMate 形态）。

待实车标定项（不阻塞）：充电枚举（等一次真实充电）、Trip 单位（一次行程核对）、重登面板端到端（**12-02 前后 sso cookie 到期时真实演练**）。

## 部署（单容器/单卷/单端口）

```bash
cp .env.example .env           # LIAM_UI_PASSWORD 必填
docker compose up -d           # 首次构建 ~5-10min（含 Chromium）
# 首启：浏览器打开 http://<host>:8080 → 设置密码登录 → /liam-login 完成理想账号短信登录
```

- 全部状态在 `liauto-mate-data` 卷（SQLite + AES-GCM 加密凭据）；备份/迁移 = 拷卷
- 登录过期（约 13 天一次）：概览页横幅「→ 网页重新登录」→ 三步完成（自动滑块 → 输短信码 → 恢复采集）
- ARM NAS：`docker buildx build --platform linux/arm64,linux/amd64 --push ...` 出多架构镜像

## MCP（Agent 接入）

设置 `LIAM_MCP_TOKEN` 后启动，Agent 端点 `http://<host>:8080/mcp/`（streamable HTTP，Bearer 必需）：

```json
{ "mcpServers": { "liauto-mate": {
    "url": "http://192.168.x.x:8080/mcp/",
    "headers": { "Authorization": "Bearer <LIAM_MCP_TOKEN>" } } } }
```

12 个只读工具：`get_vehicle_status`（实时快照含 stale 标志）/`get_state_group`/`get_signal_history`/`get_drive_sessions`/`get_drive_route`/`get_charge_sessions`/`get_charge_records_cloud`/`get_efficiency_stats`/`get_geofences`/`get_states_timeline`/`get_battery_health`/`get_system_health`。未设 token = MCP 整体关闭。

## 快速开始（开发机）

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
cp .env.example .env            # 填 LIAM_UI_PASSWORD 等
.venv/bin/python -m liam.cli import-session <spike session.json> --vin <VIN>   # 开发期桥梁
.venv/bin/python -m liam.cli serve                                             # http://localhost:8080
.venv/bin/python -m pytest tests/ -q                                            # 单测（fixture 回放，永不打真实 API）
.venv/bin/python -m liam.cli smoke-m0 --vin <VIN>                              # 真实链路验收（手动）
```

## 红线（§13.1，第一天立）

- CI/测试**永不打真实 API**（fixture 回放；smoke 显式手动）
- `.env`/`session.json`/`*.db`/`data/` 已封死在 .gitignore；开源前 gitleaks 全量扫描
- 单账号单车，绝不压测云端；登录按短信限频排期
- 配方钉住 8.25.4/iPad UA；盯 ha-lixiang 上游

## 依赖致谢

- [ha-lixiang](https://github.com/C3H3-AI/ha-lixiang)（MIT）：key_suite/signer/li_api 移植来源，见 `docs/HA-LIXIANG-LICENSE`

## 合规

仅用于本人账号/本人车辆的互操作与自用研究；非官方接口存在风控/封号的剩余风险；不绕过风控、不共享账号、不公开任何密钥。
