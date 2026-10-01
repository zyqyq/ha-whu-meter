"""Config flow: pick the web root, then drill down area -> building -> floor -> room."""
from __future__ import annotations

import logging
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

from .api import WhuMeterApiError, WhuMeterAuthError, WhuMeterClient
from .const import (
    CONF_AREA_ID,
    CONF_AREA_NAME,
    CONF_ARCHITECTURE_ID,
    CONF_ARCHITECTURE_NAME,
    CONF_BASE_URL,
    CONF_FLOOR,
    CONF_METER_ADDRESS,
    CONF_METER_ID,
    CONF_ROOM_NAME,
    CONF_ROOM_NO,
    DEFAULT_BASE_URL,
    DEFAULT_EMAIL_NOTIFY_ENTITY,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


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

    async def _ensure_client(self, base_url: str) -> WhuMeterClient:
        self.client = WhuMeterClient(base_url, get_async_client(self.hass))
        await self.client.areas()  # probe: login + area list
        return self.client

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        schema = vol.Schema(
            {
                vol.Required(CONF_BASE_URL, default=DEFAULT_BASE_URL): TextSelector(),
            }
        )
        if user_input is not None:
            base_url = user_input[CONF_BASE_URL].strip().rstrip("/")
            try:
                self.base_url = base_url
                await self._ensure_client(base_url)
                return await self.async_step_area()
            except (WhuMeterApiError, WhuMeterAuthError, httpx.HTTPError) as err:
                _LOGGER.warning("Probe failed for %s: %s", base_url, err)
                errors["base"] = "cannot_connect"
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_area(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        try:
            areas = await self.client.areas()
        except WhuMeterApiError:
            areas = []
        options = [(a["AreaID"], a["AreaName"]) for a in areas if a.get("AreaID")]

        if user_input is not None:
            self.area_id = user_input[CONF_AREA_ID]
            self.area_name = next((lbl for v, lbl in options if v == self.area_id), self.area_id)
            return await self.async_step_building()

        schema = vol.Schema({vol.Required(CONF_AREA_ID): _select(options)})
        return self.async_show_form(step_id="area", data_schema=schema, errors=errors)

    async def async_step_building(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        try:
            archs = await self.client.architectures(self.area_id)
        except WhuMeterApiError:
            archs = []
        options = [(a["ArchitectureID"], a["ArchitectureName"]) for a in archs if a.get("ArchitectureID")]

        if user_input is not None:
            self.architecture_id = user_input[CONF_ARCHITECTURE_ID]
            self.architecture_name = next(
                (lbl for v, lbl in options if v == self.architecture_id), self.architecture_id
            )
            return await self.async_step_floor()

        schema = vol.Schema({vol.Required(CONF_ARCHITECTURE_ID): _select(options)})
        return self.async_show_form(step_id="building", data_schema=schema, errors=errors)

    async def async_step_floor(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        storys = 7
        try:
            archs = await self.client.architectures(self.area_id)
            for a in archs:
                if a.get("ArchitectureID") == self.architecture_id:
                    storys = int(a.get("ArchitectureStorys") or 7)
                    break
        except WhuMeterApiError:
            pass

        if user_input is not None:
            self.floor = int(user_input[CONF_FLOOR])
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
        try:
            rooms = await self.client.rooms(self.architecture_id, self.floor)
        except WhuMeterApiError:
            rooms = []
        options = [(r["RoomNo"], r.get("RoomName") or r["RoomNo"]) for r in rooms if r.get("RoomNo")]

        if user_input is not None:
            self.room_no = user_input[CONF_ROOM_NO]
            room = next((r for r in rooms if r["RoomNo"] == self.room_no), None)
            self.room_name = (room or {}).get("RoomEntierName") or self.room_no
            self.meters = await self.client.room_meters(self.room_no)
            if not self.meters:
                errors["base"] = "no_meter"
            else:
                return await self.async_step_meter()

        schema = vol.Schema({vol.Required(CONF_ROOM_NO): _select(options)})
        return self.async_show_form(step_id="room", data_schema=schema, errors=errors)

    async def async_step_meter(self, user_input: dict[str, Any] | None = None):
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
