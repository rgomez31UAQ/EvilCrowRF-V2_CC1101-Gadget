#!/usr/bin/env python3
"""
EvilCrow RF v2 — SDR Tool Launcher (GUI)

A small tkinter GUI to select the serial port and launch the desired
SDR tool:
  - EvilCrow SDR Library (interactive Python shell)
  - URH Bridge (RTL-TCP server for Universal Radio Hacker)
  - GNU Radio Source (standalone capture test)
  - Spectrum Scanner (quick RSSI sweep)

Also supports direct command-line invocation via --tool flag.

Requirements:
    pip install pyserial numpy
"""

import sys
import os
import threading
import time

# Version info
VERSION = "1.0.2"

# Add current directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import serial
    import serial.tools.list_ports
except ImportError:
    print('ERROR: pyserial not installed. Run: pip install pyserial')
    sys.exit(1)

try:
    import tkinter as tk
    from tkinter import ttk, scrolledtext, messagebox
    HAS_TK = True
except ImportError:
    HAS_TK = False


def list_serial_ports():
    """Return list of (port_name, description) tuples."""
    ports = []
    for p in serial.tools.list_ports.comports():
        desc = p.description or p.device
        ports.append((p.device, f'{p.device} — {desc}'))
    return sorted(ports, key=lambda x: x[0])


def detect_evilcrow_port():
    """Try to find the EvilCrow port automatically."""
    for p in serial.tools.list_ports.comports():
        desc = (p.description or '').lower()
        vid = p.vid or 0
        if any(c in desc for c in ['cp210', 'ch340', 'ch9102', 'ftdi']):
            return p.device
        if vid == 0x10C4:
            return p.device
    return None


# ── CLI Mode ───────────────────────────────────────────────────

def run_cli():
    """Run in command-line mode (no GUI)."""
    import argparse

    parser = argparse.ArgumentParser(
        description='EvilCrow RF v2 — SDR Tool Launcher')
    parser.add_argument('--port', type=str, default=None,
                        help='Serial port (auto-detect if omitted)')
    parser.add_argument('--tool', type=str,
                        choices=['library', 'urh', 'gnuradio', 'spectrum'],
                        help='Tool to launch')
    parser.add_argument('--freq', type=float, default=433.92e6,
                        help='Frequency in Hz (default: 433.92 MHz)')
    parser.add_argument('--tcp-port', type=int, default=1234,
                        help='TCP port for URH bridge (default: 1234)')
    args = parser.parse_args()

    port = args.port or detect_evilcrow_port()
    if not port:
        print('No serial port found. Use --port to specify manually.')
        print('Available ports:')
        for name, desc in list_serial_ports():
            print(f'  {desc}')
        sys.exit(1)

    if args.tool == 'urh':
        from urh_bridge import URHBridge
        bridge = URHBridge(serial_port=port, tcp_port=args.tcp_port)
        bridge.run()
    elif args.tool == 'gnuradio':
        from gnuradio_source import standalone_test
        standalone_test(port, args.freq, 10.0)
    elif args.tool == 'spectrum':
        run_spectrum_scan(port, args.freq)
    elif args.tool == 'library':
        run_library_demo(port, args.freq)
    else:
        print('Please specify --tool (library, urh, gnuradio, spectrum)')


def run_spectrum_scan(port: str, center_freq: float):
    """Quick spectrum scan around center frequency."""
    from evilcrow_sdr import EvilCrowSDR

    with EvilCrowSDR(port) as sdr:
        sdr.set_frequency(center_freq)
        sdr.set_modulation('ASK')
        print(f'\nScanning around {center_freq/1e6:.2f} MHz...')
        status = sdr.get_status()
        for k, v in status.items():
            print(f'  {k}: {v}')


def run_library_demo(port: str, freq: float):
    """Interactive demo with the SDR library."""
    from evilcrow_sdr import EvilCrowSDR

    print('=== EvilCrow SDR Interactive Demo ===')
    with EvilCrowSDR(port) as sdr:
        sdr.set_frequency(freq)
        sdr.set_modulation('ASK')
        sdr.set_bandwidth(650)

        print('\nDevice status:')
        for k, v in sdr.get_status().items():
            print(f'  {k}: {v}')

        print('\nStarting RX for 5 seconds...')
        sdr.start_rx()
        time.sleep(5)
        data = sdr.read_raw(1024, timeout=1.0)
        sdr.stop_rx()

        print(f'Received {len(data)} bytes')
        if data:
            print(f'First 32 bytes: {data[:32].hex()}')


# ── GUI Mode ───────────────────────────────────────────────────

class SDRLauncherGUI:
    """Tkinter GUI for the SDR tool launcher."""

    TOOLS = [
        ('SDR Library Demo', 'library',
         'Interactive test: connect, configure, RX 5s, show data'),
        ('URH Bridge', 'urh',
         'RTL-TCP server for Universal Radio Hacker (localhost:1234)'),
        ('GNU Radio Capture', 'gnuradio',
         'Standalone capture test (saves .raw file)'),
        ('Spectrum Status', 'spectrum',
         'Quick device status and configuration check'),
    ]

    # Common ISM / sub-GHz frequency presets
    FREQ_PRESETS = [
        ('Custom', None),
        ('── ISM Bands ──', None),
        ('315.00 MHz (US ISM)', 315.00),
        ('433.92 MHz (EU ISM)', 433.92),
        ('868.35 MHz (EU ISM)', 868.35),
        ('915.00 MHz (US ISM)', 915.00),
        ('── Car Keys ──', None),
        ('300.00 MHz (Older US)', 300.00),
        ('310.00 MHz (US Toyota)', 310.00),
        ('315.00 MHz (US Standard)', 315.00),
        ('433.92 MHz (EU Standard)', 433.92),
        ('434.42 MHz (EU Alt)', 434.42),
        ('── Garage / Gates ──', None),
        ('300.00 MHz', 300.00),
        ('303.87 MHz (Chamberlain)', 303.87),
        ('315.00 MHz', 315.00),
        ('390.00 MHz (Liftmaster)', 390.00),
        ('418.00 MHz', 418.00),
        ('433.42 MHz (CAME)', 433.42),
        ('433.92 MHz (Nice/BFT)', 433.92),
        ('868.30 MHz (Hörmann)', 868.30),
        ('── Weather / Sensors ──', None),
        ('315.00 MHz (US)', 315.00),
        ('433.92 MHz (EU)', 433.92),
        ('868.00 MHz (EU)', 868.00),
        ('── TPMS ──', None),
        ('315.00 MHz (US TPMS)', 315.00),
        ('433.92 MHz (EU TPMS)', 433.92),
        ('── RC / LoRa ──', None),
        ('27.12 MHz (CB/RC)', 27.12),
        ('868.10 MHz (LoRa EU)', 868.10),
        ('915.00 MHz (LoRa US)', 915.00),
    ]

    def __init__(self):
        self.root = tk.Tk()
        self.root.title(f'EvilCrow RF v2 — SDR Launcher v{VERSION}')
        self.root.geometry('720x560')
        self.root.resizable(False, False)
        self.root.configure(bg='#0a0f0a')

        self._running_thread = None
        self._build_ui()

    def _build_ui(self):
        # Style
        style = ttk.Style()
        style.theme_use('clam')
        style.configure('TFrame', background='#0a0f0a')
        style.configure('TLabel', background='#0a0f0a', foreground='#00E676',
                        font=('Consolas', 10))
        style.configure('Header.TLabel', font=('Consolas', 14, 'bold'),
                        foreground='#00E676', background='#0a0f0a')
        style.configure('TButton', font=('Consolas', 10),
                        background='#1a2f1a', foreground='#00E676')
        style.configure('TCombobox', font=('Consolas', 10))
        style.configure('Tool.TRadiobutton', background='#0a0f0a',
                        foreground='#b0e0b0', font=('Consolas', 10))

        main = ttk.Frame(self.root, padding=16)
        main.pack(fill=tk.BOTH, expand=True)

        # Header
        ttk.Label(main, text='EvilCrow RF v2 — SDR Tools',
                  style='Header.TLabel').pack(anchor=tk.W)
        ttk.Label(main, text='Select port and tool, then click Launch.',
                  foreground='#6a9a6a').pack(anchor=tk.W, pady=(2, 10))

        # Port selection
        port_frame = ttk.Frame(main)
        port_frame.pack(fill=tk.X, pady=(0, 8))

        ttk.Label(port_frame, text='Serial Port:').pack(side=tk.LEFT)

        self.port_var = tk.StringVar()
        self.port_combo = ttk.Combobox(
            port_frame, textvariable=self.port_var,
            width=40, state='readonly')
        self.port_combo.pack(side=tk.LEFT, padx=(8, 4))

        refresh_btn = ttk.Button(port_frame, text='↻ Refresh',
                                 command=self._refresh_ports, width=10)
        refresh_btn.pack(side=tk.LEFT)

        # Frequency with presets
        freq_frame = ttk.Frame(main)
        freq_frame.pack(fill=tk.X, pady=(0, 8))

        ttk.Label(freq_frame, text='Preset:').pack(side=tk.LEFT)
        self.preset_var = tk.StringVar(value='Custom')
        preset_names = [name for name, _ in self.FREQ_PRESETS]
        self.preset_combo = ttk.Combobox(
            freq_frame, textvariable=self.preset_var,
            values=preset_names, width=30, state='readonly')
        self.preset_combo.pack(side=tk.LEFT, padx=(8, 16))
        self.preset_combo.bind('<<ComboboxSelected>>', self._on_preset_change)

        ttk.Label(freq_frame, text='Freq (MHz):').pack(side=tk.LEFT)
        self.freq_var = tk.StringVar(value='433.92')
        self.freq_entry = ttk.Entry(freq_frame, textvariable=self.freq_var,
                                    width=12)
        self.freq_entry.pack(side=tk.LEFT, padx=8)

        ttk.Label(freq_frame, text='TCP Port (URH):',
                  foreground='#6a9a6a').pack(side=tk.LEFT, padx=(8, 0))
        self.tcp_var = tk.StringVar(value='1234')
        tcp_entry = ttk.Entry(freq_frame, textvariable=self.tcp_var, width=8)
        tcp_entry.pack(side=tk.LEFT, padx=8)

        # Tool selection (checkboxes for multi-tool)
        ttk.Label(main, text='Tools (select one or more):').pack(
            anchor=tk.W, pady=(4, 2))

        self.tool_vars = {}
        for label, value, desc in self.TOOLS:
            var = tk.BooleanVar(value=(value == 'library'))
            self.tool_vars[value] = var
            cb = ttk.Checkbutton(main, text=f'{label}  \u2014  {desc}',
                                 variable=var,
                                 style='Tool.TRadiobutton')
            cb.pack(anchor=tk.W, padx=16, pady=1)

        # Buttons
        btn_frame = ttk.Frame(main)
        btn_frame.pack(fill=tk.X, pady=(10, 4))

        self.launch_btn = ttk.Button(btn_frame, text='▶  Launch',
                                     command=self._launch)
        self.launch_btn.pack(side=tk.LEFT)

        self.stop_btn = ttk.Button(btn_frame, text='■  Stop',
                                   command=self._stop, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT, padx=8)

        ttk.Button(btn_frame, text='📋 CC1101 Info',
                   command=self._show_cc1101_info).pack(side=tk.LEFT, padx=8)

        ttk.Button(btn_frame, text='📦 Install Deps',
                   command=self._install_deps).pack(side=tk.LEFT, padx=8)

        ttk.Button(btn_frame, text='Clear Log',
                   command=self._clear_log).pack(side=tk.RIGHT)

        # Output log
        self.log_text = scrolledtext.ScrolledText(
            main, height=12, bg='#050a05', fg='#00E676',
            font=('Consolas', 9), insertbackground='#00E676',
            state=tk.DISABLED, wrap=tk.WORD)
        self.log_text.pack(fill=tk.BOTH, expand=True, pady=(4, 0))

        # Initial port refresh
        self._refresh_ports()

    def _on_preset_change(self, event=None):
        """Update frequency entry when a preset is selected."""
        sel = self.preset_var.get()
        for name, freq in self.FREQ_PRESETS:
            if name == sel and freq is not None:
                self.freq_var.set(f'{freq:.2f}')
                break

    def _refresh_ports(self):
        """Scan for serial ports and populate combobox."""
        ports = list_serial_ports()
        display = [desc for _, desc in ports]
        values = [name for name, _ in ports]

        self.port_combo['values'] = display
        self._port_map = dict(zip(display, values))

        # Try auto-detect
        auto = detect_evilcrow_port()
        if auto:
            for i, (name, _) in enumerate(ports):
                if name == auto:
                    self.port_combo.current(i)
                    break
        elif display:
            self.port_combo.current(0)

    def _get_selected_port(self) -> str:
        """Get actual port name from combobox selection."""
        sel = self.port_var.get()
        return self._port_map.get(sel, sel)

    def _log(self, msg: str):
        """Append message to log widget (thread-safe)."""
        def _append():
            self.log_text.config(state=tk.NORMAL)
            self.log_text.insert(tk.END,
                                 f'[{time.strftime("%H:%M:%S")}] {msg}\n')
            self.log_text.see(tk.END)
            self.log_text.config(state=tk.DISABLED)
        self.root.after(0, _append)

    def _clear_log(self):
        self.log_text.config(state=tk.NORMAL)
        self.log_text.delete('1.0', tk.END)
        self.log_text.config(state=tk.DISABLED)

    def _install_deps(self):
        """Install Python dependencies from requirements.txt."""
        self._log('Installing dependencies (pyserial, numpy)...')

        def _do_install():
            import subprocess
            req_file = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    'requirements.txt')
            try:
                result = subprocess.run(
                    [sys.executable, '-m', 'pip', 'install', '-r', req_file],
                    capture_output=True, text=True, timeout=120)
                if result.returncode == 0:
                    self._log('[OK] Dependencies installed successfully.')
                    for line in result.stdout.strip().split('\n'):
                        if line.strip():
                            self._log(f'  {line.strip()}')
                else:
                    self._log(f'[ERR] pip install failed (code {result.returncode})')
                    for line in result.stderr.strip().split('\n'):
                        if line.strip():
                            self._log(f'  {line.strip()}')
            except FileNotFoundError:
                self._log('[ERR] Python/pip not found. Install Python 3.8+.')
            except subprocess.TimeoutExpired:
                self._log('[ERR] Installation timed out.')
            except Exception as e:
                self._log(f'[ERR] Install failed: {e}')

        threading.Thread(target=_do_install, daemon=True).start()

    def _show_cc1101_info(self):
        """Show CC1101 parameter limits in a popup and log."""
        info = (
            'CC1101 SDR Parameter Limits\n'
            '─────────────────────────────────────\n'
            'Frequency bands:\n'
            '  300.0 – 348.0 MHz\n'
            '  387.0 – 464.0 MHz\n'
            '  779.0 – 928.0 MHz\n'
            '\n'
            'Bandwidth (kHz, discrete):\n'
            '  58  68  81  102  116  135  162  203\n'
            '  232  270  325  406  464  541  650  812\n'
            '\n'
            'Data rate: 600 – 500,000 Baud\n'
            '\n'
            'Modulations:\n'
            '  0 = 2-FSK\n'
            '  1 = GFSK\n'
            '  2 = ASK/OOK\n'
            '  3 = 4-FSK\n'
            '  4 = MSK\n'
            '\n'
            'RX FIFO: 64 bytes\n'
            '\n'
            'NOTE: The CC1101 is NOT a true SDR.\n'
            'Data is demodulated bytes from the CC1101 FIFO,\n'
            'not raw IQ samples. Best used with URH for\n'
            'protocol analysis and signal detection.'
        )
        messagebox.showinfo('CC1101 SDR Info', info)
        self._log('[INFO] CC1101 parameter limits shown.')

    def _launch(self):
        """Launch selected tool(s) in background thread(s)."""
        port = self._get_selected_port()
        if not port:
            messagebox.showwarning('No Port', 'Select a serial port first.')
            return

        # Collect selected tools
        selected = [key for key, var in self.tool_vars.items() if var.get()]
        if not selected:
            messagebox.showwarning('No Tool', 'Select at least one tool.')
            return

        freq = float(self.freq_var.get()) * 1e6
        tcp_port = int(self.tcp_var.get())

        self.launch_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)

        self._stop_flag = False
        self._running_threads = []

        for tool in selected:
            t = threading.Thread(
                target=self._run_tool, args=(tool, port, freq, tcp_port),
                daemon=True)
            t.start()
            self._running_threads.append(t)

        # Monitor threads in background
        threading.Thread(target=self._monitor_threads, daemon=True).start()

    def _stop(self):
        """Request stop for running tool."""
        self._stop_flag = True
        self._log('Stop requested...')
        self.stop_btn.config(state=tk.DISABLED)

    def _run_tool(self, tool: str, port: str, freq: float, tcp_port: int):
        """Run the selected tool (called in background thread)."""
        # Use per-thread stdout redirect
        old_stdout = sys.stdout
        sys.stdout = _LogWriter(self._log)

        try:
            if tool == 'library':
                self._log(f'[Library] Starting on {port}...')
                run_library_demo(port, freq)

            elif tool == 'urh':
                self._log(f'[URH] Starting bridge on {port}, TCP :{tcp_port}...')
                from urh_bridge import URHBridge
                bridge = URHBridge(serial_port=port, tcp_port=tcp_port)
                if not bridge.connect_device():
                    self._log('[URH] Failed to connect device.')
                    return
                if not bridge.start_server():
                    self._log('[URH] Failed to start TCP server.')
                    return
                self._log('[URH] Bridge running. Connect from URH...')
                bridge.server.settimeout(1.0)
                while not self._stop_flag:
                    try:
                        client, addr = bridge.server.accept()
                        bridge.handle_client(client, addr)
                    except socket.timeout:
                        continue
                    except Exception as e:
                        self._log(f'[URH] Accept error: {e}')
                        break
                bridge.cleanup()

            elif tool == 'gnuradio':
                self._log(f'[GnuRadio] Starting capture on {port}...')
                from gnuradio_source import standalone_test
                standalone_test(port, freq, 10.0)

            elif tool == 'spectrum':
                self._log(f'[Spectrum] Checking status on {port}...')
                run_spectrum_scan(port, freq)

        except Exception as e:
            self._log(f'[{tool}] ERROR: {e}')
        finally:
            sys.stdout = old_stdout
            self._log(f'[{tool}] Finished.')

    def _on_tool_done(self):
        self.launch_btn.config(state=tk.NORMAL)
        self.stop_btn.config(state=tk.DISABLED)

    def _monitor_threads(self):
        """Wait for all running threads to finish, then re-enable launch."""
        for t in self._running_threads:
            t.join()
        self.root.after(0, self._on_tool_done)

    def run(self):
        self.root.mainloop()


class _LogWriter:
    """Redirect print() calls to GUI log."""

    def __init__(self, log_fn):
        self._log = log_fn
        self._buf = ''

    def write(self, s):
        self._buf += s
        while '\n' in self._buf:
            line, self._buf = self._buf.split('\n', 1)
            if line.strip():
                self._log(line)

    def flush(self):
        if self._buf.strip():
            self._log(self._buf.strip())
            self._buf = ''


# ── Entry Point ────────────────────────────────────────────────

import socket  # needed by URH tool in thread

if __name__ == '__main__':
    # If command-line args provided, run in CLI mode
    if len(sys.argv) > 1 and '--tool' in sys.argv:
        run_cli()
    elif HAS_TK:
        app = SDRLauncherGUI()
        app.run()
    else:
        print('tkinter not available. Use --tool flag for CLI mode:')
        print('  python sdr_launcher.py --tool library --port COM8')
        print('  python sdr_launcher.py --tool urh --port COM8')
        print('  python sdr_launcher.py --tool gnuradio --port COM8')
        run_cli()
