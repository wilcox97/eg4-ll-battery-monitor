# EG4 LL Direct — Home Assistant Integration

Direct BLE integration for the EG4 LL 400Ah battery, bypassing auto-discovery.
Protocol confirmed against real hardware (model PLFP-4S200-P00-ZTR-V2.0, fw Z4SR06).

## Installation

1. Unzip and copy the `eg4_ll_direct/` folder into `config/custom_components/`
2. **Add the EG4 logo** (optional but recommended):
   - Download: https://eg4electronics.com/wp-content/uploads/2025/09/EG4-Favicon-300x300.png
   - Save as `icon.png` inside `config/custom_components/eg4_ll_direct/`
   - This makes the real EG4 logo appear on the integration card and device panel
3. Restart Home Assistant
4. Settings → Devices & Services → Add Integration → search **EG4 LL Battery (Direct MAC)**
5. Enter your battery's MAC address (default pre-filled: `80:6F:B0:15:5E:71`)

## Entities created

| Entity | Type | Notes |
|--------|------|-------|
| Pack Voltage | Sensor (V) | |
| Current | Sensor (A) | + = charging, − = discharging |
| State of Charge | Sensor (%) | |
| State of Health | Sensor (%) | |
| Capacity Remaining | Sensor (Ah) | |
| Design Capacity | Sensor (Ah) | |
| Charge Cycles | Sensor | |
| PCB / MOS Temperature | Sensor (°C) | |
| Delta Cell Voltage | Sensor (V) | Imbalance indicator |
| Estimated Runtime | Sensor (min) | Linear estimate |
| Bluetooth Signal Strength | Sensor (dBm) | Diagnostic, from HA BLE scanner |
| Cell 1–4 Voltage | Sensor (V) | Disabled by default |
| Cell Temp 1–2 | Sensor (°C) | Disabled by default |
| Hardware Info | Sensor | Diagnostic, model/firmware/serial as attributes |
| Protection Limits | Sensor | Diagnostic, all BMS thresholds as attributes, disabled by default |
