# EG4 LL Battery Monitor

A complete monitoring solution for the **EG4 LL 400Ah LiFePO4 battery** over Bluetooth Low Energy (BLE), consisting of two components:

1. **Home Assistant Custom Integration** (`eg4_ll_direct`) — full sensor suite for HA dashboards, automations, and the Energy dashboard
2. **Web Bluetooth Dashboard** (`eg4-ll-monitor.html`) — standalone browser-based monitor, no server required

> **Developed with Claude (Anthropic)** — This project was built through an extensive collaborative reverse-engineering effort using Claude as the primary development partner. The protocol was reverse-engineered from scratch via nRF Connect BLE captures, APK decompilation of the official EG4LL Android app, and cross-referencing against open-source drivers. All byte offsets, CRC logic, and connection sequencing were validated against real hardware before being ported to both the web and HA implementations.

---

## Hardware

- **Battery:** EG4 LL 400Ah LiFePO4 (model `PLFP-4S200-P00-ZTR-V2.0`, firmware `Z4SR06`)
- **Protocol:** Modbus RTU over BLE (custom EG4 GATT service)
- **Connection:** Bluetooth LE, direct MAC address pairing

### Confirmed BLE Protocol

| Item | Value |
|------|-------|
| Service UUID | `00001000-0000-1000-8000-00805f9b34fb` |
| Notify (RX) UUID | `00001002-...` |
| Write (TX) UUID | `00001001-...` (**not** `0x1003`) |
| Live data command | `01 03 00 00 00 27 05 D0` → 83-byte reply |
| Hardware info command | `01 03 00 69 00 17 D5 D8` → 51-byte reply |
| BMS config command | `01 03 00 2D 00 5B 94 38` → 187-byte reply |

The device **does not auto-stream** — a poll command must be written after subscribing to notifications to trigger each response.

---

## Home Assistant Integration

### Features

- **Pack voltage, current, power** (W)
- **State of Charge (SOC%)** and **State of Health (SOH%)**
- **Capacity remaining** (Ah) and **design capacity**
- **Charge cycles**
- **Individual cell voltages** (4 cells, disabled by default)
- **PCB/MOS temperature** + cell temperatures
- **Delta cell voltage** (imbalance indicator)
- **Estimated runtime** — time to full (charging) or time to empty (discharging)
- **Energy Charged / Energy Discharged** (Wh, persisted across restarts) — feeds HA Energy dashboard
- **Bluetooth Signal Strength** (dBm, diagnostic)
- **Hardware Info** sensor (model, firmware, serial as attributes)
- **Protection Limits** sensor (all 33 BMS thresholds as attributes, disabled by default)
- **Low Battery** binary sensor (configurable threshold)
- **Charging / Discharging** binary sensors

### Installation

#### HACS (recommended)

1. In HACS → Integrations → ⋮ → Custom repositories
2. Add `https://github.com/YOUR_USERNAME/eg4-ll-battery-monitor` as **Integration**
3. Install **EG4 LL Battery (Direct MAC)**
4. Restart Home Assistant
5. Settings → Devices & Services → Add Integration → search **EG4 LL Battery**

#### Manual

1. Copy `custom_components/eg4_ll_direct/` to `config/custom_components/eg4_ll_direct/`
2. Restart Home Assistant
3. Settings → Devices & Services → Add Integration → search **EG4 LL Battery**

### Configuration

Enter your battery's Bluetooth MAC address when prompted (default pre-filled: `80:6F:B0:15:5E:71` — change this to match your battery).

**Options** (Settings → Integrations → EG4 LL Battery → Configure):
- **Poll interval** — seconds between BLE polls (default 30s, minimum 2s)
- **Charge target SOC%** — target for runtime-to-full estimate (default 80%)
- **Low battery warning threshold** — triggers the Low Battery binary sensor (default 20%)

### Energy Dashboard

After installing, go to **Settings → Energy → Battery systems → Add battery**:
- Battery energy going **in**: select `Energy Charged`
- Battery energy going **out**: select `Energy Discharged`

---

## Web Bluetooth Dashboard

A single self-contained HTML file that runs in Chrome/Edge on any device — no server, no install, no dependencies.

### Features

- Live pack voltage, current (with charge/discharge color coding), SOC, SOH, capacity
- Individual cell voltages with high/low highlighting
- PCB/MOS + cell temperatures (°C and °F)
- Estimated runtime (time to full or time to empty)
- **Smoothed scrolling history graphs** (voltage/SOC + current) with configurable time window
- BMS protection limits panel (all 33 thresholds — fetched once on connect)
- Hardware info panel (model, firmware, serial)
- CSV and PNG export
- Responsive desktop + mobile layout
- Bluetooth signal strength (RSSI) with signal bar

### Usage

1. Open `eg4-ll-monitor.html` in **Chrome or Edge** (Web Bluetooth required — Safari and Firefox not supported)
2. Click **Connect to Battery**
3. Select your EG4 LL battery from the device picker

### Browser Compatibility

| Browser | Support |
|---------|---------|
| Chrome (desktop/Android) | ✅ Full support |
| Edge (desktop/Android) | ✅ Full support |
| Safari (any) | ❌ No Web Bluetooth |
| Firefox | ❌ No Web Bluetooth |
| Chrome on iOS | ❌ Apple restricts Web Bluetooth on iOS |

---

## Technical Notes

### Protocol Reverse Engineering

This implementation was reverse-engineered entirely from scratch:

- **nRF Connect BLE captures** — live packet capture from the real device
- **APK decompilation** — official EG4LL Android app (`com.zetarapower.monitor`) decompiled with androguard; confirmed the app sends zero write commands on connect (pure subscribe + notify), and identified the correct write characteristic (`0x1001` not `0x1003`)
- **aiobmsble cross-reference** — confirmed TX characteristic UUID and command structure
- **tuxntoast/eg4-ll RS485 driver** — provided initial field offset hints, corrected against real captures

### Temperature Encoding (BMS Config Frame)

The 187-byte BMS config frame uses a non-obvious mixed encoding for temperature fields:
- **Under-temp (UT) fields**: plain signed 16-bit values (can be negative directly)
- **Over-temp (OT) fields**: stored with +50 bias (subtract 50 to get °C)

This was confirmed by analyzing a real 187-byte capture and finding the pattern that produced self-consistent, physically sane values across all 14 temperature thresholds.

### Known Issues

- The device occasionally sends two 83-byte frames concatenated into one 166-byte notification. The parser trims to the expected length before CRC validation.
- Startup requires the HA BLE scanner to have seen the battery before connection is attempted. The integration waits up to 60 seconds at startup for the device to appear in scanner cache before attempting to connect, ensuring `establish_connection()` from `bleak-retry-connector` is always used rather than a raw `BleakClient`.

---

## License

MIT License — see [LICENSE](LICENSE)

## Credits

- **Daimon Wilcox** — hardware, testing, real-world validation, project direction
- **Claude (Anthropic)** — protocol reverse engineering, implementation, debugging across 14+ integration iterations
