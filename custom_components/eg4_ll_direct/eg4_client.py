"""Direct BLE client for the EG4 LL battery (Modbus-over-BLE).

This bypasses Home Assistant's Bluetooth auto-discovery matching entirely
and connects directly to a configured MAC address, since this particular
battery's BLE advertisement does not reliably expose the manufacturer ID
that aiobmsble's auto-matcher looks for.

Protocol confirmed via:
  - nRF Connect captures of the real device
  - Decompilation of the official EG4LL Android app (com.zetarapower.monitor)
  - Cross-reference against aiobmsble's eg4_bms.py driver
  - tuxntoast/eg4-ll (RS485 variant) for the hardware-info / config command
    structure, with offsets re-derived and corrected against a real 187-byte
    capture from this exact battery (model PLFP-4S200-P00-ZTR-V2.0, fw Z4SR06)
  - Live-tested end-to-end in a Web Bluetooth browser implementation before
    being ported here, so the protocol itself (UUIDs, commands, CRC, byte
    offsets) is confirmed working against real hardware, not just theoretical.

Key facts:
  - Service UUID:    00001000-0000-1000-8000-00805f9b34fb
  - Notify (RX) UUID: 00001002-...  (subscribe for responses)
  - Write (TX) UUID:  00001001-...  (write the Modbus request here, NOT 0x1003)
  - Live data command:     01 03 00 00 00 27 05 D0  -> 83-byte reply
  - Hardware info command: 01 03 00 69 00 17 D5 D8  -> 51-byte reply
  - BMS config command:    01 03 00 2D 00 5B 94 38  -> 187-byte reply

BMS config temperature encoding (confirmed against real capture, not
assumed): fields run in groups of six per zone -- UT Warn, UT Protect,
UT Release, OT Warn, OT Protect, OT Release. Under-temp (UT) values are
plain signed 16-bit (can be negative directly). Over-temp (OT) values
are stored with a +50 bias and need 50 subtracted back out.

Connection behavior (also confirmed live): the device does NOT auto-stream
live data on subscribe alone -- a poll command must be written to trigger
each response, request -> response, one at a time. Sending a second poll
before the first response arrives can cause the device to concatenate two
frames into one oversized notification, hence the trim-to-expected-length
handling in the notification callback below.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from bleak import BleakClient
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError

_LOGGER = logging.getLogger(__name__)

SERVICE_UUID = "00001000-0000-1000-8000-00805f9b34fb"
NOTIFY_UUID = "00001002-0000-1000-8000-00805f9b34fb"
WRITE_UUID = "00001001-0000-1000-8000-00805f9b34fb"

# Modbus RTU commands: device 0x01, function 0x03 (read holding regs), addr, count, CRC16
LIVE_DATA_CMD = bytes([0x01, 0x03, 0x00, 0x00, 0x00, 0x27, 0x05, 0xD0])
HW_INFO_CMD = bytes([0x01, 0x03, 0x00, 0x69, 0x00, 0x17, 0xD5, 0xD8])
BMS_CONFIG_CMD = bytes([0x01, 0x03, 0x00, 0x2D, 0x00, 0x5B, 0x94, 0x38])

LIVE_DATA_LEN = 83
HW_INFO_LEN = 51
BMS_CONFIG_LEN = 187

CONNECT_TIMEOUT = 12.0
RESPONSE_TIMEOUT = 6.0
MAX_RETRIES_PER_COMMAND = 3


def _crc_modbus(data: bytes) -> int:
    """Compute Modbus RTU CRC-16 (poly 0xA001, init 0xFFFF)."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc & 0xFFFF


def _valid_crc(frame: bytes) -> bool:
    if len(frame) < 3:
        return False
    payload, received = frame[:-2], frame[-2] | (frame[-1] << 8)
    return _crc_modbus(payload) == received


def _u16(b: bytes, o: int) -> int:
    return (b[o] << 8) | b[o + 1]


def _s16(b: bytes, o: int) -> int:
    v = _u16(b, o)
    return v - 0x10000 if v >= 0x8000 else v


def _u32(b: bytes, o: int) -> int:
    return (b[o] << 24) | (b[o + 1] << 16) | (b[o + 2] << 8) | b[o + 3]


def _s8(b: bytes, o: int) -> int:
    return b[o] - 0x100 if b[o] >= 0x80 else b[o]


@dataclass
class EG4LLHardwareInfo:
    """Static hardware identification, fetched once per connection."""

    model: str
    firmware_version: str
    serial: str


@dataclass
class EG4LLLimits:
    """BMS protection thresholds / configuration, fetched once per connection."""

    balance_start_v: float
    balance_diff_v: float
    low_capacity_warn_pct: int
    cell_uv_warn: float
    cell_uv_protect: float
    cell_uv_release: float
    cell_ov_warn: float
    cell_ov_protect: float
    cell_ov_release: float
    pack_uv_warn: float
    pack_uv_protect: float
    pack_uv_release: float
    pack_ov_warn: float
    pack_ov_protect: float
    pack_ov_release: float
    charge_ut_warn_c: int
    charge_ut_protect_c: int
    charge_ut_release_c: int
    charge_ot_warn_c: int
    charge_ot_protect_c: int
    charge_ot_release_c: int
    discharge_ut_warn_c: int
    discharge_ut_protect_c: int
    discharge_ut_release_c: int
    discharge_ot_warn_c: int
    discharge_ot_protect_c: int
    discharge_ot_release_c: int
    pcb_ot_warn_c: int
    pcb_ot_protect_c: int
    charge_oc1_protect_a: float
    charge_oc1_delay_s: int
    charge_oc2_protect_a: float
    discharge_oc1_protect_a: float
    discharge_oc1_delay_s: int
    discharge_oc2_protect_a: float
    load_short_current_a: float


@dataclass
class EG4LLSample:
    """A single decoded live-telemetry reading from the EG4 LL battery."""

    voltage: float
    current: float
    battery_level: int  # SoC %
    battery_health: int  # SoH %
    cycle_charge: float  # remaining capacity, Ah
    cycles: int
    cell_count: int
    cell_voltages: list[float]
    temperature: float  # PCB/MOS temp, deg C
    temp_values: list[int]  # individual cell temp sensors, deg C
    design_capacity: int  # Ah
    hw_info: EG4LLHardwareInfo | None = None
    limits: EG4LLLimits | None = None


def _decode_live(frame: bytes) -> EG4LLSample:
    cell_count = min(_u16(frame, 75), 16)
    cells = [_u16(frame, 7 + i * 2) / 1000 for i in range(cell_count)]
    temps = [t for i in range(6) if (t := _s8(frame, 69 + i)) != 0]

    return EG4LLSample(
        voltage=_u16(frame, 3) / 100,
        current=_s16(frame, 5) / 10,
        battery_level=_u16(frame, 51),
        battery_health=_u16(frame, 49),
        cycle_charge=_u16(frame, 45) / 10,
        cycles=_u32(frame, 61),
        cell_count=cell_count,
        cell_voltages=cells,
        temperature=float(_s16(frame, 39)),
        temp_values=temps,
        design_capacity=_u16(frame, 77) // 10,
    )


def _ascii_range(b: bytes, start: int, end: int) -> str:
    """Decode a null-terminated ASCII range, trimming trailing whitespace."""
    out = bytearray()
    for i in range(start, min(end, len(b))):
        if b[i] == 0:
            break
        out.append(b[i])
    return out.decode("ascii", errors="replace").strip()


def _decode_hw_info(frame: bytes) -> EG4LLHardwareInfo:
    # Offsets confirmed against a real capture; the model string was found to
    # start one byte earlier than the original tuxntoast/eg4-ll reference
    # (e.g. real string starts with "P" in "PLFP-...", which a -1 shift recovers).
    return EG4LLHardwareInfo(
        model=_ascii_range(frame, 1, 25),
        firmware_version=_ascii_range(frame, 27, 33),
        serial=_ascii_range(frame, 33, 49),
    )


def _decode_limits(b: bytes) -> EG4LLLimits:
    def mv(o: int) -> float:
        return round(_u16(b, o) / 1000, 3)

    def cv(o: int) -> float:
        return round(_u16(b, o) / 100, 2)

    def ut(o: int) -> int:
        """Under-temp fields: plain signed value, no bias."""
        return _s16(b, o)

    def ot(o: int) -> int:
        """Over-temp fields: stored with a +50 bias, subtract back out."""
        return _s16(b, o) - 50

    return EG4LLLimits(
        balance_start_v=mv(25),
        balance_diff_v=mv(27),
        low_capacity_warn_pct=_u16(b, 29),
        cell_uv_warn=mv(35),
        cell_uv_protect=mv(37),
        cell_uv_release=mv(39),
        cell_ov_warn=mv(47),
        cell_ov_protect=mv(49),
        cell_ov_release=mv(51),
        pack_uv_warn=cv(41),
        pack_uv_protect=cv(43),
        pack_uv_release=cv(45),
        pack_ov_warn=cv(53),
        pack_ov_protect=cv(55),
        pack_ov_release=cv(57),
        charge_ut_warn_c=ut(93),
        charge_ut_protect_c=ut(95),
        charge_ut_release_c=ut(97),
        charge_ot_warn_c=ot(99),
        charge_ot_protect_c=ot(101),
        charge_ot_release_c=ot(103),
        discharge_ut_warn_c=ut(105),
        discharge_ut_protect_c=ut(107),
        discharge_ut_release_c=ut(109),
        discharge_ot_warn_c=ot(111),
        discharge_ot_protect_c=ot(113),
        discharge_ot_release_c=ot(115),
        pcb_ot_warn_c=ot(117),
        pcb_ot_protect_c=ot(119),
        charge_oc1_protect_a=round(_u16(b, 73) / 100, 1),
        charge_oc1_delay_s=_u16(b, 83),
        charge_oc2_protect_a=round(_u16(b, 79) / 100, 1),
        discharge_oc1_protect_a=round(_u16(b, 75) / 100, 1),
        discharge_oc1_delay_s=_u16(b, 87),
        discharge_oc2_protect_a=round(_u16(b, 81) / 100, 1),
        load_short_current_a=round(_u16(b, 77) / 100, 1),
    )


class EG4LLClient:
    """Manages a short-lived BLE connection to fetch one sample.

    Hardware info and BMS limits (static config, doesn't change between
    polls) are only re-fetched if not already cached on this instance, to
    avoid spending extra round-trips on data that never changes.

    Uses bleak-retry-connector's establish_connection() when a BLEDevice
    is available (populated by the coordinator from HA's BLE scanner),
    falling back to a plain BleakClient only if no device has been seen yet.
    """

    def __init__(self, address: str) -> None:
        self._address = address
        self._ble_device: BLEDevice | None = None
        self._hw_info: EG4LLHardwareInfo | None = None
        self._limits: EG4LLLimits | None = None

    def update_ble_device(self, ble_device: BLEDevice | None) -> None:
        """Update the BLEDevice from HA's scanner. Called before each fetch."""
        if ble_device is not None:
            self._ble_device = ble_device

    async def _make_client(self) -> BleakClient:
        """Create a connected BleakClient using the best available path."""
        if self._ble_device is not None:
            from bleak_retry_connector import establish_connection
            return await establish_connection(
                client_class=BleakClient,
                device=self._ble_device,
                name=self._address,
                max_attempts=3,
                use_services_cache=False,  # Disable stale GATT cache from prior connections
            )
        # Fallback: no BLEDevice yet (shouldn't happen after __init__.py fix,
        # but kept as a safety net)
        _LOGGER.debug("EG4 LL: no BLEDevice available, using address fallback")
        client = BleakClient(self._address, timeout=CONNECT_TIMEOUT)
        await client.connect()
        return client

    async def async_fetch(self) -> EG4LLSample:
        """Connect, request live data (+ static info on first connect), disconnect."""
        client = await self._make_client()

        # Per-connection mutable state shared with the notification callback.
        state: dict[str, object] = {"frame": None, "expected_len": LIVE_DATA_LEN}
        frame_event = asyncio.Event()

        def _on_notify(_handle: int, data: bytearray) -> None:
            buf = bytes(data)
            expected_len = int(state["expected_len"])
            if len(buf) < 5:
                return
            if len(buf) < expected_len:
                _LOGGER.debug(
                    "EG4 LL: partial packet (%d/%d bytes), ignoring",
                    len(buf),
                    expected_len,
                )
                return
            # Device occasionally concatenates two frames into one oversized
            # notification if a new poll goes out before the prior reply
            # finished -- trim to just the first valid frame.
            frame = buf[:expected_len]
            if not _valid_crc(frame):
                _LOGGER.debug("EG4 LL: CRC check failed on notification, ignoring")
                return
            state["frame"] = frame
            frame_event.set()

        async def _send_and_wait(cmd: bytes, expected_len: int) -> bytes:
            state["expected_len"] = expected_len
            state["frame"] = None
            last_exc: Exception | None = None
            for attempt in range(MAX_RETRIES_PER_COMMAND):
                frame_event.clear()
                try:
                    try:
                        await client.write_gatt_char(WRITE_UUID, cmd, response=True)
                    except BleakError:
                        await client.write_gatt_char(WRITE_UUID, cmd, response=False)
                    await asyncio.wait_for(frame_event.wait(), timeout=RESPONSE_TIMEOUT)
                    return bytes(state["frame"])  # type: ignore[arg-type]
                except (TimeoutError, BleakError) as exc:
                    last_exc = exc
                    _LOGGER.debug(
                        "EG4 LL: attempt %d/%d failed for cmd %s: %s",
                        attempt + 1,
                        MAX_RETRIES_PER_COMMAND,
                        cmd.hex(),
                        exc,
                    )
                # Small gap before retrying, mirrors the working browser implementation's pacing.
                await asyncio.sleep(0.5)
            raise TimeoutError(
                f"No valid response to command {cmd.hex()} after "
                f"{MAX_RETRIES_PER_COMMAND} attempts"
            ) from last_exc

        try:
            await client.connect()

            # Wait for GATT service discovery to complete before subscribing.
            # BlueZ finishes the link-layer connection slightly before GATT
            # characteristics are populated — calling start_notify() too quickly
            # throws "Characteristic not found" even though the device is connected.
            for attempt in range(10):
                try:
                    await client.start_notify(NOTIFY_UUID, _on_notify)
                    break  # success
                except Exception as exc:
                    if "not found" in str(exc).lower() and attempt < 9:
                        _LOGGER.debug(
                            "EG4 LL: GATT not ready yet (attempt %d/10), waiting 1s...", attempt + 1
                        )
                        await asyncio.sleep(1.0)
                    else:
                        raise

            # Fetch static info once per client instance, not every poll.
            if self._hw_info is None:
                try:
                    hw_frame = await _send_and_wait(HW_INFO_CMD, HW_INFO_LEN)
                    self._hw_info = _decode_hw_info(hw_frame)
                except TimeoutError:
                    _LOGGER.warning("EG4 LL: hardware info fetch failed, continuing without it")
                await asyncio.sleep(0.3)

            if self._limits is None:
                try:
                    cfg_frame = await _send_and_wait(BMS_CONFIG_CMD, BMS_CONFIG_LEN)
                    self._limits = _decode_limits(cfg_frame)
                except TimeoutError:
                    _LOGGER.warning("EG4 LL: BMS limits fetch failed, continuing without it")
                await asyncio.sleep(0.3)

            live_frame = await _send_and_wait(LIVE_DATA_CMD, LIVE_DATA_LEN)
            sample = _decode_live(live_frame)
            sample.hw_info = self._hw_info
            sample.limits = self._limits

            await client.stop_notify(NOTIFY_UUID)
            return sample
        finally:
            if client.is_connected:
                await client.disconnect()
