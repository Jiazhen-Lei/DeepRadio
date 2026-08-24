#!/usr/bin/env python3
# -*- coding: utf-8 -*-

#
# SPDX-License-Identifier: GPL-3.0
#
# GNU Radio Python Flow Graph
# Title: BLE Beacon BIN TX - USRP B210 - Channel 38
# Author: DeepRadio
# Description: The input file is little-endian interleaved float32 I/Q: 200000 complex samples at 2 MS/s. It contains one BLE advertising-channel-38 waveform followed by zero padding to 100 ms, and is repeated continuously.
# GNU Radio version: 3.10.12.0

from gnuradio import blocks
import pmt
from gnuradio import uhd
import time
import threading
from gnuradio import gr
from gnuradio.filter import firdes
from gnuradio.fft import window
import sys
import signal
from argparse import ArgumentParser
from gnuradio.eng_arg import eng_float, intx
from gnuradio import eng_notation




class ble_beacon_bin_tx_b210(gr.top_block):

    def __init__(self):
        gr.top_block.__init__(self, "BLE Beacon BIN TX - USRP B210 - Channel 38", catch_exceptions=True)

        ##################################################
        # Variables
        ##################################################
        self.waveform_samples = waveform_samples = 200000
        self.tx_gain = tx_gain = 65
        self.samp_rate = samp_rate = 2e6
        self.center_freq = center_freq = 2426e6

        ##################################################
        # Blocks
        ##################################################

        self.uhd_usrp_sink_0 = uhd.usrp_sink(
            ",".join(("type=b200", "")),
            uhd.stream_args(
                cpu_format="fc32",
                otw_format="sc16",
                args='',
                channels=list(range(0,1)),
            ),
            "",
        )
        self.uhd_usrp_sink_0.set_clock_source('internal', 0)
        self.uhd_usrp_sink_0.set_time_source('internal', 0)
        self.uhd_usrp_sink_0.set_samp_rate(samp_rate)
        # No synchronization enforced.

        self.uhd_usrp_sink_0.set_center_freq(center_freq, 0)
        self.uhd_usrp_sink_0.set_antenna('TX/RX', 0)
        self.uhd_usrp_sink_0.set_bandwidth(samp_rate, 0)
        self.uhd_usrp_sink_0.set_gain(tx_gain, 0)
        self.blocks_file_source_0 = blocks.file_source(gr.sizeof_gr_complex*1, '/Users/decade/Library/CloudStorage/OneDrive-个人/iiot/5_academia/DeepRadio/experiment/example_macos_ble_becon/ble_beacon_localname_radiomaster.bin', True, 0, waveform_samples)
        self.blocks_file_source_0.set_begin_tag(pmt.PMT_NIL)


        ##################################################
        # Connections
        ##################################################
        self.connect((self.blocks_file_source_0, 0), (self.uhd_usrp_sink_0, 0))


    def get_waveform_samples(self):
        return self.waveform_samples

    def set_waveform_samples(self, waveform_samples):
        self.waveform_samples = waveform_samples

    def get_tx_gain(self):
        return self.tx_gain

    def set_tx_gain(self, tx_gain):
        self.tx_gain = tx_gain
        self.uhd_usrp_sink_0.set_gain(self.tx_gain, 0)

    def get_samp_rate(self):
        return self.samp_rate

    def set_samp_rate(self, samp_rate):
        self.samp_rate = samp_rate
        self.uhd_usrp_sink_0.set_samp_rate(self.samp_rate)
        self.uhd_usrp_sink_0.set_bandwidth(self.samp_rate, 0)

    def get_center_freq(self):
        return self.center_freq

    def set_center_freq(self, center_freq):
        self.center_freq = center_freq
        self.uhd_usrp_sink_0.set_center_freq(self.center_freq, 0)




def main(top_block_cls=ble_beacon_bin_tx_b210, options=None):
    tb = top_block_cls()

    def sig_handler(sig=None, frame=None):
        tb.stop()
        tb.wait()

        sys.exit(0)

    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    tb.start()

    try:
        input('Press Enter to quit: ')
    except EOFError:
        pass
    tb.stop()
    tb.wait()


if __name__ == '__main__':
    main()
