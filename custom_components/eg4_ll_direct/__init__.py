"""The EG4 LL Direct integration."""

from __future__ import annotations

import asyncio
import logging

from homeassistant.components.bluetooth import async_ble_device_from_address
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .const import CONF_ADDRESS, DOMAIN
from .coordinator import EG4LLCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["sensor", "binary_sensor"]

# How long to wait for the BLE scanner to see the device before giving up
# and raising ConfigEntryNotReady (which triggers HA's automatic retry).
BLE_DISCOVERY_TIMEOUT = 60  # seconds


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up EG4 LL Direct from a config entry."""
    address = entry.data[CONF_ADDRESS]

    # Wait for HA's BLE scanner to actually see the battery before we attempt
    # to connect. Without this, the integration tries to connect immediately at
    # startup before BlueZ has finished scanning, falls through to the raw
    # BleakClient() fallback, and fails with "No ATT transport" on every write.
    # The scanner typically sees the battery within 5-15 seconds of startup.
    _LOGGER.debug("EG4 LL: waiting for BLE scanner to see device %s", address)
    ble_device = None
    waited = 0
    while ble_device is None and waited < BLE_DISCOVERY_TIMEOUT:
        ble_device = async_ble_device_from_address(hass, address, connectable=True)
        if ble_device is None:
            await asyncio.sleep(2)
            waited += 2

    if ble_device is None:
        raise ConfigEntryNotReady(
            f"EG4 LL battery {address} not found by BLE scanner after "
            f"{BLE_DISCOVERY_TIMEOUT}s. Check that the battery is powered "
            f"on and in range of the Raspberry Pi."
        )

    _LOGGER.info("EG4 LL: BLE scanner found device after %ds, setting up", waited)

    coordinator = EG4LLCoordinator(hass, address, entry)
    # Give the client the BLEDevice immediately so the first fetch
    # uses establish_connection() rather than the raw fallback.
    coordinator.update_ble_device(ble_device)

    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("EG4 LL: initial fetch failed (%s) — will retry", err)
        raise ConfigEntryNotReady(f"EG4 LL battery not responding: {err}") from err

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    coordinator: EG4LLCoordinator = hass.data[DOMAIN].get(entry.entry_id)
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        if coordinator is not None:
            await coordinator.async_shutdown()
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok
