"""LiApi 异步客户端：token 刷新 / scope 换取 / VSS 批读。

M0 范围（方案 §5.1/§6）：
  · refresh_token → LISA bearer（76+ 天通道；轮换值由 SessionStore 持久化）
  · /api/auth prompt=none + sso cookies → 900s scope token（VSS 用）
  · vss/get-batch 签名批读（返回 {path: {value, ts}}）
错误纪律（方案 §5.2）：只有结构化失效（401/login_required/100105）才触发
会话状态变化；5xx/超时一律按临时错误上抛，由调用方退避 —— 防「每分钟重登」风暴
（ha-lixiang 真机踩坑教训）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.parse
import uuid
from typing import Any

import httpx

from .constants import (
    API_APP,
    API_ID,
    AUD_VSS,
    EP_AUTH,
    EP_CHARGE_RECORDS,
    EP_MAINT_PLAN,
    EP_OTA_UPGRADE,
    EP_TIRE_ALARM,
    EP_TOKEN,
    EP_TRAVEL_ALL,
    EP_TRAVEL_DAILY,
    EP_TRAVEL_MONTHLY,
    EP_TRAVEL_MONTHS,
    EP_VSS_GET_BATCH,
    LIVIS_CLIENT_ID,
    SCOPE_VSS,
    SUBIDAAS_REDIRECT,
    UA_OKHTTP,
)
from .key_suite import derive_identity
from .signer import sign_headers

log = logging.getLogger(__name__)


class AuthExpiredError(RuntimeError):
    """结构化会话失效（refresh_token 失效 / cookie 过期 login_required）。

    捕获方应将会话状态迁移到 NEED_LOGIN，而不是重试。
    """

    def __init__(self, reason: str, *, need_relogin: bool = False) -> None:
        self.reason = reason
        self.need_relogin = need_relogin
        super().__init__(reason)


class LiApiError(RuntimeError):
    """临时性错误（网络/5xx/非 0 码）——调用方应退避重试。"""


class LiApi:
    """有状态客户端：持有 bearer/cookies/device_id/hac 等运行时凭据。

    凭据由外部（SessionStore）注入与回写 —— 本类不碰数据库。
    全部方法 async；网络经 httpx.AsyncClient。
    """

    def __init__(
        self,
        *,
        refresh_token: str = "",
        bearer: str = "",
        cookies: dict[str, str] | None = None,
        device_id: str = "",
        vin: str = "",
        timeout: float = 20.0,
    ) -> None:
        self.refresh_token = refresh_token
        self.bearer = bearer
        self.cookies: dict[str, str] = dict(cookies or {})
        self.device_id = device_id or uuid.uuid4().hex
        self.vin = vin
        self._client = httpx.AsyncClient(timeout=timeout)
        # 签名身份（hac_hex, key_id）—— 首次签名调用前派生并缓存
        self._hac_hex = ""
        self._key_id = ""

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------
    # 凭据刷新
    # ------------------------------------------------------------------

    async def refresh_bearer(self) -> str:
        """refresh_token → 新 LISA bearer。

        ★ refresh_token 会轮换：响应里的新 refresh_token 必须由调用方立即
        持久化（方案 §5.1 红线）—— 本方法通过 `self.refresh_token` 更新并
        由 SessionKeeper 落库。
        失败（400/invalid_grant）→ AuthExpiredError(need_relogin=True)。
        """
        resp = await self._client.post(
            API_ID + EP_TOKEN,
            data={
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token,
                "client_id": LIVIS_CLIENT_ID,
            },
            headers={"User-Agent": UA_OKHTTP},
        )
        if resp.status_code in (408, 429) or resp.status_code >= 500:
            raise LiApiError(f"refresh 暂时不可用 HTTP {resp.status_code}")
        if resp.status_code != 200:
            raise AuthExpiredError(
                f"refresh 失败 HTTP {resp.status_code}",
                need_relogin=True,
            )
        try:
            data = resp.json()
        except ValueError as exc:
            raise LiApiError("refresh 返回无效 JSON") from exc
        if not isinstance(data, dict):
            raise LiApiError("refresh 返回格式异常")
        if not data.get("access_token"):
            raise AuthExpiredError(
                "refresh 响应缺少 access_token",
                need_relogin=True,
            )
        self.bearer = data["access_token"]
        if data.get("refresh_token"):
            self.refresh_token = data["refresh_token"]  # 轮换（调用方须落库）
        log.info("bearer 刷新成功 expires_in=%s", data.get("expires_in"))
        return self.bearer

    async def ensure_bearer(self) -> str:
        """确保有 bearer（没有则刷新）。过期探测不在此做——由调用方按 401 反应。"""
        if not self.bearer:
            await self.refresh_bearer()
        return self.bearer

    async def exchange_scope_token(self, scope: str = SCOPE_VSS) -> str:
        """sso cookies + bearer → 900s scope token（prompt=none）。

        需要 cookies（sso_token 等）；缺失/过期 → 服务端 login_required
        → AuthExpiredError(need_relogin=True)（= 13 天 cookie 到期，走重登）。
        """
        bearer = await self.ensure_bearer()
        resp = await self._client.post(
            API_ID + EP_AUTH,
            data={
                "client_id": LIVIS_CLIENT_ID,
                "audience": AUD_VSS,
                "response_type": "token",
                "scope": scope,
                "redirect_uri": SUBIDAAS_REDIRECT,
                "offline_access": "true",
                "prompt": "none",
                "device_id": self.device_id,
            },
            headers={
                "User-Agent": UA_OKHTTP,
                "Authorization": f"Bearer {bearer}",
            },
            cookies=self.cookies,
        )
        token = ""
        if resp.status_code == 200:
            token = (resp.json() or {}).get("access_token", "")
        elif resp.status_code in (302, 303):
            loc = resp.headers.get("location", "")
            frag = urllib.parse.urlparse(loc).fragment
            token = (urllib.parse.parse_qs(frag).get("access_token") or [""])[0]
            if not token:
                # 302 无 token = 服务端要求重新登录（cookie 到期/风控）
                raise AuthExpiredError(
                    f"/api/auth 302 无 token: {loc[:120]}", need_relogin=True)
        else:
            raise LiApiError(
                f"/api/auth HTTP {resp.status_code}: {resp.text[:120]}")
        if not token:
            raise AuthExpiredError("/api/auth 响应无 access_token",
                                   need_relogin=True)
        return token

    # ------------------------------------------------------------------
    # 签名身份（hac_key 派生，首次用时做一次）
    # ------------------------------------------------------------------

    async def _ensure_identity(self) -> tuple[str, str]:
        if not (self._hac_hex and self._key_id):
            log.info("派生签名身份（keySuite）…")
            bearer = await self.ensure_bearer()
            # derive_identity 是同步阻塞（urllib）—— 放线程池
            self._hac_hex, self._key_id = await asyncio.to_thread(
                derive_identity, "livis", self.device_id, bearer)
            log.info("签名身份就绪 key_id=%s…", self._key_id[:12])
        return self._hac_hex, self._key_id

    # ------------------------------------------------------------------
    # 业务：VSS 批读
    # ------------------------------------------------------------------

    async def vss_get_batch(self, vin: str, paths: list[str],
                            scope_token: str) -> dict[str, dict[str, Any]]:
        """签名调 vss/get-batch → {path: {value, ts, ...}}。

        ★ 服务端对任一无效 path 整批 400（ha-lixiang 实测）——
        调用方（M1 采集器）负责分批与降级，本方法不吞错。
        """
        hac_hex, key_id = await self._ensure_identity()
        body = json.dumps({"vin": vin, "paths": paths})
        headers = sign_headers(
            hac_hex, key_id, self.device_id, self.bearer,
            body=body, vin=vin)
        headers["Authorization"] = f"Bearer {scope_token}"
        resp = await self._client.post(
            API_APP + EP_VSS_GET_BATCH, content=body, headers=headers)
        if resp.status_code == 401:
            raise AuthExpiredError("vss 401：scope token 过期", need_relogin=False)
        if resp.status_code == 400:
            raise LiApiError(f"vss 400 invalid_path（检查路径集）: {resp.text[:120]}")
        if resp.status_code != 200:
            raise LiApiError(f"vss HTTP {resp.status_code}: {resp.text[:120]}")
        data = resp.json()
        if data.get("code") not in (None, 0):
            raise LiApiError(
                f"vss code={data.get('code')} msg={data.get('msg') or data.get('message')}")
        items = data.get("items") or (data.get("data") or {}).get("items") or []
        out: dict[str, dict[str, Any]] = {}
        for it in items:
            dp = it.get("dp") or {}
            out[it.get("path", "")] = dp
        return out

    # ------------------------------------------------------------------
    # 业务：云端历史（travel / chargeRecords —— 主 Bearer 通道，方案 §4.2）
    # ------------------------------------------------------------------

    async def _signed_get_travel(self, path: str) -> Any:
        """travel/chargeRecords 族：签名 GET（主 Bearer 通道，方案 §4.2）。

        ★ 实测（11-19）：travel 网关对 GET 也要求 Content-MD5（空 body 的
        MD5）——缺头返回 100002「缺少必要的请求参数: Content-MD5」。
        100105（主 Bearer 过期）→ 刷新重试一次（ha-lixiang 配方）。
        """
        hac_hex, key_id = await self._ensure_identity()
        headers = sign_headers(
            hac_hex, key_id, self.device_id, self.bearer,
            method="GET", accept="*/*", content_type="",
            body="", vin=self.vin)
        # 保留空 body Content-MD5（勿删——travel 网关必需）
        headers["Authorization"] = f"Bearer {self.bearer}"
        resp = await self._client.get(
            API_APP + path, headers=headers)
        data: dict = {}
        try:
            data = resp.json()
        except ValueError:
            pass
        if data.get("code") == 100105:
            # 主 Bearer 过期 → 刷新一次重试
            await self.refresh_bearer()
            headers["Authorization"] = f"Bearer {self.bearer}"
            headers["X-CHJ-TOKEN"] = self.bearer
            resp = await self._client.get(API_APP + path, headers=headers)
            try:
                data = resp.json()
            except ValueError:
                data = {}
        if resp.status_code != 200 or data.get("code") not in (None, 0):
            raise LiApiError(
                f"travel HTTP {resp.status_code} code={data.get('code')} "
                f"{str(data.get('msg') or data.get('message'))[:100]}")
        return data.get("data")

    # --- 具体端点（ha-lixiang 已验证的 4 个 travel + chargeRecords）---

    async def get_travel_months(self) -> Any:
        return await self._signed_get_travel(
            EP_TRAVEL_MONTHS.format(vin=self.vin))

    async def get_travel_monthly(self, year: int, month: int) -> Any:
        return await self._signed_get_travel(
            EP_TRAVEL_MONTHLY.format(vin=self.vin, year=year, month=month))

    async def get_travel_daily(self, start_date: str, end_date: str) -> Any:
        return await self._signed_get_travel(
            f"{EP_TRAVEL_DAILY.format(vin=self.vin)}"
            f"?startDate={start_date}&endDate={end_date}")

    async def get_travel_all(self) -> Any:
        return await self._signed_get_travel(EP_TRAVEL_ALL.format(vin=self.vin))

    async def get_charge_records(self, page: int = 1, page_size: int = 50) -> Any:
        return await self._signed_get_travel(
            f"{EP_CHARGE_RECORDS}?vin={self.vin}&page={page}&pageSize={page_size}")

    async def get_ota_upgrade(self) -> Any:
        """OTA 升级状态/历史（ha-lixiang 已验端点；响应字段首验后修正）。"""
        return await self._signed_get_travel(EP_OTA_UPGRADE.format(vin=self.vin))

    async def get_maint_plan(self) -> Any:
        """保养计划（里程/到期项）。"""
        return await self._signed_get_travel(EP_MAINT_PLAN.format(vin=self.vin))

    async def get_tire_alarm(self) -> Any:
        """胎压告警历史。"""
        return await self._signed_get_travel(EP_TIRE_ALARM.format(vin=self.vin))
