"""网页账号登录面板路由（M5/D4，方案 §5.3 三步 UX）。

短信限频纪律：只有短信真正发出后才开始 10 分钟冷却（实测 ~2 条/10min）。
滑块失败发生在发短信前，不消耗短信，也不应阻止用户立即重试。
凭据落库后由服务恢复会话，页面可以随时关闭。
"""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse

from ..login.flow import S_DONE, S_IDLE
from .pages import templates
from .ui_auth import require_ui_session

router = APIRouter(dependencies=[Depends(require_ui_session)])


@router.get("/liam-login", response_class=HTMLResponse)
async def panel(request: Request):
    settings = request.app.state.settings
    flow = request.app.state.login_flow
    return templates.TemplateResponse(request, "liam_login.html", {
        "phone": settings.phone,
        "cooldown_left": flow.cooldown_left(),
        "login_error": flow.dependency_problem(),
    })


@router.get("/liam-login/status.json")
async def flow_status(request: Request):
    flow = request.app.state.login_flow
    return JSONResponse({**flow.status(), "cooldown_left": flow.cooldown_left()})


@router.post("/liam-login/start")
async def flow_start(request: Request):
    flow = request.app.state.login_flow
    form = await request.form()
    phone = re.sub(r"\s+", "", str(form.get("phone") or ""))
    if not (phone.isdigit() and len(phone) == 11):
        return JSONResponse({"ok": False, "error": "手机号应为 11 位数字"},
                            status_code=400)
    left = flow.cooldown_left()
    if left > 0:
        return JSONResponse(
            {"ok": False, "cooldown_left": left,
             "error": f"短信发送冷却中，请在 {left} 秒后再次发起登录。"},
            status_code=429)
    ok, msg = flow.start(phone)
    return JSONResponse(
        {"ok": ok, "error": None if ok else msg, "cooldown_left": flow.cooldown_left()},
        status_code=200 if ok else 400)


@router.post("/liam-login/code")
async def flow_code(request: Request):
    flow = request.app.state.login_flow
    form = await request.form()
    ok, msg = flow.submit_code(str(form.get("code") or "").strip())
    return JSONResponse({"ok": ok, "error": None if ok else msg},
                        status_code=200 if ok else 400)


@router.get("/liam-login/finish.json")
async def flow_finish(request: Request):
    """确认后台已完成恢复，清除面板的完成状态。"""
    flow = request.app.state.login_flow
    st = flow.status()
    if st["state"] == S_DONE:
        flow._set(S_IDLE)
        return JSONResponse({"ok": True, "state": S_DONE,
                             "next": "/" if request.app.state.vin else "/settings#session"})
    return JSONResponse({"ok": False, "state": st["state"]})
