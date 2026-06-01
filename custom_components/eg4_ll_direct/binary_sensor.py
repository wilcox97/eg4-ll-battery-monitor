"""Binary sensor platform for the EG4 LL Direct integration."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_ADDRESS, DEFAULT_NAME, DOMAIN
from .coordinator import EG4LLCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: EG4LLCoordinator = hass.data[DOMAIN][entry.entry_id]
    address = entry.data[CONF_ADDRESS]
    async_add_entities([
        EG4LLLowBatterySensor(coordinator, address),
        EG4LLChargingSensor(coordinator, address),
        EG4LLDischargingSensor(coordinator, address),
    ])


class _EG4LLBaseBinarySensor(CoordinatorEntity[EG4LLCoordinator], BinarySensorEntity):
    """Base class with shared device info."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: EG4LLCoordinator, address: str) -> None:
        super().__init__(coordinator)
        self._address = address
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, address)},
            name=f"{DEFAULT_NAME} ({address[-5:].replace(':', '')})",
            manufacturer="EG4 Electronics",
            model="LL 400Ah",
            configuration_url="https://eg4electronics.com/",
        )


class EG4LLLowBatterySensor(_EG4LLBaseBinarySensor):
    """True when SOC drops below the configured low-battery warning threshold.

    Threshold is configurable via Settings -> Integrations -> EG4 LL -> Configure.
    Default is 20%. Use this to trigger automations (notifications, load shedding,
    generator start, etc.) without having to write SOC comparisons in templates.
    """

    _attr_device_class = BinarySensorDeviceClass.BATTERY
    _attr_name = "Low Battery"

    def __init__(self, coordinator: EG4LLCoordinator, address: str) -> None:
        super().__init__(coordinator, address)
        self._attr_unique_id = f"{address}_low_battery"

    @property
    def is_on(self) -> bool | None:
        data = self.coordinator.data
        if data is None:
            return None
        return data.battery_level <= self.coordinator.low_soc_warn_pct

    @property
    def extra_state_attributes(self) -> dict:
        return {
            "threshold_pct": self.coordinator.low_soc_warn_pct,
            "current_soc_pct": self.coordinator.data.battery_level if self.coordinator.data else None,
        }


class EG4LLChargingSensor(_EG4LLBaseBinarySensor):
    """True when the battery is actively receiving charge current."""

    _attr_device_class = BinarySensorDeviceClass.BATTERY_CHARGING
    _attr_name = "Charging"

    def __init__(self, coordinator: EG4LLCoordinator, address: str) -> None:
        super().__init__(coordinator, address)
        self._attr_unique_id = f"{address}_charging"

    @property
    def is_on(self) -> bool | None:
        data = self.coordinator.data
        if data is None:
            return None
        return data.current > 0.1


class EG4LLDischargingSensor(_EG4LLBaseBinarySensor):
    """True when the battery is actively supplying power (discharging)."""

    _attr_name = "Discharging"
    _attr_icon = "mdi:battery-minus"

    def __init__(self, coordinator: EG4LLCoordinator, address: str) -> None:
        super().__init__(coordinator, address)
        self._attr_unique_id = f"{address}_discharging"

    @property
    def is_on(self) -> bool | None:
        data = self.coordinator.data
        if data is None:
            return None
        return data.current < -0.1
