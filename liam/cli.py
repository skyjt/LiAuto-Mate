"""CLI 入口：liam serve / liam smoke-m0 / liam import-session。

M0 用：serve（起服务+keeper）、smoke-m0（真实链路手动验收，显式 opt-in）、
import-session（从 spike session.json 导入凭据 —— 开发期桥梁）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="liam")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("serve", help="启动 LiAuto-Mate 服务（API + 采集器）")
    p_smoke = sub.add_parser(
        "smoke-m0", help="M0 验收：真实链路（refresh→scope→vss 批读）——显式手动运行")
    p_smoke.add_argument("--vin", required=True)
    p_smoke.add_argument("--paths", default="5")
    p_import = sub.add_parser("import-session", help="导入 spike session.json")
    p_import.add_argument("file", type=Path)
    p_import.add_argument("--vin", required=True)

    args = parser.parse_args()
    if args.cmd == "serve":
        return asyncio.run(_serve())
    if args.cmd == "smoke-m0":
        return asyncio.run(_smoke_m0(args.vin, int(args.paths)))
    if args.cmd == "import-session":
        return _import_session(args.file, args.vin)
    return 2


def _build_stack(vin: str):
    """装配：settings → db → secrets → api → store → keeper。"""
    from .config import get_settings
    from .core.db import Database
    from .core.secrets import SecretBox
    from .auth.li_api import LiApi
    from .auth.session_store import SessionStore
    from .auth.session_keeper import SessionKeeper

    settings = get_settings()
    db = Database(settings.data_dir / "liam.db")
    box = SecretBox.load_or_create(settings.data_dir)
    api = LiApi()
    store = SessionStore(db, box)
    keeper = SessionKeeper(api, store)
    return settings, db, api, store, keeper


async def _serve() -> int:
    import uvicorn
    from contextlib import asynccontextmanager

    settings, _db, _api, _store, keeper = _build_stack(vin="")
    from .web.app import create_app
    keeper_ref: dict = {"keeper": None}

    @asynccontextmanager
    async def lifespan(_app):
        await keeper.start()
        keeper_ref["keeper"] = keeper
        yield
        await keeper.stop()

    app = create_app(settings, keeper_ref, lifespan)
    config = uvicorn.Config(app, host=settings.host, port=settings.port,
                            log_level="info")
    server = uvicorn.Server(config)
    await server.serve()
    return 0


async def _smoke_m0(vin: str, n_paths: int) -> int:
    """M0 验收链路：重启恢复 → refresh → scope → vss 批读 → 落库回写。

    ★ 打真实 API —— 仅手动运行，绝不进 CI（方案 §13.1 红线）。
    """
    settings, db, api, store, keeper = _build_stack(vin)
    print(f"[smoke-m0] data_dir={settings.data_dir} db=liam.db")
    try:
        restored = store.restore_into(api)
        print(f"[1] 凭据恢复: {'OK' if restored else '无存档（需先 import-session）'}")
        if not restored:
            return 1
        print(f"[2] refresh_token → bearer …")
        await api.refresh_bearer()
        store.persist_all(api)   # ★ 轮换值立即落库
        print(f"    OK bearer={api.bearer[:16]}…")
        print(f"[3] 换 scope token（cookies={len(api.cookies)} 个）…")
        scope_tok = await keeper.get_scope_token()
        print(f"    OK scope={scope_tok[:16]}…")
        # 探测用最小路径集（spike 实测过的 5 条）
        paths = [
            "Vehicle.Powertrain.Battery.ResidueBattery",
            "Vehicle.Powertrain.Battery.ChargeStatus",
            "Vehicle.Cabin.AC.FrtACIncarTemp",
            "Vehicle.Body.DoorLockStatus.MainDoor",
            "Vehicle.Location.CurrentLocationInfo",
        ][:max(1, n_paths)]
        print(f"[4] vss/get-batch（{len(paths)} 路径）…")
        result = await api.vss_get_batch(vin, paths, scope_tok)
        for p, dp in result.items():
            v = dp.get("value")
            if isinstance(v, str) and len(v) > 60:
                v = v[:60] + "…"
            print(f"    {p:55s} = {v!r}")
        if not result:
            print("    !! 无返回")
            return 1
        print(f"[5] 会话状态: {keeper.status()}")
        print("\n[✓] M0 smoke 全链路通过：恢复→刷新→换票→签名批读。")
        return 0
    finally:
        await api.aclose()


def _import_session(file: Path, vin: str) -> int:
    """spike session.json → session_store（开发期桥梁；生产走 Web 登录面板）。"""
    from .config import get_settings
    from .core.db import Database
    from .core.secrets import SecretBox
    from .auth.session_store import (
        K_BEARER, K_COOKIES, K_DEVICE, K_REFRESH, SessionStore)

    data = json.loads(file.read_text())
    settings = get_settings()
    db = Database(settings.data_dir / "liam.db")
    box = SecretBox.load_or_create(settings.data_dir)
    store = SessionStore(db, box)
    store.save(K_BEARER, data["bearer"])
    store.save(K_REFRESH, data["refresh_token"])
    store.save(K_COOKIES, data["cookies"])
    store.save(K_DEVICE, data["device_id"])
    # VIN 也存 meta（M1 车辆配置用）
    db.execute(
        "INSERT INTO meta(key, value) VALUES('vin', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (vin,))
    print(f"[✓] 已导入凭据到 {settings.data_dir}/liam.db（vin={vin}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
