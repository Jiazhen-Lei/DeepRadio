---
name: grc-hardware
description: Configure SDR blocks while enforcing confirmation for real hardware access.
---

# GRC hardware

`configure_sdr` remains configuration-only. `discover_devices` and
`probe_device` are read-only checks (`uhd_find_devices` for B210, `iio_info`
for PlutoSDR). `build_usrp_rx_spectrum_flowgraph`
builds a B210 `uhd_usrp_source` + `qtgui_freq_sink_x` receiver without starting
it. Building a UHD or Pluto flowgraph does not mean it was started. Validate the
graph **while the hardware sink is enabled**, then disable it. Host
re-validation uses `arm_disabled_rf` so a later critic pass on that unarmed
graph is not a Stage failure — GNU Radio reports `Port is not connected` for
disabled sinks. RF start requires an explicit MainAgent user-decision request
bound to the current Workflow and flowgraph version, a session-owned `.grc`,
bounded duration, and a verified stop. Realtime spectrum appears in the GNU Radio QT window, not as a
PNG in chat. `stop_flowgraph` and `emergency_stop` are always allowed. Never
claim over-the-air success without independent evidence.
