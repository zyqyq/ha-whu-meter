"""API client for the WHU Water & Electricity service platform (ICBS_V2_Server).

Replicates the exact request chain used by the official mobile web app:
  1. SpecialSignIn with credentials baked into the frontend JS
     (p1=account, p2=md5(password), p3=sysid, t=unix seconds, s=md5(query))
     -> returns an access token used as `Authorization: Bearer <token>`.
  2. Business endpoints for areas / buildings / rooms / meters / reserve / day values.

=============================================================================
2026-10-12 限流改造 —— 服务端高频访问的硬性保护
=============================================================================
服务器对高频访问敏感，因此要求进程内**所有**访问路径合计「最高 1 秒 1 次」。
本模块用三道机制保证这一点，且不需要任何调用方配合：

1. **唯一出口 + 硬限速。**
   所有 HTTP 调用（登录、业务、以及 Code=2 之后的重试）统一走
   :meth:`WhuMeterClient._request`，它在发请求前先向进程级限速器
   （`.rate_limit`）预约一个发车时刻 —— **排队等待，而不是并发硬打**。
   限速器按**主机**共享配额（注册表挂在 `sys` 上，进程内唯一），
   所以"配置流临时客户端 + 每个 entry 的协调器 + 将来新增的调用路径"
   加起来仍然只有 1 次/秒。排队过深时抛 `RateLimitExceeded`（请求根本没发出）。

2. **token 单飞。**
   并发调用不会各自登录一次：`_ensure_token()` 用锁 + 二次检查；
   `_refresh_token()` 用代次（generation）判断"是否已被别人刷新过"，
   避免 N 个并发请求触发 N 次重新登录。

3. **在途合并（in-flight coalescing）。**
   完全相同的请求若已在飞行中，后来者直接复用同一结果，
   不再占用排队名额 —— 连点/重渲染把 N 次请求折叠成 1 次。

结果：服务器侧观测到的相邻请求间隔恒 >= 1 秒（见 whu_meter_mock/ratelimit_verify.py）。
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from datetime import date, timedelta
from typing import Any
from urllib.parse import urlparse

import httpx

from .const import (
    PATH_AREA,
    PATH_ARCH,
    PATH_DAY_VALUE,
    PATH_METER_INFO,
    PATH_RESERVE,
    PATH_ROOM,
    PATH_ROOM_METER,
    PATH_SIGNIN,
    SIGNIN_ACCOUNT,
    SIGNIN_PASSWORD,
    SIGNIN_SYS_ID,
)
from .rate_limit import DEFAULT_MIN_INTERVAL, RateLimitExceeded, ServerRateLimiter, get_limiter

_LOGGER = logging.getLogger(__name__)


class WhuMeterApiError(Exception):
    """Base error for the WHU meter API."""


class WhuMeterAuthError(WhuMeterApiError):
    """Raised when the server rejects our credentials/token."""


class WhuMeterRateLimitError(WhuMeterApiError):
    """排队等待超过上限，请求被有意丢弃（为了不超频，而不是发生故障）。"""


def _md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def _limiter_key(base_url: str) -> str:
    """配额按主机（含端口）共享，而不是按客户端实例。

    这样即使同时存在"配置流客户端 + N 个协调器客户端 + 将来新增的调用路径"，
    它们对同一台服务器也只有一份额度。
    """
    parsed = urlparse(base_url)
    return (parsed.netloc or base_url).lower()


def _consume_exception(task: asyncio.Future) -> None:
    """吞掉被合并任务的异常，避免 asyncio 报 "exception was never retrieved"。"""
    if not task.cancelled():
        task.exception()


class WhuMeterClient:
    """Small async client around the ICBS_V2_Server HTTP API."""

    def __init__(self, base_url: str, http: httpx.AsyncClient) -> None:
        self._http = http
        self.base_url = base_url.rstrip("/")
        self._server = self.base_url + "/ICBS_V2_Server"
        self._token: str | None = None
        self._token_gen = 0
        self._signin_lock: asyncio.Lock | None = None
        self._inflight: dict[Any, asyncio.Future] = {}
        # 进程级、按主机共享的限速器
        self.limiter: ServerRateLimiter = get_limiter(_limiter_key(base_url))

    # ------------------------------------------------------------ 唯一出口

    def _lock(self) -> asyncio.Lock:
        """惰性创建（客户端可能在没有事件循环时被构造）。"""
        if self._signin_lock is None:
            self._signin_lock = asyncio.Lock()
        return self._signin_lock

    async def _request(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 20.0,
    ) -> httpx.Response:
        """**所有** HTTP 请求的唯一出口：先过限速器，再发车。"""
        try:
            waited = await self.limiter.acquire()
        except RateLimitExceeded as err:
            raise WhuMeterRateLimitError(str(err)) from err
        if waited > 0.05:
            _LOGGER.debug("whu_meter: 受 1 次/秒 限制，等待 %.2fs 后请求 %s", waited, url)
        return await self._http.get(url, params=params, headers=headers, timeout=timeout)

    # ------------------------------------------------------------------ core

    async def _do_signin(self) -> None:
        """真实的登录动作（调用方须已持有 _signin_lock 或确认无人并发）。"""
        query = (
            f"p1={SIGNIN_ACCOUNT}"
            f"&p2={_md5(SIGNIN_PASSWORD)}"
            f"&p3={SIGNIN_SYS_ID}"
            f"&t={int(time.time())}"
        )
        url = f"{self._server}{PATH_SIGNIN}?{query}&s={_md5(query)}"
        try:
            resp = await self._request(url, timeout=20)
            resp.raise_for_status()
            payload = resp.json()
        except (httpx.HTTPError, ValueError) as err:
            raise WhuMeterApiError(f"登录请求失败: {err}") from err

        data = payload.get("Data") or {}
        token = data.get("access_Jwt")
        if payload.get("Code") != 0 or not token:
            raise WhuMeterAuthError(f"登录失败: {payload.get('Mess') or payload}")
        self._token = token
        self._token_gen += 1

    async def _ensure_token(self) -> None:
        """首次登录；并发调用只会产生一次真实登录。"""
        if self._token is not None:
            return
        async with self._lock():
            if self._token is None:          # 二次检查：等锁期间可能已被刷新
                await self._do_signin()

    async def _refresh_token(self, used_gen: int) -> bool:
        """token 被服务端拒绝时重新登录。返回是否真的重登了。"""
        async with self._lock():
            if self._token_gen != used_gen:
                return False                 # 别人已经换过 token，直接用新的
            await self._do_signin()
            return True

    async def _request_json(
        self, path: str, params: dict[str, Any] | None
    ) -> dict[str, Any]:
        url = f"{self._server}{path}"
        headers = {"Authorization": f"Bearer {self._token}"}
        try:
            resp = await self._request(url, params=params, headers=headers, timeout=25)
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPError, ValueError) as err:
            raise WhuMeterApiError(f"请求 {path} 失败: {err}") from err

    async def _get_impl(self, path: str, params: dict[str, Any] | None) -> dict[str, Any]:
        await self._ensure_token()
        gen = self._token_gen
        payload = await self._request_json(path, params)

        if payload.get("Code") == 2:  # "未授权" -> token 可能过期，重试一次
            refreshed = await self._refresh_token(gen)
            _LOGGER.debug(
                "Token rejected for %s (%s), retrying once",
                path, "re-signed in" if refreshed else "refreshed by a concurrent call",
            )
            payload = await self._request_json(path, params)

        if payload.get("Code") != 0:
            raise WhuMeterApiError(f"{path} 返回错误: {payload.get('Mess') or payload}")
        return payload.get("Data") or {}

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """带"在途合并"的 GET：完全相同的并发请求只打服务器一次。"""
        key = (path, tuple(sorted((params or {}).items())))
        task = self._inflight.get(key)
        owner = task is None
        if owner:
            task = asyncio.ensure_future(self._get_impl(path, params))
            task.add_done_callback(_consume_exception)
            self._inflight[key] = task
        else:
            _LOGGER.debug("whu_meter: 相同请求已在途，复用之 (%s)", path)
        try:
            # shield：某个等待者被取消（如前端断开）不应连带取消共享任务
            return await asyncio.shield(task)
        finally:
            if owner:
                self._inflight.pop(key, None)

    # ------------------------------------------------------ discovery chain

    async def areas(self) -> list[dict[str, Any]]:
        data = await self._get(PATH_AREA)
        return data.get("areaInfoList") or []

    async def architectures(self, area_id: str) -> list[dict[str, Any]]:
        data = await self._get(PATH_ARCH, {"AreaID": area_id})
        return data.get("architectureInfoList") or []

    async def rooms(self, architecture_id: str, floor: int) -> list[dict[str, Any]]:
        data = await self._get(PATH_ROOM, {"ArchitectureID": architecture_id, "Floor": floor})
        return data.get("roomInfoList") or []

    async def room_meters(self, room_no: str) -> list[dict[str, Any]]:
        data = await self._get(PATH_ROOM_METER, {"RoomID": room_no})
        return data.get("meterList") or []

    async def meter_info(self, meter_id: str) -> dict[str, Any]:
        return await self._get(PATH_METER_INFO, {"MeterID": meter_id})

    # ------------------------------------------------------------- readings

    async def reserve(self, meter_id: str) -> dict[str, Any]:
        """Balance / state / last read time for a meter."""
        return await self._get(PATH_RESERVE, {"MeterID": meter_id})

    async def day_values(
        self, meter_id: str, start: date, end: date
    ) -> list[dict[str, Any]]:
        """Daily usage points (kWh) between start and end, oldest first."""
        data = await self._get(
            PATH_DAY_VALUE,
            {"MeterID": meter_id, "startDate": start.isoformat(), "endDate": end.isoformat()},
        )
        values = data.get("DayValues") or []
        values.sort(key=lambda v: str(v.get("curDayTime")))
        return values

    async def recent_days(self, meter_id: str, days: int = 8) -> list[dict[str, Any]]:
        end = date.today()
        start = end - timedelta(days=days)
        return await self.day_values(meter_id, start, end)

    # ------------------------------------------------------------- diagnostics

    def limiter_stats(self) -> dict[str, Any]:
        """当前限速器计数（诊断用；不产生任何网络请求）。"""
        snapshot = self.limiter.snapshot()
        snapshot.setdefault("min_interval_s", DEFAULT_MIN_INTERVAL)
        snapshot["inflight"] = len(self._inflight)
        snapshot["token_gen"] = self._token_gen
        return snapshot
