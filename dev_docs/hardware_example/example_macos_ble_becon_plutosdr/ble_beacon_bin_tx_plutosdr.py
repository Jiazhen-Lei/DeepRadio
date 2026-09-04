#!/usr/bin/env python3
# -*- coding: utf-8 -*-

#
# SPDX-License-Identifier: GPL-3.0
#
# GNU Radio Python Flow Graph
# Title: BLE Beacon BIN TX - PlutoSDR - Channel 38
# Author: DeepRadio
# Description: The input file is little-endian interleaved float32 I/Q: 200000 complex samples at 2 MS/s. It contains one BLE advertising-channel-38 waveform followed by zero padding to 100 ms, and is repeated continuously.
# GNU Radio version: 3.10.12.0

from gnuradio import blocks
import pmt
from gnuradio import gr
from gnuradio.filter import firdes
from gnuradio.fft import window
import sys
import signal
from argparse import ArgumentParser
from gnuradio.eng_arg import eng_float, intx
from gnuradio import eng_notation
from gnuradio import iio
import threading




class ble_beacon_bin_tx_plutosdr(gr.top_block):

    def __init__(self):
        gr.top_block.__init__(self, "BLE Beacon BIN TX - PlutoSDR - Channel 38", catch_exceptions=True)
        self.flowgraph_started = threading.Event()

        ##################################################
        # Variables
        ##################################################
        self.waveform_samples = waveform_samples = 200000
        self.tx_attenuation = tx_attenuation = 30
        self.samp_rate = samp_rate = 2000000
        self.center_freq = center_freq = 2426000000

        ##################################################
        # Blocks
        ##################################################

        self.iio_pluto_sink_0 = iio.fmcomms2_sink_fc32('' if '' else iio.get_pluto_uri(), [True, True], 32768, False)
        self.iio_pluto_sink_0.set_len_tag_key("")
        self.iio_pluto_sink_0.set_bandwidth(samp_rate)
        self.iio_pluto_sink_0.set_frequency(center_freq)
        self.iio_pluto_sink_0.set_samplerate(samp_rate)
        self.iio_pluto_sink_0.set_attenuation(0, tx_attenuation)
        self.iio_pluto_sink_0.set_filter_params('Auto', '', 0, 0)
        self.blocks_file_source_0 = blocks.file_source(gr.sizeof_gr_complex*1, 'ble_beacon_localname_radiomaster.bin', True, 0, waveform_samples)
        self.blocks_file_source_0.set_begin_tag(pmt.PMT_NIL)


        ##################################################
        # Connections
        ##################################################
        self.connect((self.blocks_file_source_0, 0), (self.iio_pluto_sink_0, 0))


    def get_waveform_samples(self):
        return self.waveform_samples

    def set_waveform_samples(self, waveform_samples):
        self.waveform_samples = waveform_samples

    def get_tx_attenuation(self):
        return self.tx_attenuation

    def set_tx_attenuation(self, tx_attenuation):
        self.tx_attenuation = tx_attenuation
        self.iio_pluto_sink_0.set_attenuation(0,self.tx_attenuation)

    def get_samp_rate(self):
        return self.samp_rate

    def set_samp_rate(self, samp_rate):
        self.samp_rate = samp_rate
        self.iio_pluto_sink_0.set_bandwidth(self.samp_rate)
        self.iio_pluto_sink_0.set_samplerate(self.samp_rate)

    def get_center_freq(self):
        return self.center_freq

    def set_center_freq(self, center_freq):
        self.center_freq = center_freq
        self.iio_pluto_sink_0.set_frequency(self.center_freq)




def main(top_block_cls=ble_beacon_bin_tx_plutosdr, options=None):
    tb = top_block_cls()

    def sig_handler(sig=None, frame=None):
        tb.stop()
        tb.wait()

        sys.exit(0)

    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    tb.start()
    tb.flowgraph_started.set()

    try:
        input('Press Enter to quit: ')
    except EOFError:
        pass
    tb.stop()
    tb.wait()


if __name__ == '__main__':
    main()
