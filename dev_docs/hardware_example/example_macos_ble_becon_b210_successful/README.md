# macOS GNU Radio BLE Beacon TX

This example replays the supplied pre-generated BLE complex-IQ waveform through a USRP B210.

## Signal parameters

- GNU Radio / RadioConda: 3.10.12.0
- Input format: little-endian interleaved `float32` I/Q (`complex64`)
- Complex samples: 200,000
- Sample rate: 2 MS/s
- File/repeat duration: 100 ms
- BLE PHY represented by the waveform: LE 1M GFSK
- BLE advertising channel: 38
- RF center frequency: 2426 MHz
- USRP channel: 0, antenna `TX/RX`
- CPU / wire formats: `fc32` / `sc16`
- TX gain: 65 dB, matching the previous UHD implementation

The waveform contains 464 non-zero complex samples followed by zero padding. Repeating the complete 200,000-sample file at 2 MS/s therefore preserves the embedded 100 ms beacon interval.

## Run

Connect the B210 with a USB 3.x data connection, then verify it first:

```bash
source ~/radioconda/bin/activate
uhd_find_devices --args "type=b200"
```

Open and run the flowgraph:

```bash
gnuradio-companion ble_beacon_bin_tx_b210.grc
```

Alternatively, run the generated Python file from this directory:

```bash
python ble_beacon_bin_tx_b210.py
```

The 65 dB gain is intentionally retained for parameter parity with the earlier implementation. Reduce `tx_gain` before close-range or conducted tests, and connect a suitable antenna or rated 50-ohm load before transmitting.
