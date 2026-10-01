"""The WHU Electricity Meter integration."""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.httpx_client import get_async_client
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import WhuMeterApiError, WhuMeterClient
from .const import (
    CONF_BALANCE_WARN,
    CONF_METER_ID,
    CONF_ROOM_NAME,
    CONF_SCAN_INTERVAL,
    DEFAULT_BALANCE_WARN,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up WHU Electricity Meter from a config entry."""
    client = WhuMeterClient(entry.data["base_url"], get_async_client(hass))

    scan_minutes = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
    coordinator = WhuMeterCoordinator(hass, client, entry, timedelta(minutes=scan_minutes))
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))

    # Keep the low-balance email alert automation in sync with options.
    # Deferred import to avoid a circular import with .sensor.
    from .automation import async_sync_alert_automation

    try:
        await async_sync_alert_automation(hass, entry)
    except Exception:  # noqa: BLE001 - automation sync must never break setup
        _LOGGER.exception("whu_meter: 同步邮件警报自动化失败")

    return True


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove the owned automation when the config entry is deleted."""
    from .automation import async_sync_alert_automation

    try:
        # Force-disable: removes the automation from automations.yaml.
        class _DisabledEntry:
            options = {"email_alert_enabled": False}

        await async_sync_alert_automation(hass, _DisabledEntry())
    except Exception:  # noqa: BLE001
        _LOGGER.exception("whu_meter: 移除邮件警报自动化失败")


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return unload_ok


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when options change."""
    await hass.config_entries.async_reload(entry.entry_id)


class WhuMeterCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Fetch meter data from the WHU platform on a schedule."""

    def __init__(self, hass, client: WhuMeterClient, entry: ConfigEntry, interval: timedelta) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=interval)
        self.client = client
        self.entry = entry

    async def _async_update_data(self) -> dict[str, Any]:
        """Login + pull reserve and recent day values for the configured meter."""
        meter_id = self.entry.data[CONF_METER_ID]
        try:
            reserve = await self.client.reserve(meter_id)
            days = await self.client.recent_days(meter_id, days=8)
        except WhuMeterApiError as err:
            raise UpdateFailed(f"抓取水电数据失败: {err}") from err

        today = reserve.get("LastQueryDate", "")
        latest_by_day: dict[str, dict[str, Any]] = {}
        for point in days:
            latest_by_day[str(point.get("curDayTime"))] = point

        data: dict[str, Any] = {
            "reserve": reserve,
            "days": latest_by_day,
            "balance_warn": DEFAULT_BALANCE_WARN,
        }
        data["balance_warn"] = self.entry.options.get(CONF_BALANCE_WARN, DEFAULT_BALANCE_WARN)

        _LOGGER.debug(
            "Updated %s: balance=%s, readTime=%s",
            self.entry.data.get(CONF_ROOM_NAME),
            reserve.get("remainPower"),
            today,
        )
        return data
