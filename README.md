# LiAuto-Mate

自托管理想汽车数据记录器（TeslaMate 形态）——走「理想同学」（livis）通道，独立会话，**不与手机「理想汽车」App 冲突**；四态感知轮询（绝不唤醒车辆）、SQLite 单文件存储、自建 Web 看板、MCP `/mcp` 供 Agent 只读查车、单容器 Docker 部署。

- 本机开发、运行离线测试与提交改动见[开发与验证](CONTRIBUTING.md)，自动化协作入口见 [AGENTS.md](AGENTS.md)。
- 启动条件、轮询节奏、数据表与代码入口见[项目架构与数据流](docs/项目架构.md)。
- 早期选型及实车观察见[可行性与设计方案 v1.6](docs/可行性与设计方案.md)。

## 里程碑记录（2026-11-20）

| 里程碑 | 内容 | 状态 |
|---|---|---|
| **M0 骨架+认证核心** | SQLite(WAL)/AES-GCM 凭据/key_suite+signer(8.25.4)/LiApi/SessionKeeper；smoke 全链路验收 | ✅ |
| **M1 采集器+四态状态机** | 76 实测路径分档、滞回状态机、ts 分层修正 | ✅ |
| **M2 会话推导** | 行车/充电会话+positions+geofence；真实行车会话落库验证 | ✅ |
| **M3 云端历史同步** | travel+chargeRecords 幂等入库（实测 40+20 条） | ✅ |
| **M4 Web UI + MCP** | 六页看板、UI 密码登录、同端口的只读 MCP 查询 | ✅ |
| **M5 部署闭环** | Dockerfile(2.2GB 含 Chromium，libatomic1 实测修复)+compose+**网页重登面板**（滑块自动解、失败不耗短信可重试）+**干净容器验收通过** | ✅ |
| **M6-info 信息深化** | T4 云端同步+retention 自动跑；行程能耗推导（SOC×42.8kWh 可配）+效率列+24h 时间线；充电月度统计图；电池电量采样分布；geofence 管理 UI+数据导出（整库/CSV）；趋势域分组 | ✅ |

> 车控（M6-control）明确不在当前范围——只做信息获取（TeslaMate 形态）。

待实车标定项（不阻塞）：充电枚举（等一次真实充电）、Trip 单位（一次行程核对）、重登面板端到端（**12-02 前后 sso cookie 到期时真实演练**）。

## 部署（单容器/单卷/单端口）

首次部署先执行 `cp .env.example .env`，在 `.env` 中填写看板密码 `LIAM_UI_PASSWORD`。以下命令用当前仓库构建镜像并启动服务。[配置示例](.env.example) · [Compose 配置](compose.yml)

```bash
docker compose up -d --build --wait
curl -fsS http://127.0.0.1:8080/healthz
```

打开 `http://127.0.0.1:8080/`，用配置的密码进入看板。`/liam-login` 用于理想账号登录和重登。面板会自动填写手机号、解开滑块并发送短信；滑块未通过时不会消耗短信，可直接重试，只有短信发出后才进入 10 分钟冷却。启用 UI 密码后，六个看板、图表数据、地点管理、导出和账号重登均使用同一登录会话。健康检查与服务状态接口可独立访问，远程部署应使用可信网络或统一鉴权代理。

**首次接车**　进入「设置 → 会话」，核对行驶证上的 17 位 VIN，点击「绑定车辆」，再进入「理想账号登录」。也可以先登录账号，完成后页面会引导绑定 VIN。服务在两项就绪时自动启动采集。每个数据目录对应一辆车，绑定后可在设置里核对 VIN 尾号。

**导入已有会话**　已有本人车辆的会话文件时，也可以停服后导入同一数据卷，再启动服务。`SESSION_FILE` 是宿主机上的文件绝对路径，`VIN` 是该车的车架号。

```bash
docker compose stop liauto-mate
docker compose run --rm --no-deps -v "${SESSION_FILE:?}:/run/session.json:ro" \
  liauto-mate liam import-session /run/session.json --vin "${VIN:?}"
docker compose up -d
```

备份或迁移时先停止服务，复制挂在 `/data` 的整个 `liauto-mate-data` 卷，包括隐藏文件 `.secret.key` 和 SQLite 的 WAL 文件（如存在）。部署用的 `.env` 单独保管。ARM NAS 与 x86 主机可用 Buildx 分别构建 `linux/arm64` 和 `linux/amd64` 镜像。

## MCP 接入

常规安装和默认容器镜像都包含 Python MCP SDK 1.x。客户端连接后可查询当前绑定车辆的本地数据。[依赖声明](pyproject.toml) · [Dockerfile](Dockerfile)

在 `.env` 中设置 `LIAM_MCP_TOKEN` 并重启服务。同机 Agent 用以下配置连接 Streamable HTTP，令牌留空时服务会关闭 MCP。

```json
{ "mcpServers": { "liauto-mate": {
    "url": "http://127.0.0.1:8080/mcp/",
    "headers": { "Authorization": "Bearer <LIAM_MCP_TOKEN>" } } } }
```

客户端可用 `tools/list` 查看本实例注册的工具。车况类查询读取最近快照，可用返回的时间戳和 `stale` 字段判断新鲜度。[工具实现](liam/mcp_server.py)

| 查询内容 | 工具 |
| --- | --- |
| 车况与信号 | `get_vehicle_status`、`get_state_group`、`get_signal_history`、`get_states_timeline` |
| 行车、轨迹与地点 | `get_drive_sessions`、`get_drive_route`、`get_geofences`、`get_visited_places` |
| 充电与能耗 | `get_charge_sessions`、`get_charge_records_cloud`、`get_efficiency_stats`、`get_battery_health`、`get_vampire_drain` |
| 版本、保养与服务健康 | `get_ota_updates`、`get_system_health` |

## Spike（协议验证，2026-11-19 全链路实测）

`spike/` 是可行性定稿时的协议验证脚本（纯 Python HTTP，无 App/无模拟器）：`browser_login.py`（Playwright 浏览器滑块登录）→ `login_sms.py`（纯 requests 短信登录，被滑块拦截时用前者）→ `spike_vss.py` / `finish_vss.py`（keySuite 派生 + 签名调 `vss/get-batch`）。key_suite 直接复用 `liam/auth/key_suite.py`。

个人值一律环境变量传入（`LX_PHONE` / `LX_VIN` / `LX_REFRESH_TOKEN`）；运行产物（`session.json`、截图、`login_state.*`）已封死在 .gitignore，`session.json` 经 `liam.cli import-session` 导入产品。依赖：`pip install -e '.[login]'`，浏览器路线另需 `python -m playwright install chromium`。

## 红线（§13.1，第一天立）

- CI/测试**永不打真实 API**（fixture 回放；smoke 显式手动）
- `.env`/`session.json`/`*.db`/`data/` 已封死在 .gitignore；开源前 gitleaks 全量扫描
- 单账号单车，绝不压测云端；登录按短信限频排期
- 配方钉住 8.25.4/iPad UA；盯 ha-lixiang 上游

## 依赖致谢

- [ha-lixiang](https://github.com/C3H3-AI/ha-lixiang)（MIT）：key_suite/signer/li_api 移植来源，见 `docs/HA-LIXIANG-LICENSE`

## 合规

仅用于本人账号/本人车辆的互操作与自用研究；非官方接口存在风控/封号的剩余风险；不绕过风控、不共享账号、不公开任何密钥。
