"""Sensor platform for the EG4 LL Direct integration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_ADDRESS, CONF_CHARGE_TARGET_PCT, DEFAULT_CHARGE_TARGET_PCT, DEFAULT_NAME, DOMAIN
from .coordinator import EG4LLCoordinator
from .eg4_client import EG4LLSample


@dataclass(frozen=True, kw_only=True)
class EG4LLSensorDescription(SensorEntityDescription):
    """Describes an EG4 LL sensor and how to read it from a sample."""

    value_fn: Callable[[EG4LLSample], float | int | None]


SENSOR_TYPES: tuple[EG4LLSensorDescription, ...] = (
    EG4LLSensorDescription(
        key="voltage",
        name="Pack Voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda s: s.voltage,
    ),
    EG4LLSensorDescription(
        key="current",
        name="Current",
        device_class=SensorDeviceClass.CURRENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda s: s.current,
    ),
    EG4LLSensorDescription(
        key="battery_level",
        name="State of Charge",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda s: s.battery_level,
    ),
    EG4LLSensorDescription(
        key="battery_health",
        name="State of Health",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:heart-pulse",
        value_fn=lambda s: s.battery_health,
    ),
    EG4LLSensorDescription(
        key="cycle_charge",
        name="Capacity Remaining",
        native_unit_of_measurement="Ah",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        icon="mdi:battery-charging-100",
        value_fn=lambda s: s.cycle_charge,
    ),
    EG4LLSensorDescription(
        key="design_capacity",
        name="Design Capacity",
        native_unit_of_measurement="Ah",
        icon="mdi:battery-outline",
        value_fn=lambda s: s.design_capacity,
    ),
    EG4LLSensorDescription(
        key="cycles",
        name="Charge Cycles",
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:battery-sync",
        value_fn=lambda s: s.cycles,
    ),
    EG4LLSensorDescription(
        key="power",
        name="Power",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement="W",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        icon="mdi:lightning-bolt",
        value_fn=lambda s: round(s.voltage * s.current, 1),
    ),
    EG4LLSensorDescription(
        key="temperature",
        name="PCB / MOS Temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda s: s.temperature,
    ),
    EG4LLSensorDescription(
        key="delta_cell_voltage",
        name="Delta Cell Voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=3,
        icon="mdi:battery-alert-variant-outline",
        value_fn=lambda s: (
            round(max(s.cell_voltages) - min(s.cell_voltages), 3)
            if s.cell_voltages
            else None
        ),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up EG4 LL sensors from a config entry."""
    coordinator: EG4LLCoordinator = hass.data[DOMAIN][entry.entry_id]
    address = entry.data[CONF_ADDRESS]

    entities: list[SensorEntity] = [
        EG4LLSensor(coordinator, address, description)
        for description in SENSOR_TYPES
    ]

    # Individual cell voltage sensors. Cell count is only known after the
    # first successful poll, which async_config_entry_first_refresh()
    # guarantees has already happened by the time this runs.
    cell_count = len(coordinator.data.cell_voltages) if coordinator.data else 0
    entities.extend(
        EG4LLCellSensor(coordinator, address, i) for i in range(cell_count)
    )

    # Individual temperature sensors (beyond the main PCB/MOS one above).
    temp_count = len(coordinator.data.temp_values) if coordinator.data else 0
    entities.extend(
        EG4LLTempSensor(coordinator, address, i) for i in range(temp_count)
    )

    # Diagnostic-only sensors: hardware identification and BMS protection
    # limits, both fetched once per connection and exposed as rich attribute
    # sets rather than dozens of individual entities.
    entities.append(EG4LLHardwareInfoSensor(coordinator, address))
    entities.append(EG4LLLimitsSensor(coordinator, address))
    entities.append(EG4LLRSSISensor(coordinator, address))

    charged_sensor = EG4LLEnergySensor(coordinator, address, "charged")
    discharged_sensor = EG4LLEnergySensor(coordinator, address, "discharged")
    entities.extend([charged_sensor, discharged_sensor])

    # Single combined runtime sensor — time to full (charging) or time to
    # empty (discharging), with both values always available as attributes.
    entities.append(EG4LLRuntimeSensor(coordinator, address))

    async_add_entities(entities)


class _EG4LLBaseEntity(CoordinatorEntity[EG4LLCoordinator]):
    """Shared device info for all EG4 LL entities."""

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

    @property
    def _firmware_version(self) -> str | None:
        data = self.coordinator.data
        if data and data.hw_info:
            return data.hw_info.firmware_version
        return None


class EG4LLSensor(_EG4LLBaseEntity, SensorEntity):
    """A single battery-level sensor (voltage, current, SoC, etc.)."""

    entity_description: EG4LLSensorDescription

    def __init__(
        self,
        coordinator: EG4LLCoordinator,
        address: str,
        description: EG4LLSensorDescription,
    ) -> None:
        super().__init__(coordinator, address)
        self.entity_description = description
        self._attr_unique_id = f"{address}_{description.key}"

    @property
    def native_value(self) -> float | int | None:
        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data)


class EG4LLCellSensor(_EG4LLBaseEntity, SensorEntity):
    """Individual cell voltage sensor."""

    _attr_device_class = SensorDeviceClass.VOLTAGE
    _attr_native_unit_of_measurement = UnitOfElectricPotential.VOLT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 3
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: EG4LLCoordinator, address: str, index: int) -> None:
        super().__init__(coordinator, address)
        self._index = index
        self._attr_name = f"Cell {index + 1} Voltage"
        self._attr_unique_id = f"{address}_cell_{index + 1}_voltage"

    @property
    def native_value(self) -> float | None:
        data = self.coordinator.data
        if data is None or self._index >= len(data.cell_voltages):
            return None
        return data.cell_voltages[self._index]


class EG4LLTempSensor(_EG4LLBaseEntity, SensorEntity):
    """Individual cell temperature sensor."""

    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: EG4LLCoordinator, address: str, index: int) -> None:
        super().__init__(coordinator, address)
        self._index = index
        self._attr_name = f"Cell Temp {index + 1}"
        self._attr_unique_id = f"{address}_cell_temp_{index + 1}"

    @property
    def native_value(self) -> int | None:
        data = self.coordinator.data
        if data is None or self._index >= len(data.temp_values):
            return None
        return data.temp_values[self._index]


class EG4LLHardwareInfoSensor(_EG4LLBaseEntity, SensorEntity):
    """Diagnostic sensor exposing model/firmware/serial via attributes.

    The state itself is just the model string for quick glance value; the
    full detail (firmware version, serial) lives in the attributes, the same
    pattern bms_ble uses for cell voltages on its delta-voltage sensor.
    """

    _attr_icon = "mdi:information-outline"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: EG4LLCoordinator, address: str) -> None:
        super().__init__(coordinator, address)
        self._attr_name = "Hardware Info"
        self._attr_unique_id = f"{address}_hardware_info"

    @property
    def native_value(self) -> str | None:
        data = self.coordinator.data
        if data is None or data.hw_info is None:
            return None
        return data.hw_info.model or None

    @property
    def extra_state_attributes(self) -> dict | None:
        data = self.coordinator.data
        if data is None or data.hw_info is None:
            return None
        return {
            "model": data.hw_info.model,
            "firmware_version": data.hw_info.firmware_version,
            "serial": data.hw_info.serial,
        }


class EG4LLLimitsSensor(_EG4LLBaseEntity, SensorEntity):
    """Diagnostic sensor exposing every BMS protection threshold as an
    attribute. These are static config values, not normally visible in any
    vendor app, fetched once per connection.
    """

    _attr_icon = "mdi:shield-alert-outline"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: EG4LLCoordinator, address: str) -> None:
        super().__init__(coordinator, address)
        self._attr_name = "Protection Limits"
        self._attr_unique_id = f"{address}_protection_limits"

    @property
    def native_value(self) -> str | None:
        data = self.coordinator.data
        if data is None or data.limits is None:
            return None
        return "loaded"

    @property
    def extra_state_attributes(self) -> dict | None:
        data = self.coordinator.data
        if data is None or data.limits is None:
            return None
        limits = data.limits
        return {
            "balance_start_voltage": limits.balance_start_v,
            "balance_voltage_diff": limits.balance_diff_v,
            "low_capacity_warning_pct": limits.low_capacity_warn_pct,
            "cell_uv_warn": limits.cell_uv_warn,
            "cell_uv_protect": limits.cell_uv_protect,
            "cell_uv_release": limits.cell_uv_release,
            "cell_ov_warn": limits.cell_ov_warn,
            "cell_ov_protect": limits.cell_ov_protect,
            "cell_ov_release": limits.cell_ov_release,
            "pack_uv_warn": limits.pack_uv_warn,
            "pack_uv_protect": limits.pack_uv_protect,
            "pack_uv_release": limits.pack_uv_release,
            "pack_ov_warn": limits.pack_ov_warn,
            "pack_ov_protect": limits.pack_ov_protect,
            "pack_ov_release": limits.pack_ov_release,
            "charge_under_temp_warn_c": limits.charge_ut_warn_c,
            "charge_under_temp_protect_c": limits.charge_ut_protect_c,
            "charge_under_temp_release_c": limits.charge_ut_release_c,
            "charge_over_temp_warn_c": limits.charge_ot_warn_c,
            "charge_over_temp_protect_c": limits.charge_ot_protect_c,
            "charge_over_temp_release_c": limits.charge_ot_release_c,
            "discharge_under_temp_warn_c": limits.discharge_ut_warn_c,
            "discharge_under_temp_protect_c": limits.discharge_ut_protect_c,
            "discharge_under_temp_release_c": limits.discharge_ut_release_c,
            "discharge_over_temp_warn_c": limits.discharge_ot_warn_c,
            "discharge_over_temp_protect_c": limits.discharge_ot_protect_c,
            "discharge_over_temp_release_c": limits.discharge_ot_release_c,
            "pcb_over_temp_warn_c": limits.pcb_ot_warn_c,
            "pcb_over_temp_protect_c": limits.pcb_ot_protect_c,
            "charge_oc1_protect_a": limits.charge_oc1_protect_a,
            "charge_oc1_delay_s": limits.charge_oc1_delay_s,
            "charge_oc2_protect_a": limits.charge_oc2_protect_a,
            "discharge_oc1_protect_a": limits.discharge_oc1_protect_a,
            "discharge_oc1_delay_s": limits.discharge_oc1_delay_s,
            "discharge_oc2_protect_a": limits.discharge_oc2_protect_a,
            "load_short_current_a": limits.load_short_current_a,
        }


class EG4LLRSSISensor(_EG4LLBaseEntity, SensorEntity):
    """Bluetooth signal strength sensor, updated from HA's BLE scanner cache."""

    _attr_device_class = SensorDeviceClass.SIGNAL_STRENGTH
    _attr_native_unit_of_measurement = "dBm"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:bluetooth-audio"

    def __init__(self, coordinator: EG4LLCoordinator, address: str) -> None:
        super().__init__(coordinator, address)
        self._attr_name = "Bluetooth Signal Strength"
        self._attr_unique_id = f"{address}_rssi"

    @property
    def native_value(self) -> int | None:
        return self.coordinator.rssi


class EG4LLEnergySensor(_EG4LLBaseEntity, SensorEntity, RestoreEntity):
    """Energy accumulation sensor for HA's Energy dashboard.

    Tracks total Wh charged into or discharged from the battery. Updates
    every 5 seconds from the coordinator's continuous energy integration
    loop — no BLE connection needed between polls, just uses the last known
    power reading for continuous integration.

    Uses RestoreEntity so the running total survives HA restarts.

    To add to the Energy dashboard:
      Settings -> Energy -> Battery systems -> Add battery
        - Battery energy going into the battery:  select "Energy Charged"
        - Battery energy going out of the battery: select "Energy Discharged"
    """

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfEnergy.WATT_HOUR
    _attr_suggested_display_precision = 2

    def __init__(
        self, coordinator: EG4LLCoordinator, address: str, direction: str
    ) -> None:
        super().__init__(coordinator, address)
        self._direction = direction  # "charged" or "discharged"
        if direction == "charged":
            self._attr_name = "Energy Charged"
            self._attr_unique_id = f"{address}_energy_charged"
            self._attr_icon = "mdi:battery-arrow-up"
        else:
            self._attr_name = "Energy Discharged"
            self._attr_unique_id = f"{address}_energy_discharged"
            self._attr_icon = "mdi:battery-arrow-down"

    async def async_added_to_hass(self) -> None:
        """Restore last known energy value and register for continuous updates."""
        await super().async_added_to_hass()

        # Restore persisted totals from HA state machine
        last_state = await self.async_get_last_state()
        if last_state is not None and last_state.state not in ("unknown", "unavailable"):
            try:
                restored_wh = float(last_state.state)
                if self._direction == "charged":
                    self.coordinator.restore_energy(
                        charged_wh=restored_wh,
                        discharged_wh=self.coordinator.energy_discharged_wh,
                    )
                else:
                    self.coordinator.restore_energy(
                        charged_wh=self.coordinator.energy_charged_wh,
                        discharged_wh=restored_wh,
                    )
                _LOGGER.debug(
                    "EG4 LL: restored %s energy = %.3f Wh", self._direction, restored_wh
                )
            except ValueError:
                _LOGGER.warning(
                    "EG4 LL: could not parse restored energy state '%s'", last_state.state
                )

        # Register with the coordinator's energy loop so we get a state write
        # every 5 seconds, not just on BLE poll boundaries (every 30s).
        # This is what makes the energy sensors update continuously.
        self.coordinator.register_energy_listener(
            lambda: self.async_write_ha_state()
        )

        await self.coordinator.async_request_refresh()

    @property
    def native_value(self) -> float:
        if self._direction == "charged":
            return round(self.coordinator.energy_charged_wh, 3)
        return round(self.coordinator.energy_discharged_wh, 3)


class EG4LLRuntimeSensor(_EG4LLBaseEntity, SensorEntity):
    """Combined runtime sensor — works like the current sensor but for time.

    Charging  → shows time to 100% full
    Discharging → shows time to empty (0%)
    Idle → unavailable

    Extra attributes always expose BOTH values simultaneously so a dashboard
    template card can show "⬆ 4h 20m to full  |  ⬇ 12h 5m to empty" style
    display regardless of current direction, similar to how current in/out
    are both always visible on the current sensor history graph.

    Also keeps time_to_target_min (your configurable % threshold) as an
    attribute for use in automations without needing a separate entity.
    """

    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_suggested_display_precision = 0
    _attr_name = "Estimated Runtime"

    def __init__(self, coordinator: EG4LLCoordinator, address: str) -> None:
        super().__init__(coordinator, address)
        self._attr_unique_id = f"{address}_estimated_runtime"

    @property
    def icon(self) -> str:
        data = self.coordinator.data
        if data is None:
            return "mdi:battery-clock-outline"
        if data.current > 0.1:
            return "mdi:battery-arrow-up"
        if data.current < -0.1:
            return "mdi:battery-arrow-down"
        return "mdi:battery-clock-outline"

    def _time_to_full_min(self, data) -> float | None:
        """Minutes to reach 100% at current charge rate."""
        if data.current <= 0.1:
            return None
        if data.battery_level >= 100:
            return None
        ah_to_full = data.design_capacity * (100 - data.battery_level) / 100.0
        if ah_to_full <= 0:
            return None
        return round(ah_to_full / data.current * 60, 1)

    def _time_to_empty_min(self, data) -> float | None:
        """Minutes to reach 0% at current discharge rate."""
        if data.current >= -0.1:
            return None
        if data.cycle_charge <= 0:
            return None
        return round(data.cycle_charge / abs(data.current) * 60, 1)

    def _time_to_target_min(self, data) -> float | None:
        """Minutes to reach configured target % — used by automations."""
        target_pct = self.coordinator.charge_target_pct
        if data.current <= 0.1:
            return None
        if data.battery_level >= target_pct:
            return None
        ah_to_target = data.design_capacity * (target_pct - data.battery_level) / 100.0
        if ah_to_target <= 0:
            return None
        return round(ah_to_target / data.current * 60, 1)

    @property
    def native_value(self) -> float | None:
        data = self.coordinator.data
        if data is None:
            return None
        # Primary state: time to full while charging, time to empty while discharging
        if data.current > 0.1:
            return self._time_to_full_min(data)
        if data.current < -0.1:
            return self._time_to_empty_min(data)
        return None

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data
        if data is None:
            return {}
        warn_pct = self.coordinator.low_soc_warn_pct
        target_pct = self.coordinator.charge_target_pct

        # Time to low warning threshold while discharging
        time_to_warn = None
        if data.current < -0.1:
            ah_at_warn = data.design_capacity * warn_pct / 100.0
            ah_until_warn = data.cycle_charge - ah_at_warn
            if ah_until_warn > 0:
                time_to_warn = round(ah_until_warn / abs(data.current) * 60, 1)
            else:
                time_to_warn = 0.0

        return {
            # Both directions always exposed for dashboard templates
            "time_to_full_min": self._time_to_full_min(data),
            "time_to_empty_min": self._time_to_empty_min(data),
            # Automation helpers
            "time_to_target_min": self._time_to_target_min(data),
            "time_to_low_warning_min": time_to_warn,
            # Context
            "charge_target_pct": target_pct,
            "low_soc_warning_pct": warn_pct,
            "current_soc_pct": data.battery_level,
            "direction": "charging" if data.current > 0.1 else "discharging" if data.current < -0.1 else "idle",
        }

