---
name: grc-hardware
description: Configure SDR blocks while enforcing confirmation for real hardware access.
---

# GRC hardware

Phase one supports `configure_sdr` only as flowgraph configuration. Do not claim
that a physical device was discovered, opened, or transmitting. `list_devices`
is intentionally disabled. Any future hardware action must pass PolicyGateway
and require explicit user confirmation.
