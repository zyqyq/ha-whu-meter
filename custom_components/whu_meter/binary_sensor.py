"""Binary sensor: low balance warning."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_METER_ADDRESS, CONF_METER_ID, CONF_ROOM_NAME, DOMAIN
from . import WhuMeterCoordinator
from .sensor import WhuMeterEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: WhuMeterCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([LowBalanceBinarySensor(coordinator, entry)])


class LowBalanceBinarySensor(WhuMeterEntity, BinarySensorEntity):
    """余额低于阈值时置 on（PROBLEM）。"""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_icon = "mdi:battery-alert"

    def __init__(self, coordinator: WhuMeterCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "balance_low", "余额过低")

    @property
    def is_on(self) -> bool:
        try:
            balance = float(self.reserve.get("remainPower"))
        except (TypeError, ValueError):
            return False
        threshold = float(self.coordinator.data.get("balance_warn", 20.0))
        return balance < threshold

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs = super().extra_state_attributes
        attrs["告警阈值"] = self.coordinator.data.get("balance_warn", 20.0)
        return attrs
