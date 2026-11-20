"""网页重登面板路由（M5/D4，方案 §5.3 三步 UX）。

短信限频纪律：start 前 10 分钟冷却（实测 ~2 条/10min）。
done 后自动 keeper.recover()（NEED_LOGIN → OK，恢复采集）。
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from ..login.flow import S_DONE, S_FAILED, S_IDLE
from .pages import templates

router = APIRouter()

# 进程内冷却（短信纪律）；失效可容忍（重启清零）
_last_start_ts: list[float] = [0.0]
START_COOLDOWN_S = 600


async def _panel(request: Request, **ctx) -> HTMLResponse:
    auth = request.app.state.ui_auth
    if not auth.guard(request):
        from fastapi.responses import RedirectResponse
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse(request, "liam_login.html", ctx)


@router.get("/liam-login", response_class=HTMLResponse)
async def panel(request: Request):
    flow = request.app.state.login_flow
    settings = request.app.state.settings
    return await _panel(
        request, f=flow.status(), phone=settings.phone,
        cooldown_left=max(0, int(START_COOLDOWN_S - (time.time() - _last_start_ts[0]))))


@router.get("/liam-login/status.json")
async def flow_status(request: Request):
    flow = request.app.state.login_flow
    return JSONResponse(flow.status())


@router.post("/liam-login/start")
async def flow_start(request: Request):
    flow = request.app.state.login_flow
    form = await request.form()
    phone = str(form.get("phone") or "").strip()
    if not (phone.isdigit() and len(phone) == 11):
        return JSONResponse({"ok": False, "error": "手机号应为 11 位数字"}, status_code=400)
    left = int(START_COOLDOWN_S - (time.time() - _last_start_ts[0]))
    if left > 0:
        return JSONResponse(
            {"ok": False,
             "error": f"短信限频冷却中，{left // 60} 分钟后再试"}, status_code=429)
    ok, msg = flow.start(phone)
    if ok:
        _last_start_ts[0] = time.time()
    return JSONResponse({"ok": ok, "error": None if ok else msg}, status_code=200)


@router.post("/liam-login/code")
async def flow_code(request: Request):
    flow = request.app.state.login_flow
    form = await request.form()
    ok, msg = flow.submit_code(str(form.get("code") or "").strip())
    return JSONResponse({"ok": ok, "error": None if ok else msg},
                        status_code=200 if ok else 400)


@router.get("/liam-login/finish.json")
async def flow_finish(request: Request):
    """面板轮询的收尾检查：DONE → keeper.recover 并返回成功。"""
    flow = request.app.state.login_flow
    st = flow.status()
    if st["state"] == S_DONE:
        keeper = request.app.state.keeper_ref.get("keeper")
        if keeper:
            await keeper.recover()
            flow._set(S_IDLE)      # 面板清零，允许下次再登
        return JSONResponse({"ok": True, "state": "done"})
    if st["state"] == S_FAILED:
        # 失败后冷却减半（滑块失败无短信消耗）
        _last_start_ts[0] = min(_last_start_ts[0], time.time() - START_COOLDOWN_S / 2)
    return JSONResponse({"ok": False, "state": st["state"]})
