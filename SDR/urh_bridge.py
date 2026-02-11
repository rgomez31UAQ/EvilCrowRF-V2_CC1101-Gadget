#!/usr/bin/env python3
"""
EvilCrow RF v2 — URH Bridge (RTL-TCP compatible)

Acts as an RTL-TCP server so Universal Radio Hacker (URH) can connect
to the EvilCrow RF v2 as if it were an RTL-SDR dongle.

How it works:
  1. Connects to EvilCrow via USB serial.
  2. Starts a TCP server on localhost:1234.
  3. Sends the RTL-TCP DongleInfo header when URH connects.
  4. Translates RTL-TCP commands (set_freq, set_rate, set_gain) to
     EvilCrow serial commands.
  5. Reads raw demodulated bytes from CC1101 FIFO and streams them
     as 8-bit unsigned IQ samples to URH.

IMPORTANT: The CC1101 does NOT produce raw IQ data. The bytes streamed
are demodulated FIFO data, not true I/Q. URH will display signal
activity but spectral analysis will be limited. This is useful for:
  - Seeing when signals are present
  - Recording demodulated data for protocol analysis
  - Basic signal detection and timing analysis

Usage:
    python urh_bridge.py                  # Auto-detect port
    python urh_bridge.py --port COM8      # Specify serial port
    python urh_bridge.py --tcp-port 1235  # Custom TCP port

Then in URH: File > New Project > "RTL-TCP" source > localhost:1234

Requirements:
    pip install pyserial
"""

# Module version
VERSION = "1.0.1"

import logging
import socket

try:
    import serial
    import serial.tools.list_ports
except ImportError:
    raise ImportError(
        "pyserial is required: pip install pyserial"
    )

import struct
import threading
import time
import sys
import argparse
import select

log = logging.getLogger(__name__)


def find_evilcrow_port() -> str:
    """Auto-detect EvilCrow serial port (CP2102 / CH340 USB-UART)."""
    ports = serial.tools.list_ports.comports()
    for p in ports:
        desc = (p.description or '').lower()
        vid = p.vid or 0
        # Common USB-UART chips used with ESP32
        if any(chip in desc for chip in ['cp210', 'ch340', 'ch9102', 'ftdi']):
            return p.device
        # Silicon Labs CP2102 (common on ESP32 devboards)
        if vid == 0x10C4:
            return p.device
    # Fallback: return first port
    if ports:
        return ports[0].device
    raise RuntimeError('No serial ports found. Is the device connected?')


class URHBridge:
    """RTL-TCP compatible bridge for EvilCrow RF v2."""

    def __init__(self, serial_port: str, tcp_port: int = 1234):
        self.serial_port = serial_port
        self.tcp_port = tcp_port
        self.ser: serial.Serial = None
        self.server: socket.socket = None
        self.client: socket.socket = None
        self.running = False

    def log(self, msg: str):
        print(f'[{time.strftime("%H:%M:%S")}] {msg}')

    def connect_device(self) -> bool:
        """Open serial connection to EvilCrow and enable SDR mode."""
        try:
            self.log(f'Connecting to {self.serial_port}...')
            self.ser = serial.Serial(self.serial_port, 115200, timeout=0.1)
            time.sleep(1.5)
            self.ser.reset_input_buffer()

            # Auto-enable SDR mode via serial (no app/phone needed)
            self.log('Enabling SDR mode via serial...')
            self.ser.write(b'sdr_enable\n')
            time.sleep(0.5)
            resp = b''
            while self.ser.in_waiting:
                resp += self.ser.read(self.ser.in_waiting)
            resp_str = resp.decode('ascii', errors='replace')

            if 'SUCCESS' in resp_str.upper():
                self.log('SDR mode enabled.')
            else:
                self.log(f'sdr_enable response: {resp_str.strip()}')
                self.log('Trying board_id_read anyway...')

            # Verify device
            self.ser.write(b'board_id_read\n')
            time.sleep(0.3)
            resp = b''
            while self.ser.in_waiting:
                resp += self.ser.read(self.ser.in_waiting)
            resp_str = resp.decode('ascii', errors='replace')

            if 'HACKRF' not in resp_str.upper():
                self.log(f'Device did not respond as EvilCrow SDR.')
                self.log(f'Response: {resp_str.strip()}')
                self.log('Firmware may need updating (sdr_enable support).')
                return False

            self.log('Device connected and SDR mode verified.')

            # Set initial config
            self.ser.write(b'set_freq 433920000\n')
            time.sleep(0.1)
            self.ser.write(b'set_sample_rate 250000\n')
            time.sleep(0.1)
            self.ser.write(b'set_gain 15\n')
            time.sleep(0.1)
            # Drain responses
            self.ser.reset_input_buffer()

            return True
        except Exception as e:
            self.log(f'Connection failed: {e}')
            return False

    def start_server(self) -> bool:
        """Start TCP server for URH connections."""
        try:
            self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.server.bind(('127.0.0.1', self.tcp_port))
            self.server.listen(1)
            self.log(f'TCP server listening on 127.0.0.1:{self.tcp_port}')
            self.log('In URH: File > New > "RTL-TCP" source > localhost:{}'.format(
                self.tcp_port))
            return True
        except Exception as e:
            self.log(f'Server start failed: {e}')
            return False

    def handle_rtl_command(self, data: bytes):
        """Handle RTL-TCP 5-byte commands from URH."""
        if len(data) < 5:
            return
        cmd = data[0]
        param = struct.unpack('>I', data[1:5])[0]

        if cmd == 0x01:  # Set frequency
            self.log(f'  Freq: {param} Hz ({param/1e6:.3f} MHz)')
            self.ser.write(f'set_freq {param}\n'.encode())
        elif cmd == 0x02:  # Set sample rate
            self.log(f'  Rate: {param} Hz')
            self.ser.write(f'set_sample_rate {param}\n'.encode())
        elif cmd == 0x04:  # Set gain
            gain = param // 10
            self.log(f'  Gain: {gain} dB')
            self.ser.write(f'set_gain {gain}\n'.encode())
        elif cmd == 0x05:  # Set gain mode (auto/manual)
            pass  # CC1101 always uses AGC
        else:
            self.log(f'  Unknown RTL cmd: 0x{cmd:02X} param={param}')

    def stream_data(self):
        """Read CC1101 FIFO data and stream to URH as 8-bit unsigned IQ."""
        self.log('Starting RX and data stream...')
        self.ser.write(b'rx_start\n')
        time.sleep(0.2)
        self.ser.reset_input_buffer()

        sample_count = 0
        last_log = time.time()

        try:
            while self.running and self.client:
                # Read available serial data from CC1101 FIFO
                avail = self.ser.in_waiting
                if avail > 0:
                    raw = self.ser.read(min(avail, 512))
                    if raw:
                        # Convert demodulated bytes to unsigned 8-bit IQ pairs.
                        # The CC1101 outputs demodulated bytes, not true IQ.
                        # We send each byte as both I and Q (mono signal).
                        iq_buf = bytearray(len(raw) * 2)
                        for i, b in enumerate(raw):
                            iq_buf[i * 2] = b       # I channel
                            iq_buf[i * 2 + 1] = 127  # Q channel (DC center)
                        try:
                            self.client.sendall(iq_buf)
                            sample_count += len(raw)
                        except (BrokenPipeError, OSError):
                            self.log('Client disconnected during stream.')
                            break
                else:
                    # No data available — send silence (center value) to keep
                    # URH's sample rate clock ticking
                    silence = bytes([127, 127]) * 64  # 64 silent samples
                    try:
                        self.client.sendall(silence)
                    except (BrokenPipeError, OSError):
                        break
                    time.sleep(0.005)

                # Log progress every 5 seconds
                now = time.time()
                if now - last_log >= 5.0:
                    self.log(f'  Streamed {sample_count} samples')
                    last_log = now

        finally:
            try:
                self.ser.write(b'rx_stop\n')
                time.sleep(0.1)
            except Exception:
                pass
            self.log(f'Stream stopped ({sample_count} total samples)')

    def handle_client(self, client: socket.socket, addr):
        """Handle a URH client connection."""
        self.client = client
        self.log(f'URH connected from {addr[0]}:{addr[1]}')

        try:
            client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

            # Send RTL-TCP DongleInfo header (12 bytes)
            # Magic: "RTL0" | Tuner type: uint32 | Gain count: uint32
            header = b'RTL0' + struct.pack('>II', 1, 1)
            client.sendall(header)
            self.log('Sent RTL-TCP header (12 bytes)')

            time.sleep(0.1)

            # Start streaming in background thread
            self.running = True
            stream_t = threading.Thread(target=self.stream_data, daemon=True)
            stream_t.start()

            # Handle commands from URH in main loop
            while self.running:
                try:
                    ready, _, _ = select.select([client], [], [], 1.0)
                    if ready:
                        data = client.recv(1024)
                        if not data:
                            break
                        # Process 5-byte RTL-TCP commands
                        for i in range(0, len(data) - 4, 5):
                            self.handle_rtl_command(data[i:i + 5])
                    if not stream_t.is_alive():
                        break
                except (ConnectionResetError, OSError):
                    break

        finally:
            self.running = False
            if stream_t.is_alive():
                stream_t.join(timeout=2.0)
            try:
                client.close()
            except Exception:
                pass
            self.client = None
            self.log('URH disconnected — ready for next connection.')

    def run(self):
        """Main entry point: connect device, start server, accept clients."""
        if not self.connect_device():
            return False
        if not self.start_server():
            return False

        self.log('Bridge ready. Waiting for URH...')
        try:
            while True:
                client, addr = self.server.accept()
                self.handle_client(client, addr)
                self.log('Ready for next connection...')
        except KeyboardInterrupt:
            self.log('Shutting down.')
        finally:
            self.cleanup()

    def cleanup(self):
        self.running = False
        if self.client:
            try:
                self.client.close()
            except Exception:
                pass
        if self.server:
            try:
                self.server.close()
            except Exception:
                pass
        if self.ser and self.ser.is_open:
            try:
                self.ser.write(b'rx_stop\n')
                self.ser.close()
            except Exception:
                pass
        self.log('Cleanup done.')


def main():
    parser = argparse.ArgumentParser(
        description='EvilCrow RF v2 — URH Bridge (RTL-TCP compatible)')
    parser.add_argument('--port', type=str, default=None,
                        help='Serial port (auto-detect if omitted)')
    parser.add_argument('--tcp-port', type=int, default=1234,
                        help='TCP server port (default: 1234)')
    args = parser.parse_args()

    port = args.port or find_evilcrow_port()
    bridge = URHBridge(serial_port=port, tcp_port=args.tcp_port)
    bridge.run()


if __name__ == '__main__':
    main()
