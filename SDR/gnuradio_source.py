#!/usr/bin/env python3
"""
EvilCrow RF v2 — GNU Radio Source Block

Provides a gr.sync_block that reads demodulated data from the EvilCrow
RF v2 and outputs it as complex float samples for GNU Radio flowgraphs.

IMPORTANT: The CC1101 outputs demodulated bytes, not raw IQ. The block
converts each byte to a complex sample (amplitude-modulated). This works
well for OOK/ASK signal visualization and basic analysis but does NOT
provide true quadrature data.

Usage in GNU Radio Companion:
    1. Copy this file to your GRC blocks directory or Python path.
    2. In GRC, add a "Python Block" or import directly.
    3. Connect the output to analysis blocks (FFT, waterfall, file sink).

Standalone test:
    python gnuradio_source.py --port COM8 --freq 433.92e6

Requirements:
    pip install pyserial numpy
    GNU Radio 3.8+ (only for GRC integration)
"""

# Module version
VERSION = "1.0.1"

import logging
import numpy as np

try:
    import serial
    import serial.tools.list_ports
except ImportError:
    raise ImportError(
        "pyserial is required: pip install pyserial"
    )

import threading
import queue
import time
import argparse
import sys

log = logging.getLogger(__name__)

# Try to import GNU Radio (optional — standalone mode works without it)
try:
    from gnuradio import gr
    import pmt
    HAS_GNURADIO = True
except ImportError:
    HAS_GNURADIO = False


def find_serial_port() -> str:
    """Auto-detect EvilCrow serial port."""
    for p in serial.tools.list_ports.comports():
        desc = (p.description or '').lower()
        if any(c in desc for c in ['cp210', 'ch340', 'ch9102', 'ftdi']):
            return p.device
        if (p.vid or 0) == 0x10C4:
            return p.device
    ports = serial.tools.list_ports.comports()
    if ports:
        return ports[0].device
    raise RuntimeError('No serial ports found')


class EvilCrowSource:
    """
    Core SDR source that reads from EvilCrow serial.

    Can be used standalone or wrapped in a GNU Radio block.
    """

    def __init__(self, port: str, frequency: float = 433.92e6,
                 modulation: int = 2, baudrate: int = 115200):
        self.port = port
        self.frequency = frequency
        self.modulation = modulation
        self.baudrate = baudrate

        self.ser: serial.Serial = None
        self.connected = False
        self.streaming = False
        self.sample_queue: queue.Queue = queue.Queue(maxsize=20000)
        self._thread: threading.Thread = None

    def connect(self) -> bool:
        """Connect to device, auto-enable SDR mode, and configure."""
        try:
            self.ser = serial.Serial(self.port, self.baudrate, timeout=0.1)
            time.sleep(1.5)
            self.ser.reset_input_buffer()

            # Auto-enable SDR mode via serial (no app/phone needed)
            self.ser.write(b'sdr_enable\n')
            time.sleep(0.5)
            while self.ser.in_waiting:
                self.ser.read(self.ser.in_waiting)

            self.ser.write(b'board_id_read\n')
            time.sleep(0.3)
            resp = b''
            while self.ser.in_waiting:
                resp += self.ser.read(self.ser.in_waiting)

            if b'HACKRF' not in resp.upper():
                print(f'[ERR] Not an EvilCrow SDR device')
                return False

            self.connected = True
            print(f'[OK] Connected to EvilCrow SDR on {self.port}')

            # Apply settings
            self.set_frequency(self.frequency)
            self.set_modulation(self.modulation)

            return True
        except Exception as e:
            print(f'[ERR] Connect failed: {e}')
            return False

    def set_frequency(self, freq_hz: float):
        """Set center frequency."""
        if self.connected:
            self.ser.write(f'set_freq {int(freq_hz)}\n'.encode())
            self.frequency = freq_hz
            time.sleep(0.1)
            self.ser.reset_input_buffer()

    def set_modulation(self, mod: int):
        """Set modulation (0=2FSK, 2=ASK/OOK, etc)."""
        if self.connected:
            self.ser.write(f'set_modulation {mod}\n'.encode())
            self.modulation = mod
            time.sleep(0.1)
            self.ser.reset_input_buffer()

    def set_bandwidth(self, bw_khz: float):
        """Set RX bandwidth."""
        if self.connected:
            self.ser.write(f'set_bandwidth {bw_khz}\n'.encode())
            time.sleep(0.1)
            self.ser.reset_input_buffer()

    def start_streaming(self) -> bool:
        """Start RX and background read thread."""
        if not self.connected:
            return False
        self.ser.write(b'rx_start\n')
        time.sleep(0.2)
        self.ser.reset_input_buffer()
        self.streaming = True
        self._thread = threading.Thread(target=self._read_worker, daemon=True)
        self._thread.start()
        print('[OK] RX streaming started')
        return True

    def stop_streaming(self):
        """Stop RX."""
        self.streaming = False
        if self._thread:
            self._thread.join(timeout=2.0)
        if self.connected:
            self.ser.write(b'rx_stop\n')
            time.sleep(0.1)
        print('[OK] RX streaming stopped')

    def _read_worker(self):
        """Background: read serial bytes and enqueue as complex samples."""
        while self.streaming and self.ser and self.ser.is_open:
            try:
                avail = self.ser.in_waiting
                if avail > 0:
                    data = self.ser.read(min(avail, 512))
                    for b in data:
                        # Convert demodulated byte to complex float.
                        # Normalize 0-255 to -1.0..+1.0 range.
                        sample = complex((b - 128) / 128.0, 0.0)
                        try:
                            self.sample_queue.put_nowait(sample)
                        except queue.Full:
                            try:
                                self.sample_queue.get_nowait()
                            except queue.Empty:
                                pass
                            self.sample_queue.put_nowait(sample)
                else:
                    time.sleep(0.002)
            except Exception:
                break

    def read_samples(self, count: int) -> np.ndarray:
        """Read N complex samples (blocking)."""
        samples = []
        deadline = time.time() + 2.0
        while len(samples) < count and time.time() < deadline:
            try:
                s = self.sample_queue.get(timeout=0.05)
                samples.append(s)
            except queue.Empty:
                continue
        if not samples:
            return np.zeros(count, dtype=np.complex64)
        return np.array(samples[:count], dtype=np.complex64)

    def close(self):
        """Clean up."""
        if self.streaming:
            self.stop_streaming()
        if self.ser and self.ser.is_open:
            self.ser.close()
        print('[OK] Disconnected')


# ── GNU Radio Block ────────────────────────────────────────────

if HAS_GNURADIO:
    class EvilCrowGRSource(gr.sync_block):
        """
        GNU Radio source block for EvilCrow RF v2 SDR.

        Parameters (set via GRC):
            port: Serial port (e.g. COM8, /dev/ttyUSB0)
            frequency: Center frequency in Hz
            modulation: 0=2FSK, 2=ASK/OOK
        """

        def __init__(self, port: str = 'COM8', frequency: float = 433.92e6,
                     modulation: int = 2):
            gr.sync_block.__init__(
                self,
                name='EvilCrow SDR Source',
                in_sig=None,
                out_sig=[np.complex64],
            )

            self.source = EvilCrowSource(port, frequency, modulation)

            # Message ports for runtime control
            self.message_port_register_in(pmt.intern('freq'))
            self.set_msg_handler(pmt.intern('freq'), self._handle_freq)

        def start(self):
            if self.source.connect():
                self.source.start_streaming()
                return True
            return False

        def stop(self):
            self.source.close()
            return True

        def work(self, input_items, output_items):
            out = output_items[0]
            n = len(out)
            samples = self.source.read_samples(n)
            out[:len(samples)] = samples
            if len(samples) < n:
                out[len(samples):] = 0
            return n

        def _handle_freq(self, msg):
            if pmt.is_number(msg):
                self.source.set_frequency(pmt.to_double(msg))


# ── GRC XML block definition ──────────────────────────────────

GRC_BLOCK_XML = """<?xml version="1.0"?>
<block>
  <name>EvilCrow SDR Source</name>
  <key>evilcrow_sdr_source</key>
  <category>[EvilCrow RF]</category>
  <import>from gnuradio_source import EvilCrowGRSource</import>
  <make>EvilCrowGRSource($port, $frequency, $modulation)</make>
  <param>
    <name>Serial Port</name>
    <key>port</key>
    <type>string</type>
    <value>COM8</value>
  </param>
  <param>
    <name>Frequency (Hz)</name>
    <key>frequency</key>
    <type>real</type>
    <value>433.92e6</value>
  </param>
  <param>
    <name>Modulation</name>
    <key>modulation</key>
    <type>int</type>
    <value>2</value>
  </param>
  <source>
    <name>out</name>
    <type>complex</type>
  </source>
  <sink>
    <name>freq</name>
    <type>message</type>
    <optional>1</optional>
  </sink>
  <doc>
EvilCrow RF v2 SDR Source Block.

Reads demodulated data from the CC1101 transceiver via USB serial
and outputs complex float samples. Best for OOK/ASK signals.

Parameters:
  - Serial Port: USB-UART port (e.g. COM8 or /dev/ttyUSB0)
  - Frequency: Center frequency in Hz (CC1101 bands: 300-348, 387-464, 779-928 MHz)
  - Modulation: 0=2FSK, 2=ASK/OOK, 1=GFSK, 3=4FSK, 4=MSK
  </doc>
</block>
"""


# ── Standalone test ────────────────────────────────────────────

def standalone_test(port: str, freq: float, duration: float):
    """Run a standalone capture test (no GNU Radio needed)."""
    print(f'\n=== EvilCrow SDR Standalone Test ===')
    print(f'Port: {port}  Freq: {freq/1e6:.2f} MHz  Duration: {duration}s\n')

    source = EvilCrowSource(port, frequency=freq, modulation=2)
    if not source.connect():
        return

    source.start_streaming()
    time.sleep(duration)

    samples = source.read_samples(2048)
    source.close()

    print(f'\nCaptured {len(samples)} samples')
    if len(samples) > 0:
        power = np.mean(np.abs(samples) ** 2)
        peak = np.max(np.abs(samples))
        print(f'Average power: {power:.6f}')
        print(f'Peak amplitude: {peak:.4f}')

        # Save to file
        outfile = f'capture_{freq/1e6:.0f}MHz.raw'
        samples.tofile(outfile)
        print(f'Saved to {outfile} (complex64 format)')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='EvilCrow RF v2 — GNU Radio SDR Source')
    parser.add_argument('--port', type=str, default=None,
                        help='Serial port (auto-detect if omitted)')
    parser.add_argument('--freq', type=float, default=433.92e6,
                        help='Center frequency in Hz (default: 433.92 MHz)')
    parser.add_argument('--duration', type=float, default=5.0,
                        help='Capture duration in seconds (default: 5)')
    args = parser.parse_args()

    port = args.port or find_serial_port()
    standalone_test(port, args.freq, args.duration)
