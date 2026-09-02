---
name: grc-ble-advertising
description: Build and offline-verify BLE advertising PDUs, 1M PHY waveforms, and TX flowgraphs.
---

# BLE advertising

Use `build_ble_advertising_pdu`; never invent PDU bits, CRC, whitening, or an
over-the-air success claim. Complete Local Name is required and must fit the
legacy advertising payload. Treat phone observation as independent evidence.

# BLE PHY

Use `generate_ble_1m_waveform` and `verify_ble_packet_bits`. Building a B210
UHD or PlutoSDR TX flowgraph does not mean it was started. RF execution requires
confirmation bound to the current Workflow and flowgraph version, bounded
duration, and a confirmed stop.
