"""DataUpdateCoordinator for the EG4 LL Direct integration."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_ADDRESS,
    CONF_CHARGE_TARGET_PCT,
    CONF_LOW_SOC_WARN_PCT,
    DEFAULT_CHARGE_TARGET_PCT,
    DEFAULT_LOW_SOC_WARN_PCT,
    DOMAIN,
    UPDATE_INTERVAL_SECONDS,
)
from .eg4_client import EG4LLClient, EG4LLSample

_LOGGER = logging.getLogger(__name__)

# Energy integration runs on its own faster loop, independent of the BLE poll
# rate. This uses the last known voltage/current values to integrate power
# continuously, rather than only at BLE poll boundaries.
ENERGY_INTEGRATION_INTERVAL_S = 5


class EG4LLCoordinator(DataUpdateCoordinator[EG4LLSample]):
    """Polls the EG4 LL battery over BLE on a fixed interval.

    Energy accumulation (Wh in / Wh out) runs on a separate faster loop
    (every 5s) that continuously integrates the last known power reading.
    This gives much more accurate energy tracking than integrating only at
    the 30s BLE poll boundaries, especially when current is fluctuating.

    The two loops are independent:
      - BLE poll (30s): fetches fresh voltage/current/SOC/etc from the battery
      - Energy loop (5s): integrates last known power, updates energy sensors,
        writes state to HA for persistence -- no BLE connection needed
    """

    def __init__(self, hass: HomeAssistant, address: str, config_entry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=UPDATE_INTERVAL_SECONDS),
        )
        self._address = address
        self._config_entry = config_entry
        self._client = EG4LLClient(address)
        self.rssi: int | None = None

    def update_ble_device(self, ble_device) -> None:
        """Pre-seed the BLEDevice so the first connection uses establish_connection()."""
        self._client.update_ble_device(ble_device)

        # Energy accumulators in Wh, always positive, always increasing.
        # Charged = energy put INTO the battery (positive current).
        # Discharged = energy taken OUT (negative current).
        self.energy_charged_wh: float = 0.0
        self.energy_discharged_wh: float = 0.0

        # Last known power reading, updated every BLE poll.
        # The energy loop uses this to integrate continuously.
        self._last_power_w: float | None = None
        self._last_energy_time: datetime | None = None

        # Energy sensor callbacks — registered by EG4LLEnergySensor so the
        # energy loop can push state updates without waiting for a BLE poll.
        self._energy_listeners: list[callable] = []

        # HA cancel handle for the energy integration loop.
        self._energy_cancel: callable | None = None

        # Restore flag — set once persisted values are loaded from HA storage.
        self._energy_restored: bool = False

    @property
    def charge_target_pct(self) -> int:
        """Configurable charge target SOC (default 80%)."""
        return self._config_entry.options.get(
            CONF_CHARGE_TARGET_PCT, DEFAULT_CHARGE_TARGET_PCT
        )

    @property
    def low_soc_warn_pct(self) -> int:
        """Configurable low SOC warning threshold (default 20%)."""
        return self._config_entry.options.get(
            CONF_LOW_SOC_WARN_PCT, DEFAULT_LOW_SOC_WARN_PCT
        )

    # ------------------------------------------------------------------
    # HA lifecycle
    # ------------------------------------------------------------------

    async def _async_setup(self) -> None:
        """Called by DataUpdateCoordinator after hass is set. Start the
        energy integration loop here so it has access to self.hass."""
        self._energy_cancel = async_track_time_interval(
            self.hass,
            self._async_energy_tick,
            timedelta(seconds=ENERGY_INTEGRATION_INTERVAL_S),
        )

    async def async_shutdown(self) -> None:
        """Stop the energy loop on unload."""
        if self._energy_cancel is not None:
            self._energy_cancel()
            self._energy_cancel = None
        await super().async_shutdown()

    # ------------------------------------------------------------------
    # Energy persistence restore hook
    # ------------------------------------------------------------------

    def restore_energy(self, charged_wh: float, discharged_wh: float) -> None:
        """Called by EG4LLEnergySensor.async_added_to_hass() via RestoreEntity.
        Restores persisted totals so the accumulators don't reset on reboot."""
        if not self._energy_restored:
            self.energy_charged_wh = max(0.0, charged_wh)
            self.energy_discharged_wh = max(0.0, discharged_wh)
            self._energy_restored = True
            _LOGGER.debug(
                "EG4 LL: restored energy — charged=%.3f Wh, discharged=%.3f Wh",
                self.energy_charged_wh,
                self.energy_discharged_wh,
            )

    def register_energy_listener(self, listener: callable) -> None:
        """Register a callback to be called every time energy values update.
        Used by EG4LLEnergySensor to push state updates from the energy loop."""
        self._energy_listeners.append(listener)

    # ------------------------------------------------------------------
    # Continuous energy integration loop (every 5s)
    # ------------------------------------------------------------------

    @callback
    def _async_energy_tick(self, _now: datetime) -> None:
        """Integrate power over the elapsed time since the last tick.
        Runs every ENERGY_INTEGRATION_INTERVAL_S seconds on HA's event loop.
        Uses the last BLE-polled power reading — no new BLE connection needed.
        """
        if self._last_power_w is None:
            # No data yet — nothing to integrate
            return

        now = datetime.now(tz=timezone.utc)
        if self._last_energy_time is None:
            self._last_energy_time = now
            return

        elapsed_h = (now - self._last_energy_time).total_seconds() / 3600.0
        self._last_energy_time = now

        # Guard against large gaps (HA paused/restarted mid-session)
        if elapsed_h > 120 / 3600:  # > 2 minutes is suspicious, skip
            return

        power_w = self._last_power_w
        if power_w > 0.1:
            self.energy_charged_wh += power_w * elapsed_h
        elif power_w < -0.1:
            self.energy_discharged_wh += abs(power_w) * elapsed_h

        # Notify energy sensor entities so they write updated state to HA.
        # This is what makes the energy sensors update every 5s rather than
        # waiting for the next 30s BLE poll.
        for listener in self._energy_listeners:
            try:
                listener()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------
    # BLE poll (every 30s) — updates live data and refreshes power reading
    # ------------------------------------------------------------------

    async def _async_update_data(self) -> EG4LLSample:
        # Get fresh BLEDevice and RSSI from HA's BLE scanner before each fetch.
        # Passing the BLEDevice ensures establish_connection() is used rather
        # than the raw BleakClient fallback that triggers habluetooth warnings.
        try:
            from homeassistant.components.bluetooth import async_ble_device_from_address
            ble_device = async_ble_device_from_address(
                self.hass, self._address, connectable=True
            )
            if ble_device is not None:
                self._client.update_ble_device(ble_device)
            adv = bluetooth.async_last_service_info(self.hass, self._address)
            if adv is not None:
                self.rssi = adv.rssi
        except Exception:  # noqa: BLE001
            pass

        try:
            sample = await self._client.async_fetch()
        except Exception as err:  # noqa: BLE001
            raise UpdateFailed(f"Error communicating with EG4 LL battery: {err}") from err

        # Update the power reading the energy loop integrates from.
        # The energy loop will now use this fresh value for the next 30s
        # worth of 5-second ticks, until the next BLE poll lands.
        self._last_power_w = sample.voltage * sample.current

        # Ensure the energy loop has a start time on first successful poll.
        if self._last_energy_time is None:
            self._last_energy_time = datetime.now(tz=timezone.utc)

        return sample
