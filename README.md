# LiAuto-Mate

自托管理想汽车数据记录器（TeslaMate 形态）——走「理想同学」（livis）通道，独立会话，**不与手机「理想汽车」App 冲突**；四态感知轮询（绝不唤醒车辆）、SQLite 单文件存储、自建 Web 看板、MCP `/mcp` 供 Agent 只读查车、单容器 Docker 部署。

> 设计文档：`../lixiangtongxue/docs/design/06-liauto-mate-可行性与设计方案.md`（v1.6）
> 协议与证据仓库：`../lixiangtongxue`（spike 实测、逆向报告、参考实现）

## 当前状态：M0 已完成（2026-11-19）

| 里程碑 | 内容 | 状态 |
|---|---|---|
| **M0 骨架+认证核心** | 仓库/配置/SQLite(WAL)/AES-GCM 凭据/key_suite+signer(8.25.4 配方)/LiApi/SessionStore+SessionKeeper/`/healthz`+`/api/v1/system/status`；单测 8/8；**真实链路 smoke 验收通过**（凭据恢复→refresh→scope→签名批读真实车况） | ✅ |
| M1 采集器+四态状态机 | T0–T4 分档轮询、行驶/充电/静止/休眠状态机、滞回与退避 | 待开工 |
| M2 会话推导 | 充电/行车/停车功耗、states 时间线、geofence 命名 | |
| M3 云端历史同步 | travel/chargeRecords/OTA/保养 | |
| M4 Web UI + MCP | 6 页 + 登录面板 + `/mcp` 只读 12 工具 | |
| M5 部署闭环 | 单容器 compose、首启向导 | |

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
