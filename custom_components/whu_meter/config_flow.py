"""Config flow: pick the web root, then drill down area -> building -> floor -> room.

=============================================================================
2026-10-11 HARDENING — 为什么必须"渲染时零网络 I/O"
=============================================================================
Home Assistant 并不只在用户点击"提交"时才调用步骤处理器。它在**每次读取流程
状态**时都会重新执行当前步骤：

    GET /api/config/config_entries/flow/{flow_id}
      -> FlowManagerResourceView.get()
      -> FlowManager.async_configure(flow_id)          # user_input=None
      -> 重新执行 flow.cur_step 对应的 async_step_*()

（源码：homeassistant/helpers/data_entry_flow.py::FlowManagerResourceView.get）

前端在下列时机都会发这个 GET：打开/重新打开集成弹窗、刷新页面、
WebSocket 重连（HA 重启、网络抖动）、以及 flow 被重新挂载。

旧版本在 `async_step_area / building / floor / room` 的**函数开头**（即渲染分支
里）直接 `await self.client.areas()` 等，于是：
  * 每一次渲染 = 一次真实的 HTTP 请求；
  * `_ensure_client()` 每次都新建 WhuMeterClient，token 不复用 => 每次还多一次登录；
  * `async_step_floor` 还把 `architectures()` 又拉了一遍（同一接口重复请求）；
  * 没有任何 in-flight / 去重 / 限速，连点"提交"会并发地全量打到服务器。

叠加武大服务器对失败连接是**立即 RST**（毫秒级失败，不是超时），一次"提交"
瞬间就能返回错误，用户会条件反射地连点 => 每次提交 2 个请求
（登录 + 业务），很容易在 1 秒内堆到 ~15 次。

本版本保证：
  1. 每个资源在每个 flow 实例内**最多请求一次**（缓存到 self）；
  2. **重渲染（GET）永不触网**：网络调用只发生在"提交"分支里；
  3. 并发/连点被 `asyncio.Lock` + 防抖窗口折叠，不会并发打服务器。

=============================================================================
2026-10-12 修正：缓存必须按"请求参数"作键
=============================================================================
上面的缓存最初写成"整个 flow 只存一份"，这引入了三处**功能回归**
（用户改选区域/楼层后会拿到上一次的数据）：

  * `_archs` 单份 -> 改选**区域**后仍显示旧区域下的楼栋；
  * `_rooms` 单份 -> 改选**楼层**后仍显示旧楼层的房间；
  * `_areas is None` 时表单只能空列表（流程被外部重建时不可恢复）。

现在改为 `_archs_by_area[area_id]` / `_rooms_by_key[(arch_id, floor)]`：
**同样的参数只请求一次，参数变了就重新请求** —— 既保留原版语义，又不会重复打服务器。

=============================================================================
2026-10-12 限流：服务器侧硬上限 1 次/秒
=============================================================================
真正的硬约束不在本文件，而在 `api.py` 的唯一下行通道 + `rate_limit.py` 的
进程级限速器（按主机共享配额，进程内所有调用路径合计 1 次/秒）。本文件只做两件事：
  * 让重渲染/连点**根本不产生请求**（缓存 + 锁 + 失败重试防抖）；
  * 把限速器的拒绝（排队过深）翻译成一条清晰的用户提示，而不是"未知错误"。

=============================================================================
2026-10-12 生产地址确认门：真实流量必须由人在界面上显式授权
=============================================================================
`is_restricted_base_url()`（见 `guard.py`）把地址分成两类：本机/局域网，和
**外部/生产服务器**。对后者，本文件插了一步 `async_step_confirm_real_server`：

    step user（提交地址）
      ├─ 本地地址 ────────────────→ 直接 _probe()（登录 + GetAreaInfo）
      └─ 外部/生产地址 ─→ step confirm_real_server（**不触网**）
                            ├─ 未勾选确认 → 报 confirm_required，请求数仍为 0
                            └─ 已勾选确认 → self._confirmed_real_url = 地址
                                            → _probe() → step area …

设计要点：
  * **确认步是纯本地的**：它只渲染一个勾选框，绝不在渲染分支里发请求；
  * **未确认前零请求**：这是本机制的硬保证 —— "对外真实流量只能由人手发起"
    不是靠自觉，而是代码路径上根本不存在；
  * 确认是**按地址**授予的：换成另一个受限地址会重新询问；
  * 探测失败时回到 step user（并保留已填地址），避免用户卡在确认页无法改地址；
  * 连点/重渲染依旧零副作用（确认步不触网、`_probe` 自带去重）。
=============================================================================
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.httpx_client import get_async_client
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
)

from .api import (
    WhuMeterApiError,
    WhuMeterAuthError,
    WhuMeterClient,
    WhuMeterRateLimitError,
)
from .const import (
    CONF_AREA_ID,
    CONF_AREA_NAME,
    CONF_ARCHITECTURE_ID,
    CONF_ARCHITECTURE_NAME,
    CONF_BASE_URL,
    CONF_CONFIRM_REAL_SERVER,
    CONF_FLOOR,
    CONF_METER_ADDRESS,
    CONF_METER_ID,
    CONF_ROOM_NAME,
    CONF_ROOM_NO,
    DEFAULT_BASE_URL,
    DEFAULT_EMAIL_NOTIFY_ENTITY,
    DOMAIN,
)
from .guard import is_restricted_base_url

_LOGGER = logging.getLogger(__name__)

# 同一次"探测"失败后，至少间隔这么多秒才允许再次探测（防连点）。
# 成功探测过同一 base_url 的重复提交会直接命中缓存，不受此限制。
# 取值与服务器侧 1 次/秒 的硬限制对齐。
MIN_PROBE_INTERVAL = 1.0


class _ProbeDebounced(WhuMeterApiError):
    """距上次失败的探测太近，被本地防抖拦下（没有发出任何请求）。"""


def _select(options: list[tuple[str, str]], current: Any = None) -> SelectSelector:
    return SelectSelector(
        SelectSelectorConfig(
            options=[SelectOptionDict(value=v, label=label) for v, label in options],
            mode=SelectSelectorMode.DROPDOWN,
        )
    )


class WhuMeterConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the WHU meter config flow."""

    VERSION = 1

    def __init__(self) -> None:
        self.client: WhuMeterClient | None = None
        self.base_url: str = DEFAULT_BASE_URL
        self.area_id: str = ""
        self.area_name: str = ""
        self.architecture_id: str = ""
        self.architecture_name: str = ""
        self.floor: int = 1
        self.room_no: str = ""
        self.room_name: str = ""
        self.meters: list[dict[str, Any]] = []

        # --- 缓存：同样的参数只请求一次（按请求参数作键，改选即失效）---
        self._areas: list[dict[str, Any]] | None = None
        self._archs_by_area: dict[str, list[dict[str, Any]]] = {}
        self._rooms_by_key: dict[tuple[str, int], list[dict[str, Any]]] = {}

        # --- 并发保护 ---
        self._probe_lock = asyncio.Lock()
        self._last_probe_at: float = 0.0
        self._probed_base_url: str | None = None

        # --- 生产地址确认门 ---
        # _pending_base_url：等待用户在确认步上授权的地址（尚未发过任何请求）
        # _confirmed_real_url：本 flow 内已被用户显式确认过的受限地址
        self._pending_base_url: str = ""
        self._confirmed_real_url: str | None = None

    # ------------------------------------------------------------- internals

    def _ensure_client(self, base_url: str) -> WhuMeterClient:
        """复用客户端；只有 base_url 变了才重建（重建时清空缓存）。"""
        if self.client is None or self._client_base_url != base_url:
            self.client = WhuMeterClient(base_url, get_async_client(self.hass))
            self._client_base_url = base_url
            self._areas = None
            self._archs_by_area = {}
            self._rooms_by_key = {}
            self._probed_base_url = None
        return self.client

    async def _probe(self, base_url: str) -> None:
        """登录 + 拉一次区域列表。

        每个 base_url 只成功执行一次；并用锁 + 防抖窗口折叠并发/连点提交。
        注意：只有"提交"分支会调用它 —— 渲染分支绝不调用。
        """
        if self._probed_base_url == base_url and self._areas is not None:
            return  # 已经探测过，直接用缓存（重复提交/连点走这里，零请求）

        async with self._probe_lock:
            if self._probed_base_url == base_url and self._areas is not None:
                return
            now = time.monotonic()
            if now - self._last_probe_at < MIN_PROBE_INTERVAL:
                # 距离上次**失败**太近，直接拒绝，不打服务器
                raise _ProbeDebounced(
                    f"请求过于频繁，请 {MIN_PROBE_INTERVAL:.0f} 秒后再试"
                )
            self._last_probe_at = now

            client = self._ensure_client(base_url)
            self._areas = await client.areas()  # 登录 + GetAreaInfo（各 1 次）
            self._probed_base_url = base_url

    async def _fetch_archs(self, area_id: str) -> list[dict[str, Any]]:
        """按区域缓存：同一区域只请求一次；换了区域必然重新请求。"""
        cached = self._archs_by_area.get(area_id)
        if cached is None:
            cached = await self.client.architectures(area_id)
            self._archs_by_area[area_id] = cached
        return cached

    async def _fetch_rooms(self, architecture_id: str, floor: int) -> list[dict[str, Any]]:
        """按 (楼栋, 楼层) 缓存：改了楼层必然重新请求。"""
        key = (architecture_id, floor)
        cached = self._rooms_by_key.get(key)
        if cached is None:
            cached = await self.client.rooms(architecture_id, floor)
            self._rooms_by_key[key] = cached
        return cached

    def _user_schema(self) -> vol.Schema:
        """第一步表单。默认值跟随 self.base_url，探测失败退回时可保留用户已填内容。"""
        return vol.Schema(
            {
                vol.Required(
                    CONF_BASE_URL, default=self.base_url or DEFAULT_BASE_URL
                ): TextSelector(),
            }
        )

    async def _try_probe(self, base_url: str):
        """探测并推进到区域步。

        返回 ``(errors, result)``：``result`` 非 None 表示流程已推进，调用方直接
        把它返回给 HA；否则 ``errors`` 交给 ``async_show_form`` 渲染。

        这样 `async_step_user` 与 `async_step_confirm_real_server` 共用同一套
        错误映射，不必各写一份 try/except。
        """
        try:
            if is_restricted_base_url(base_url):
                _LOGGER.info(
                    "whu_meter: 已获用户确认，开始对外部服务器 %s 发起真实请求", base_url
                )
            await self._probe(base_url)
        except _ProbeDebounced as err:
            _LOGGER.debug("whu_meter 探测被本地防抖拦下: %s", err)
            return {"base": "rate_limited"}, None
        except WhuMeterRateLimitError as err:
            _LOGGER.warning("whu_meter 探测被服务器限速拒绝 %s: %s", base_url, err)
            return {"base": "rate_limited"}, None
        except (WhuMeterApiError, WhuMeterAuthError) as err:
            _LOGGER.warning("whu_meter 探测失败 %s: %s", base_url, err)
            return {"base": "cannot_connect"}, None
        except (httpx.HTTPError, asyncio.TimeoutError, OSError, ValueError) as err:
            # 兜底：不让意外异常冒泡造成前端 "Unknown error"
            _LOGGER.warning("whu_meter 探测异常 %s: %r", base_url, err, exc_info=True)
            return {"base": "cannot_connect"}, None
        return {}, await self.async_step_area()

    # ------------------------------------------------------------- flow steps

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        schema = self._user_schema()
        if user_input is not None:  # ← 唯一触网分支（且受下面的确认门约束）
            base_url = user_input[CONF_BASE_URL].strip().rstrip("/")
            self.base_url = base_url  # 先记住，退回本步时表单能带回用户已填的地址

            # ---- 生产地址确认门：未在界面上勾选确认前，一个请求都不发 ----
            if is_restricted_base_url(base_url) and self._confirmed_real_url != base_url:
                _LOGGER.warning(
                    "whu_meter: %s 被判定为外部/生产服务器；已暂停，"
                    "等待用户在确认步骤中显式授权（当前未发出任何请求）",
                    base_url,
                )
                self._pending_base_url = base_url
                return await self.async_step_confirm_real_server()

            errors, result = await self._try_probe(base_url)
            if result is not None:
                return result
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_confirm_real_server(
        self, user_input: dict[str, Any] | None = None
    ):
        """外部/生产地址的警告确认步骤。**不触网**（渲染与校验都不发请求）。

        只有用户勾选确认后才会走到 `_try_probe`：只要这一步没被确认，
        针对该地址的服务器请求数就是 0。
        """
        errors: dict[str, str] = {}
        base_url = self._pending_base_url or self.base_url

        if user_input is not None:
            if not user_input.get(CONF_CONFIRM_REAL_SERVER):
                _LOGGER.info("whu_meter: 用户未确认，未对 %s 发起任何请求", base_url)
                errors["base"] = "confirm_required"
            else:
                self._confirmed_real_url = base_url
                self._pending_base_url = ""
                errors, result = await self._try_probe(base_url)
                if result is not None:
                    return result
                # 探测失败：退回第一步，让用户能改地址（例如换回本机 mock），
                # 而不是卡在确认页上反复点。
                self.base_url = base_url
                return self.async_show_form(
                    step_id="user", data_schema=self._user_schema(), errors=errors
                )

        # vol.Optional + default：任何载荷（含缺字段）都能到达本函数，
        # 从而给出可读的 confirm_required，而不是流程管理器层的裸 schema 400。
        schema = vol.Schema(
            {
                vol.Optional(CONF_CONFIRM_REAL_SERVER, default=False): BooleanSelector(),
            }
        )
        return self.async_show_form(
            step_id="confirm_real_server",
            data_schema=schema,
            errors=errors,
            description_placeholders={"base_url": base_url},
        )

    async def async_step_area(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        if self._areas is None:
            # 理论上不可达（渲染分支不触网，缓存只可能由 user 步填充）。
            # 若流程被外部重建导致缓存丢失，给一句可操作的提示而不是空列表。
            _LOGGER.warning("whu_meter: 区域缓存缺失（流程可能已被重建），请重新开始配置")
        areas = self._areas or []          # ← 读缓存，不触网
        options = [(a["AreaID"], a["AreaName"]) for a in areas if a.get("AreaID")]

        if user_input is not None:
            self.area_id = user_input[CONF_AREA_ID]
            self.area_name = next((lbl for v, lbl in options if v == self.area_id), self.area_id)
            try:
                await self._fetch_archs(self.area_id)
            except WhuMeterRateLimitError as err:
                _LOGGER.warning("whu_meter 获取楼栋被限速拒绝: %s", err)
                errors["base"] = "rate_limited"
            except WhuMeterApiError as err:
                _LOGGER.warning("whu_meter 获取楼栋失败: %s", err)
                errors["base"] = "cannot_connect"
            else:
                return await self.async_step_building()

        if not options:
            errors.setdefault("base", "cannot_connect")
        schema = vol.Schema({vol.Required(CONF_AREA_ID): _select(options)})
        return self.async_show_form(step_id="area", data_schema=schema, errors=errors)

    async def async_step_building(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        # ← 只读"当前区域"的缓存，不触网；改选区域会拿到该区域自己的楼栋
        archs = self._archs_by_area.get(self.area_id) or []
        options = [(a["ArchitectureID"], a["ArchitectureName"]) for a in archs if a.get("ArchitectureID")]

        if user_input is not None:
            self.architecture_id = user_input[CONF_ARCHITECTURE_ID]
            self.architecture_name = next(
                (lbl for v, lbl in options if v == self.architecture_id), self.architecture_id
            )
            return await self.async_step_floor()

        if not options:
            errors.setdefault("base", "cannot_connect")
        schema = vol.Schema({vol.Required(CONF_ARCHITECTURE_ID): _select(options)})
        return self.async_show_form(step_id="building", data_schema=schema, errors=errors)

    async def async_step_floor(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        storys = 7
        for a in (self._archs_by_area.get(self.area_id) or []):
            # ← 复用 building 步已缓存的楼栋列表，不再重复请求
            if a.get("ArchitectureID") == self.architecture_id:
                storys = int(a.get("ArchitectureStorys") or 7)
                break

        if user_input is not None:
            self.floor = int(user_input[CONF_FLOOR])
            try:
                await self._fetch_rooms(self.architecture_id, self.floor)
            except WhuMeterRateLimitError as err:
                _LOGGER.warning("whu_meter 获取房间被限速拒绝: %s", err)
                errors["base"] = "rate_limited"
            except WhuMeterApiError as err:
                _LOGGER.warning("whu_meter 获取房间失败: %s", err)
                errors["base"] = "cannot_connect"
            else:
                return await self.async_step_room()

        schema = vol.Schema(
            {
                vol.Required(CONF_FLOOR, default=1): NumberSelector(
                    NumberSelectorConfig(min=1, max=max(1, storys), step=1, mode="box")
                )
            }
        )
        return self.async_show_form(step_id="floor", data_schema=schema, errors=errors)

    async def async_step_room(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        # ← 只读"当前 (楼栋, 楼层)"的缓存，不触网；改了楼层会拿到新楼层的房间
        rooms = self._rooms_by_key.get((self.architecture_id, self.floor)) or []
        options = [(r["RoomNo"], r.get("RoomName") or r["RoomNo"]) for r in rooms if r.get("RoomNo")]

        if user_input is not None:
            self.room_no = user_input[CONF_ROOM_NO]
            room = next((r for r in rooms if r["RoomNo"] == self.room_no), None)
            self.room_name = (room or {}).get("RoomEntierName") or self.room_no
            try:
                self.meters = await self.client.room_meters(self.room_no)   # 1 次（用户动作）
            except WhuMeterRateLimitError as err:
                _LOGGER.warning("whu_meter 获取表具被限速拒绝: %s", err)
                errors["base"] = "rate_limited"
            except WhuMeterApiError as err:
                _LOGGER.warning("whu_meter 获取表具失败: %s", err)
                errors["base"] = "cannot_connect"
            else:
                if not self.meters:
                    errors["base"] = "no_meter"
                else:
                    return await self.async_step_meter()

        if not options:
            errors.setdefault("base", "cannot_connect")
        schema = vol.Schema({vol.Required(CONF_ROOM_NO): _select(options)})
        return self.async_show_form(step_id="room", data_schema=schema, errors=errors)

    async def async_step_meter(self, user_input: dict[str, Any] | None = None):
        """纯本地步骤：无任何网络 I/O。"""
        meter_options = [
            (m.get("meterId"), f"{m.get('meterType', '电表')} {m.get('meter_address', '')}")
            for m in self.meters
            if m.get("meterId")
        ]
        if user_input is None:
            if len(meter_options) == 1:
                user_input = {CONF_METER_ID: meter_options[0][0]}
            else:
                schema = vol.Schema({vol.Required(CONF_METER_ID): _select(meter_options)})
                return self.async_show_form(step_id="meter", data_schema=schema)

        meter_id = user_input[CONF_METER_ID]
        meter = next((m for m in self.meters if m.get("meterId") == meter_id), {})
        await self.async_set_unique_id(f"whu_meter_{meter_id}")
        self._abort_if_unique_id_configured()

        return self.async_create_entry(
            title=self.room_name,
            data={
                CONF_BASE_URL: self.base_url,
                CONF_AREA_ID: self.area_id,
                CONF_AREA_NAME: self.area_name,
                CONF_ARCHITECTURE_ID: self.architecture_id,
                CONF_ARCHITECTURE_NAME: self.architecture_name,
                CONF_FLOOR: self.floor,
                CONF_ROOM_NO: self.room_no,
                CONF_ROOM_NAME: self.room_name,
                CONF_METER_ID: meter_id,
                CONF_METER_ADDRESS: meter.get("meter_address", ""),
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry):
        return WhuMeterOptionsFlow(config_entry)


class WhuMeterOptionsFlow(config_entries.OptionsFlow):
    """Options: scan interval and low-balance threshold."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self.entry = config_entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        schema = vol.Schema(
            {
                vol.Required(
                    "scan_interval_minutes",
                    default=self.entry.options.get("scan_interval_minutes", 15),
                ): NumberSelector(NumberSelectorConfig(min=5, max=1440, step=1, mode="box")),
                vol.Required(
                    "balance_warn_threshold",
                    default=self.entry.options.get("balance_warn_threshold", 20.0),
                ): NumberSelector(NumberSelectorConfig(min=0, max=1000, step=0.5, mode="box")),
                vol.Required(
                    "email_alert_enabled",
                    default=self.entry.options.get("email_alert_enabled", True),
                ): BooleanSelector(),
                vol.Optional(
                    "email_notify_entity",
                    description={"suggested_value": self.entry.options.get(
                        "email_notify_entity", DEFAULT_EMAIL_NOTIFY_ENTITY)},
                ): EntitySelector(EntitySelectorConfig(domain="notify")),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
