"""Sensors for the WHU Electricity Meter integration.

The `累计电量` sensor (cumulative register, kWh, total_increasing) is the one
to select inside Home Assistant's Energy dashboard -> Grid consumption.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import logging
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_ARCHITECTURE_NAME,
    CONF_METER_ADDRESS,
    CONF_METER_ID,
    CONF_ROOM_NAME,
    DOMAIN,
    MANUFACTURER,
    MODEL,
)
from . import WhuMeterCoordinator

_LOGGER = logging.getLogger(__name__)


def _stable_unique_id(entry: ConfigEntry, suffix: str) -> str:
    raw = f"whu_meter_{entry.data[CONF_METER_ID]}_{suffix}"
    return raw


class WhuMeterEntity(CoordinatorEntity[WhuMeterCoordinator]):
    """Base entity wired to the coordinator."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: WhuMeterCoordinator, entry: ConfigEntry, suffix: str, name: str) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = _stable_unique_id(entry, suffix)
        self._attr_name = name
        meter_id = entry.data[CONF_METER_ID]
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, meter_id)},
            name=entry.data.get(CONF_ROOM_NAME, meter_id),
            manufacturer=MANUFACTURER,
            model=MODEL,
            suggested_area=entry.data.get(CONF_ARCHITECTURE_NAME),
        )

    @property
    def reserve(self) -> dict[str, Any]:
        return self.coordinator.data.get("reserve", {}) if self.coordinator.data else {}

    @property
    def days(self) -> dict[str, dict[str, Any]]:
        return self.coordinator.data.get("days", {}) if self.coordinator.data else {}

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        reserve = self.reserve
        return {
            "房间": self._entry.data.get(CONF_ROOM_NAME),
            "表具ID": self._entry.data[CONF_METER_ID],
            "表址": self._entry.data.get(CONF_METER_ADDRESS, ""),
            "电表状态": reserve.get("state"),
            "回路": reserve.get("valve"),
            "电价方案": reserve.get("cpricename"),
            "上次抄表": reserve.get("readTime"),
        }


class WhuMeterBalanceSensor(WhuMeterEntity, SensorEntity):
    """账户余额 (CNY)."""

    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.TOTAL
    _attr_icon = "mdi:cash"

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "balance", "账户余额")

    @property
    def native_value(self) -> float | None:
        try:
            return round(float(self.reserve.get("remainPower")), 2)
        except (TypeError, ValueError):
            return None

    @property
    def native_unit_of_measurement(self) -> str:
        return "CNY"

    @property
    def extra_state_attributes(self):
        attrs = super().extra_state_attributes
        attrs["余额单位"] = self.reserve.get("remainName", "元")
        return attrs


class WhuMeterCumulativeSensor(WhuMeterEntity, SensorEntity):
    """累计电量（电表总读数, kWh）—— 用于能源模块."""

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = "kWh"

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "cumulative", "累计电量")

    @property
    def native_value(self) -> float | None:
        try:
            return round(float(self.reserve.get("ZVlaue")), 2)
        except (TypeError, ValueError):
            return None


class WhuMeterYesterdaySensor(WhuMeterEntity, SensorEntity):
    """昨日用量 (kWh, measurement)."""

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = "kWh"
    _attr_icon = "mdi:home-lightning-bolt"

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "yesterday", "昨日用量")

    @property
    def native_value(self) -> float | None:
        from datetime import date, timedelta

        yday = (date.today() - timedelta(days=1)).isoformat()
        point = self.days.get(yday)
        if not point:
            return None
        try:
            return round(float(point.get("dayValue")), 2)
        except (TypeError, ValueError):
            return None

    @property
    def extra_state_attributes(self):
        attrs = super().extra_state_attributes
        from datetime import date, timedelta

        yday = (date.today() - timedelta(days=1)).isoformat()
        point = self.days.get(yday) or {}
        attrs["昨日电费"] = point.get("dayUseMeony")
        attrs["抄表起止"] = f'{point.get("StartReadTime")} ~ {point.get("EndReadTime")}'
        return attrs


class WhuMeterTodaySensor(WhuMeterEntity, SensorEntity):
    """今日用量 (kWh, measurement, partial until settlement)."""

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = "kWh"
    _attr_icon = "mdi:home-lightning-bolt-outline"

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "today", "今日用量")

    @property
    def native_value(self) -> float | None:
        from datetime import date

        point = self.days.get(date.today().isoformat())
        if not point:
            return None
        try:
            return round(float(point.get("dayValue")), 2)
        except (TypeError, ValueError):
            return None


class WhuMeterLastReadSensor(WhuMeterEntity, SensorEntity):
    """上次抄表时间 (timestamp)."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:meter-electric"

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "lastread", "上次抄表时间")

    @property
    def native_value(self) -> datetime | None:
        raw = self.reserve.get("readTime")
        if not raw:
            return None
        try:
            local = datetime.strptime(str(raw), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
        return local.replace(tzinfo=timezone(timedelta(hours=8)))


async def async_setup_entry(
    hass, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: WhuMeterCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            WhuMeterBalanceSensor(coordinator, entry),
            WhuMeterCumulativeSensor(coordinator, entry),
            WhuMeterYesterdaySensor(coordinator, entry),
            WhuMeterTodaySensor(coordinator, entry),
            WhuMeterLastReadSensor(coordinator, entry),
        ]
    )
