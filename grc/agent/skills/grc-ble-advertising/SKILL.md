---
name: grc-ble-advertising
description: Build BLE advertising data with a deterministic protocol tool.
---

# BLE advertising

Use `build_ble_advertising_pdu`; never invent PDU bits, CRC, whitening, or an
over-the-air success claim. Complete Local Name is required and must fit the
legacy advertising payload. Treat phone observation as independent evidence.
