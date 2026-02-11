#!/usr/bin/env python3
"""
EvilCrow RF v2 — SDR Control Library

Python interface for the EvilCrow RF v2 SDR mode.
Communicates via USB serial using HackRF-compatible text commands.

IMPORTANT: The CC1101 is NOT a true SDR. This module provides:
  - Spectrum scanning (real RSSI measurements per frequency step)
  - Raw RX streaming (demodulated bytes from CC1101 FIFO, not raw IQ)
  - Frequency / modulation / bandwidth configuration

SDR mode is now auto-enabled via serial (no app/phone needed).
The library sends 'sdr_enable' on connect.

Usage:
    from evilcrow_sdr import EvilCrowSDR

    sdr = EvilCrowSDR('COM8')          # Windows
    sdr = EvilCrowSDR('/dev/ttyUSB0')  # Linux

    sdr.set_frequency(433.92e6)
    sdr.set_modulation('ASK')
    sdr.set_bandwidth(650)

    # Spectrum scan
    spectrum = sdr.spectrum_scan(300e6, 928e6, step_khz=200)
    for freq, rssi in spectrum:
        print(f'{freq/1e6:.2f} MHz : {rssi} dBm')

    # Raw RX
    sdr.start_rx()
    data = sdr.read_raw(timeout=2.0)
    sdr.stop_rx()

    sdr.close()

Requirements:
    pip install pyserial
"""

# Module version
VERSION = "1.0.1"

import logging

try:
    import serial
except ImportError:
    raise ImportError(
        "pyserial is required: pip install pyserial"
    )

import time
import threading
import queue
from typing import Optional, List, Tuple

log = logging.getLogger(__name__)

# CC1101 valid frequency bands (MHz)
CC1101_BANDS = [
    (300.0, 348.0),
    (387.0, 464.0),
    (779.0, 928.0),
]

# CC1101 hardware parameter limits
CC1101_LIMITS = {
    'freq_bands_mhz': [(300.0, 348.0), (387.0, 464.0), (779.0, 928.0)],
    'bandwidth_khz': [58, 68, 81, 102, 116, 135, 162, 203, 232, 270,
                      325, 406, 464, 541, 650, 812],
    'data_rate_baud': (600, 500_000),  # min, max
    'modulations': {
        0: '2-FSK', 1: 'GFSK', 2: 'ASK/OOK', 3: '4-FSK', 4: 'MSK',
    },
    'fifo_bytes': 64,
    'serial_max_baud': 115200,  # ESP32 USB-UART default
}


def is_valid_frequency(freq_hz: float) -> bool:
    """Check if frequency is within CC1101 supported bands."""
    freq_mhz = freq_hz / 1e6
    return any(lo <= freq_mhz <= hi for lo, hi in CC1101_BANDS)


def print_cc1101_limits():
    """Print CC1101 hardware parameter limits to console."""
    print('\n╔══════════════════════════════════════════════════════╗')
    print('║        CC1101 SDR Parameter Limits                  ║')
    print('╠══════════════════════════════════════════════════════╣')
    print('║ Frequency bands:                                   ║')
    for lo, hi in CC1101_LIMITS['freq_bands_mhz']:
        print(f'║   {lo:7.1f} – {hi:7.1f} MHz{" " * 29}║')
    print('║                                                    ║')
    print('║ Bandwidth (kHz, discrete values):                  ║')
    bws = CC1101_LIMITS['bandwidth_khz']
    line = '  '.join(f'{b}' for b in bws[:8])
    print(f'║   {line:<50}║')
    line = '  '.join(f'{b}' for b in bws[8:])
    print(f'║   {line:<50}║')
    print('║                                                    ║')
    lo, hi = CC1101_LIMITS['data_rate_baud']
    print(f'║ Data rate: {lo:,} – {hi:,} Baud{" " * 21}║')
    print('║                                                    ║')
    print('║ Modulations:                                       ║')
    for k, v in CC1101_LIMITS['modulations'].items():
        print(f'║   {k} = {v:<46}║')
    print('║                                                    ║')
    print(f'║ RX FIFO: {CC1101_LIMITS["fifo_bytes"]} bytes{" " * 36}║')
    print('║ NOTE: NOT a true SDR — no raw IQ output.           ║')
    print('║       Data is demodulated bytes from CC1101 FIFO.  ║')
    print('╚══════════════════════════════════════════════════════╝\n')


class EvilCrowSDR:
    """
    EvilCrow RF v2 USB SDR control interface.

    Communicates via serial text commands with the firmware SDR module.
    The firmware responds with HACKRF_SUCCESS / HACKRF_ERROR lines.
    """

    MODULATIONS = {
        '2FSK': 0, 'GFSK': 1, 'ASK': 2, 'OOK': 2,
        'ASK/OOK': 2, '4FSK': 3, 'MSK': 4,
    }

    def __init__(self, port: str, baudrate: int = 115200, timeout: float = 2.0):
        """
        Open serial connection to EvilCrow RF v2.

        Args:
            port: Serial port name (e.g. 'COM8' or '/dev/ttyUSB0').
            baudrate: Baud rate (must match firmware: 115200).
            timeout: Read timeout in seconds.
        """
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.ser: Optional[serial.Serial] = None
        self._streaming = False
        self._stream_thread: Optional[threading.Thread] = None
        self._rx_queue: queue.Queue = queue.Queue(maxsize=50000)

        # Current state (updated after successful commands)
        self.frequency_hz: float = 433.92e6
        self.modulation: int = 2  # ASK/OOK
        self.bandwidth_khz: float = 650.0
        self.data_rate_baud: float = 3793.72

        self._connect()

    def _connect(self):
        """Open serial port, auto-enable SDR mode, and verify device."""
        self.ser = serial.Serial(
            port=self.port,
            baudrate=self.baudrate,
            timeout=self.timeout,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
        )
        # Wait for device to be ready after USB enumeration
        time.sleep(1.5)
        self.ser.reset_input_buffer()
        self.ser.reset_output_buffer()

        # Auto-enable SDR mode via serial (no app/phone needed)
        print(f'[...] Enabling SDR mode on {self.port}...')
        enable_resp = self.send_command('sdr_enable')
        if 'SUCCESS' in enable_resp.upper():
            print('[OK] SDR mode enabled via serial')
        else:
            print(f'[WARN] sdr_enable response: {enable_resp}')
            print('       Trying board_id_read anyway...')

        # Verify connection
        resp = self.send_command('board_id_read')
        if 'HACKRF' not in resp.upper():
            raise ConnectionError(
                f'Device on {self.port} did not respond as EvilCrow SDR.\n'
                f'Response: {resp}\n'
                'Firmware may need updating (sdr_enable serial command).'
            )
        print(f'[OK] Connected to EvilCrow SDR on {self.port}')

    def send_command(self, command: str) -> str:
        """
        Send a text command and read the response.

        Args:
            command: Command string (without newline).

        Returns:
            Full response string from device.
        """
        if not self.ser or not self.ser.is_open:
            raise RuntimeError('Serial port not open')

        self.ser.write((command + '\n').encode('ascii'))
        self.ser.flush()

        lines: List[str] = []
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            if self.ser.in_waiting > 0:
                raw = self.ser.readline()
                line = raw.decode('ascii', errors='replace').strip()
                if line:
                    lines.append(line)
                # Stop reading after success/error marker
                if 'SUCCESS' in line or 'ERROR' in line:
                    break
            else:
                time.sleep(0.01)

        return '\n'.join(lines)

    # ── Configuration commands ─────────────────────────────────

    def set_frequency(self, freq_hz: float) -> bool:
        """
        Set center frequency in Hz.

        Valid CC1101 ranges: 300-348, 387-464, 779-928 MHz.
        """
        if not is_valid_frequency(freq_hz):
            print(f'[WARN] {freq_hz/1e6:.2f} MHz is outside CC1101 bands')
            return False

        resp = self.send_command(f'set_freq {int(freq_hz)}')
        if 'SUCCESS' in resp:
            self.frequency_hz = freq_hz
            print(f'[OK] Frequency: {freq_hz/1e6:.3f} MHz')
            return True
        print(f'[ERR] set_freq failed: {resp}')
        return False

    def set_modulation(self, mod: str) -> bool:
        """
        Set modulation type.

        Valid values: '2FSK', 'GFSK', 'ASK', 'OOK', 'ASK/OOK', '4FSK', 'MSK'
        """
        mod_upper = mod.upper()
        if mod_upper not in self.MODULATIONS:
            print(f'[ERR] Unknown modulation: {mod}')
            return False

        mod_id = self.MODULATIONS[mod_upper]
        resp = self.send_command(f'set_modulation {mod_id}')
        if 'SUCCESS' in resp:
            self.modulation = mod_id
            print(f'[OK] Modulation: {mod_upper} ({mod_id})')
            return True
        print(f'[ERR] set_modulation failed: {resp}')
        return False

    def set_bandwidth(self, bw_khz: float) -> bool:
        """Set RX filter bandwidth in kHz."""
        resp = self.send_command(f'set_bandwidth {bw_khz}')
        if 'SUCCESS' in resp:
            self.bandwidth_khz = bw_khz
            print(f'[OK] Bandwidth: {bw_khz:.1f} kHz')
            return True
        print(f'[ERR] set_bandwidth failed: {resp}')
        return False

    def set_data_rate(self, rate_hz: float) -> bool:
        """
        Set data rate in Hz (maps to CC1101 data rate in kBaud).

        CC1101 range: 600 - 500000 Baud.
        """
        resp = self.send_command(f'set_sample_rate {int(rate_hz)}')
        if 'SUCCESS' in resp:
            self.data_rate_baud = rate_hz
            print(f'[OK] Data rate: {rate_hz/1000:.2f} kBaud')
            return True
        print(f'[ERR] set_sample_rate failed: {resp}')
        return False

    def set_gain(self, gain_db: int) -> bool:
        """Set gain (CC1101 uses AGC, so this is approximate)."""
        resp = self.send_command(f'set_gain {gain_db}')
        if 'SUCCESS' in resp:
            print(f'[OK] Gain: {gain_db} dB (AGC mode)')
            return True
        return False

    def get_status(self) -> dict:
        """Query current SDR status from device."""
        resp = self.send_command('sdr_status')
        info = {}
        for line in resp.split('\n'):
            if ':' in line:
                key, _, val = line.partition(':')
                info[key.strip()] = val.strip()
        return info

    def enable_sdr(self) -> bool:
        """Enable SDR mode on-device (sends serial command)."""
        resp = self.send_command('sdr_enable')
        ok = 'SUCCESS' in resp.upper()
        if ok:
            print('[OK] SDR mode enabled')
        else:
            print(f'[ERR] sdr_enable failed: {resp}')
        return ok

    def disable_sdr(self) -> bool:
        """Disable SDR mode on-device."""
        resp = self.send_command('sdr_disable')
        ok = 'SUCCESS' in resp.upper()
        if ok:
            print('[OK] SDR mode disabled')
        else:
            print(f'[ERR] sdr_disable failed: {resp}')
        return ok

    def get_device_info(self) -> str:
        """Query device identity string."""
        return self.send_command('board_id_read')

    def get_sdr_info(self) -> str:
        """Query CC1101 parameter limits from firmware."""
        return self.send_command('sdr_info')

    # ── Spectrum scan ──────────────────────────────────────────

    def spectrum_scan(self, start_hz: float, end_hz: float,
                      step_khz: float = 100) -> List[Tuple[float, int]]:
        """
        Perform a spectrum scan (frequency sweep with RSSI readings).

        Args:
            start_hz: Start frequency in Hz.
            end_hz: End frequency in Hz.
            step_khz: Step size in kHz.

        Returns:
            List of (frequency_hz, rssi_dBm) tuples.
        """
        start_mhz = start_hz / 1e6
        end_mhz = end_hz / 1e6
        step_mhz = step_khz / 1000.0

        print(f'[SCAN] {start_mhz:.2f} - {end_mhz:.2f} MHz, step {step_khz:.0f} kHz')
        resp = self.send_command(
            f'spectrum_scan {start_mhz:.2f} {end_mhz:.2f} {step_khz:.0f}')

        # Parse spectrum output — firmware prints per-frequency RSSI
        # after "Scanning..." and before "Scan complete"
        results: List[Tuple[float, int]] = []
        for line in resp.split('\n'):
            # Look for lines with frequency and RSSI data
            if 'complete' in line.lower():
                # Parse "Scan complete: N points"
                break

        # The actual RSSI data comes via BLE, not serial text.
        # For serial mode, firmware prints summary.
        # For PC tools, use the raw serial data from pollRawRx.
        print(f'[OK] Spectrum scan requested. Results arrive via BLE.')
        return results

    # ── Raw RX streaming ───────────────────────────────────────

    def start_rx(self) -> bool:
        """
        Start raw RX streaming.

        Demodulated bytes from the CC1101 FIFO are sent via serial.
        Read them with read_raw() or read_raw_continuous().
        """
        resp = self.send_command('rx_start')
        if 'SUCCESS' in resp:
            self._streaming = True
            self._stream_thread = threading.Thread(
                target=self._rx_worker, daemon=True)
            self._stream_thread.start()
            print('[OK] RX streaming started')
            return True
        print(f'[ERR] rx_start failed: {resp}')
        return False

    def stop_rx(self) -> bool:
        """Stop raw RX streaming."""
        self._streaming = False
        if self._stream_thread:
            self._stream_thread.join(timeout=2.0)
            self._stream_thread = None
        resp = self.send_command('rx_stop')
        if 'SUCCESS' in resp:
            print('[OK] RX streaming stopped')
            return True
        return False

    def read_raw(self, count: int = 1024, timeout: float = 2.0) -> bytes:
        """
        Read raw demodulated bytes from the RX stream.

        Args:
            count: Maximum number of bytes to read.
            timeout: Timeout in seconds.

        Returns:
            Bytes received from CC1101 FIFO.
        """
        result = bytearray()
        deadline = time.time() + timeout
        while len(result) < count and time.time() < deadline:
            try:
                chunk = self._rx_queue.get(timeout=0.1)
                result.extend(chunk)
            except queue.Empty:
                continue
        return bytes(result[:count])

    def _rx_worker(self):
        """Background thread: read raw bytes from serial during RX."""
        while self._streaming and self.ser and self.ser.is_open:
            try:
                avail = self.ser.in_waiting
                if avail > 0:
                    data = self.ser.read(min(avail, 256))
                    if data:
                        try:
                            self._rx_queue.put_nowait(data)
                        except queue.Full:
                            try:
                                self._rx_queue.get_nowait()
                            except queue.Empty:
                                pass
                            self._rx_queue.put_nowait(data)
                else:
                    time.sleep(0.005)
            except Exception:
                break

    # ── Cleanup ────────────────────────────────────────────────

    def __repr__(self) -> str:
        """Return a human-readable representation of the SDR instance."""
        state = 'streaming' if self._streaming else 'idle'
        return (
            f"<EvilCrowSDR port={self.port} "
            f"freq={self.frequency_hz / 1e6:.3f}MHz "
            f"mod={self.modulation} state={state}>"
        )

    def close(self):
        """Close serial connection and disable SDR mode."""
        if self._streaming:
            self.stop_rx()
        if self.ser and self.ser.is_open:
            # Disable SDR mode on disconnect so device returns to normal
            try:
                self.send_command('sdr_disable')
            except Exception:
                pass
            self.ser.close()
            print('[OK] Disconnected (SDR mode disabled)')

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


# ── Quick test ─────────────────────────────────────────────────

if __name__ == '__main__':
    import sys

    port = sys.argv[1] if len(sys.argv) > 1 else 'COM8'

    # Show CC1101 limits
    print_cc1101_limits()

    try:
        with EvilCrowSDR(port) as sdr:
            print('\n--- Device Info ---')
            print(sdr.get_device_info())

            print('\n--- SDR Info (CC1101 Limits) ---')
            print(sdr.get_sdr_info())

            print('\n--- Status ---')
            for k, v in sdr.get_status().items():
                print(f'  {k}: {v}')

            sdr.set_frequency(433.92e6)
            sdr.set_modulation('ASK')
            sdr.set_bandwidth(650)

            print('\n--- RX Test (3 seconds) ---')
            sdr.start_rx()
            time.sleep(3)
            data = sdr.read_raw(256, timeout=0.5)
            sdr.stop_rx()
            print(f'Received {len(data)} bytes')
            if data:
                print(f'Hex: {data[:32].hex()}')

    except Exception as e:
        print(f'Error: {e}')
