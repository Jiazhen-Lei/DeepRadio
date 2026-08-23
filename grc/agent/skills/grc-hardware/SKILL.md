---
name: grc-hardware
description: Configure SDR blocks while enforcing confirmation for real hardware access.
---

# GRC hardware

`configure_sdr` remains configuration-only. `discover_devices` and
`probe_device` are read-only UHD checks. `build_usrp_rx_spectrum_flowgraph`
builds a B210 `uhd_usrp_source` + `qtgui_freq_sink_x` receiver without starting
it. Building a UHD flowgraph does not mean it was started. RF start is disabled
by default and requires the dedicated Workflow checkpoint,
`GRC_AGENT_ENABLE_RF=1`, a session-owned `.grc`, bounded duration, and a
verified stop. Realtime spectrum appears in the GNU Radio QT window, not as a
PNG in chat. `stop_flowgraph` and `emergency_stop` are always allowed. Never
claim over-the-air success without independent evidence.
