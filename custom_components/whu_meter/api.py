"""API client for the WHU Water & Electricity service platform (ICBS_V2_Server).

Replicates the exact request chain used by the official mobile web app:
  1. SpecialSignIn with credentials baked into the frontend JS
     (p1=account, p2=md5(password), p3=sysid, t=unix seconds, s=md5(query))
     -> returns an access token used as `Authorization: Bearer <token>`.
  2. Business endpoints for areas / buildings / rooms / meters / reserve / day values.
"""
from __future__ import annotations

import hashlib
import logging
import time
from datetime import date, timedelta
from typing import Any

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

_LOGGER = logging.getLogger(__name__)


class WhuMeterApiError(Exception):
    """Base error for the WHU meter API."""


class WhuMeterAuthError(WhuMeterApiError):
    """Raised when the server rejects our credentials/token."""


def _md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


class WhuMeterClient:
    """Small async client around the ICBS_V2_Server HTTP API."""

    def __init__(self, base_url: str, http: httpx.AsyncClient) -> None:
        self._http = http
        self._server = base_url.rstrip("/") + "/ICBS_V2_Server"
        self._token: str | None = None

    # ------------------------------------------------------------------ core

    async def _signin(self) -> None:
        """Obtain a fresh access token (cheap; safe to call every cycle)."""
        query = (
            f"p1={SIGNIN_ACCOUNT}"
            f"&p2={_md5(SIGNIN_PASSWORD)}"
            f"&p3={SIGNIN_SYS_ID}"
            f"&t={int(time.time())}"
        )
        url = f"{self._server}{PATH_SIGNIN}?{query}&s={_md5(query)}"
        try:
            resp = await self._http.get(url, timeout=20)
            resp.raise_for_status()
            payload = resp.json()
        except (httpx.HTTPError, ValueError) as err:
            raise WhuMeterApiError(f"登录请求失败: {err}") from err

        data = payload.get("Data") or {}
        token = data.get("access_Jwt")
        if payload.get("Code") != 0 or not token:
            raise WhuMeterAuthError(f"登录失败: {payload.get('Mess') or payload}")
        self._token = token

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Authenticated GET with a single re-login retry on auth failure."""
        if self._token is None:
            await self._signin()
        assert self._token is not None

        url = f"{self._server}{path}"
        headers = {"Authorization": f"Bearer {self._token}"}
        try:
            resp = await self._http.get(url, params=params, headers=headers, timeout=25)
            resp.raise_for_status()
            payload = resp.json()
        except (httpx.HTTPError, ValueError) as err:
            raise WhuMeterApiError(f"请求 {path} 失败: {err}") from err

        code = payload.get("Code")
        if code == 2:  # "未授权" -> token may have expired, retry once
            _LOGGER.debug("Token rejected for %s, re-signing in", path)
            await self._signin()
            headers = {"Authorization": f"Bearer {self._token}"}
            try:
                resp = await self._http.get(url, params=params, headers=headers, timeout=25)
                resp.raise_for_status()
                payload = resp.json()
            except (httpx.HTTPError, ValueError) as err:
                raise WhuMeterApiError(f"请求 {path} 失败(重试): {err}") from err
            code = payload.get("Code")

        if code != 0:
            raise WhuMeterApiError(f"{path} 返回错误: {payload.get('Mess') or payload}")
        return payload.get("Data") or {}

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
