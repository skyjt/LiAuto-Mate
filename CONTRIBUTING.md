# 开发与验证

日常开发可以在没有车辆、手机号和登录凭据的环境中完成。要定位功能所属的模块和表，先看[项目架构与数据流](docs/项目架构.md)。

## 本地开发

从仓库根目录运行以下命令。需要 Python 3.12 或以上版本，并带有 `venv` 和 `pip`。Debian / Ubuntu 的 Python 3.12 如提示缺少 `ensurepip`，对应的系统包是 `python3.12-venv`。

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev,login]'
python -m pytest -q
```

`dev` 提供 pytest、pytest-asyncio、respx 和 Ruff。完整测试还会从 `login` 依赖组使用 NumPy。运行测试和打开空库看板均无需下载浏览器。使用网页账号登录功能时才需要执行 `python -m playwright install chromium`，容器镜像在构建时已安装 Chromium。[依赖与工具配置](pyproject.toml)

用一个新的数据目录启动服务，可以检查与部署相同的入口，又能避免读取已有车辆会话。以下命令只监听本机。

```bash
dev_data="$(mktemp -d)"
LIAM_DATA_DIR="$dev_data" LIAM_HOST=127.0.0.1 LIAM_PORT=8080 liam serve
```

在另一个终端查看进程和账号会话。预期第一个响应含 `"ok": true`，第二个响应的 `session.state` 为 `need_login`。可直接访问 `http://127.0.0.1:8080/` 查看空数据页面。若配置了 `LIAM_UI_PASSWORD`，先使用该密码登录。

```bash
curl -fsS http://127.0.0.1:8080/healthz
curl -fsS http://127.0.0.1:8080/api/v1/system/status
```

接入本人车辆时，可在设置页绑定 VIN 并登录理想账号。已有会话文件也可停服后用 `liam import-session "$SESSION_FILE" --vin "$VIN"` 导入同一个 `LIAM_DATA_DIR`。模拟接车用临时库与合成 VIN，按「先绑定」和「先登录」两种顺序分别验证。

## 实现约定

- **Python 与配置**　遵循现有模块的类型注解和命名。Ruff 目标为 `py312`，行宽 100。新增运行参数优先放进 `Settings`，同步说明 `LIAM_*` 变量、默认值和单位。容器也要支持的参数需检查 Compose 的 `environment` 是否传入。[配置类](liam/config.py) · [环境示例](.env.example) · [Compose](compose.yml)
- **采集与鉴权**　保持单账号、单车、只读。新增 VSS 路径先用脱敏样本确认拼写、返回结构和单位，再选择已有轮询档位。默认每批最多 50 条，保留休眠轻探测。区分临时请求错误与账号会话失效，刷新成功后立即保存轮换值，避免高频重登。[分档与批大小](liam/collector/paths.py) · [会话看护](liam/auth/session_keeper.py)
- **写库与升级**　通过 `Database` 绑定 SQL 参数，需要新行 ID 时用 `db.insert()`。多步写入放在 `with db.transaction():` 内，所有写入成功后一起提交。用中途失败的用例验证回滚，事务块内只做同步数据库操作。改表在 `_migrate()` 增加版本分支并更新 `USER_VERSION`，用临时库检查首次建库、旧版本带数据升级和再次打开后的结果。[SQLite 封装](liam/core/db.py)
- **指标与展示**　区分车端时间和入库时间，保留毫秒单位。经纬度按 WGS-84 保存。有效值 `0` 和缺失值 `None` 分开处理。VSS 字段可能是嵌套 JSON，展示前提取业务值。例如车外温度原值 `164` 显示为 `16.4 °C`，Web 与 MCP 应得到一致结果。跨入口的新统计优先放进[共享分析模块](liam/collector/analysis.py)。
- **接口与隐私**　新增 Web 数据或写入路由要在服务端校验访问权限，MCP 保持 Bearer 鉴权并仅读本地数据。请用合成输入或脱敏回放。提交代码、日志、截图和 fixture 前检查手机号、VIN、精确轨迹、账号标识和令牌。`.env`、会话文件、整库和加密密钥均留在本机。[忽略规则](.gitignore)

## 按改动选择验证

| 改动范围 | 优先运行或检查 |
| --- | --- |
| 认证客户端、凭据与重启恢复 | `python -m pytest -q tests/test_li_api.py tests/test_m0_core.py`。HTTP 用 respx 模拟，凭据用临时密钥。 |
| 状态判定、行车与充电会话 | `python -m pytest -q tests/test_state_machine.py tests/test_sessionizer.py`。用连续多轮输入检查状态切换、会话开闭和样本归属。 |
| 网页登录与采集启停 | `python -m pytest -q tests/test_login_flow.py tests/test_login_captcha.py tests/test_service.py`。模拟凭据保存、失效、恢复和重复提交，核对实时与历史任务的启停次数；用合成图检查滑块缺口/拼图匹配和拖拽轨迹。 |
| 页面、JSON、MCP 或数据库迁移 | 看板回归运行 `python -m pytest -q tests/test_web_ui.py`。用临时目录启动，检查受影响的入口、空数据和含合成数据的结果。MCP 变更用客户端的 `tools/list` 检查注册，调用受影响的查询核对字段和单位。 |

逐页交互和手机布局的检查步骤见[看板逐页验收](docs/UI验收.md)。

全量回归运行 `python -m pytest -q`。测试使用 `tmp_path` 或 `tmp_data` 隔离磁盘状态，修改配置的用例注意 `get_settings()` 的缓存。[测试夹具](tests/conftest.py)

提交前可运行 `ruff check liam tests` 与 `git diff --check` 检查诊断和补丁空白。当前代码已有 Ruff 告警，处理本次改动引入的问题即可。提交说明写明改了什么、如何验证，以及对既有数据或配置的影响。涉及移植代码时保留[上游署名与许可证](docs/HA-LIXIANG-LICENSE)。

`smoke-m0`、`observe` 和 `spike/` 下的联网脚本会访问真实服务，仅供车主显式手动验证。自动化测试和 CI 始终使用本地回放。
