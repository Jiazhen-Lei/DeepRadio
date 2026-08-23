---
name: grc-ble-phy
description: Generate and verify BLE 1M PHY GFSK artifacts safely offline.
---

# BLE PHY

Use `generate_ble_1m_waveform` and `verify_ble_packet_bits`. Building a UHD TX
flowgraph does not mean it was started. RF execution requires the dedicated
checkpoint, explicit feature flag, bounded duration, and a confirmed stop.
