#!/usr/bin/env python3
"""
EvilCrow SDR — Executable Builder (GUI)

Modern tkinter GUI tool to build a standalone .exe from sdr_launcher.py
using PyInstaller.  Works from a virtualenv, system Python, or a
frozen environment.  Automatically resolves paths relative to this
script — no hard-coded paths anywhere.

Features
--------
* Auto-detect / install PyInstaller (asks user first).
* Auto-detect / download UPX compressor (asks user first).
* Read & bump version from sdr_launcher.py.
* Read module versions from all SDR .py files.
* Auto-find the project icon (evilcrow.ico).
* Full build log with colour-coded output.
* Dark-theme UI with clear sections and progress bar.

Requirements
------------
    pip install pyserial numpy          # runtime deps
    pip install pyinstaller             # build dep  (auto-installed)
    (optional) UPX in PATH or local     # compression (auto-installed)
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import ttk, scrolledtext, filedialog, messagebox
from typing import Dict, List, Optional, Tuple
import urllib.request
import zipfile
import io

# ── Constants ──────────────────────────────────────────────────

BUILDER_VERSION = "2.0.0"

# Directories — always relative to *this* script
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SDR_DIR = SCRIPT_DIR  # alias for clarity

# Modules whose VERSION constant we track
MODULE_FILES: List[str] = [
    "sdr_launcher.py",
    "evilcrow_sdr.py",
    "gnuradio_source.py",
    "urh_bridge.py",
]

ICON_FILENAME = "evilcrow.ico"

# UPX download URL template (GitHub releases)
UPX_VERSION = "4.2.4"
UPX_WIN_URL = (
    f"https://github.com/upx/upx/releases/download/v{UPX_VERSION}/"
    f"upx-{UPX_VERSION}-win64.zip"
)
UPX_LOCAL_DIR = os.path.join(SDR_DIR, "upx")


# ── Utility helpers ────────────────────────────────────────────

def _python_exe() -> str:
    """Return the Python interpreter path (works inside venv too)."""
    return sys.executable


def read_module_version(filepath: str) -> Optional[str]:
    """
    Read ``VERSION = "x.y.z"`` from a Python source file.

    Returns None if not found.
    """
    try:
        with open(filepath, "r", encoding="utf-8") as fh:
            for line in fh:
                m = re.match(r'^VERSION\s*=\s*["\']([^"\']+)["\']', line.strip())
                if m:
                    return m.group(1)
    except OSError:
        pass
    return None


def write_module_version(filepath: str, version: str) -> None:
    """
    Overwrite the ``VERSION = "..."`` line inside *filepath*.

    Raises ``RuntimeError`` if the line is not found.
    """
    with open(filepath, "r", encoding="utf-8") as fh:
        lines = fh.readlines()

    found = False
    for idx, line in enumerate(lines):
        if re.match(r'^VERSION\s*=\s*["\']', line.strip()):
            lines[idx] = f'VERSION = "{version}"\n'
            found = True
            break

    if not found:
        raise RuntimeError(f"VERSION line not found in {filepath}")

    with open(filepath, "w", encoding="utf-8") as fh:
        fh.writelines(lines)


def find_icon() -> Optional[str]:
    """
    Look for ``evilcrow.ico`` next to this script.

    Returns absolute path or ``None``.
    """
    candidate = os.path.join(SDR_DIR, ICON_FILENAME)
    if os.path.isfile(candidate):
        return os.path.abspath(candidate)
    return None


def parse_semver(text: str) -> Tuple[int, int, int]:
    """Parse ``"major.minor.patch"`` and return a 3-tuple of ints."""
    parts = text.strip().split(".")
    if len(parts) != 3:
        raise ValueError(f"Expected x.y.z, got '{text}'")
    return int(parts[0]), int(parts[1]), int(parts[2])


def format_semver(major: int, minor: int, patch: int) -> str:
    return f"{major}.{minor}.{patch}"


# ── Dependency detection ───────────────────────────────────────

def find_pyinstaller() -> Optional[str]:
    """
    Return path to pyinstaller executable, or ``None``.

    Checks both ``import PyInstaller`` and ``shutil.which``.
    """
    # 1. Try Python module
    try:
        import PyInstaller  # noqa: F401
        return "module"
    except ImportError:
        pass
    # 2. Try system PATH
    path = shutil.which("pyinstaller")
    if path:
        return path
    return None


def get_pyinstaller_version() -> Optional[str]:
    """Return PyInstaller version string or None."""
    try:
        import PyInstaller
        return PyInstaller.__version__
    except Exception:
        pass
    # Fallback: try running pyinstaller --version
    try:
        result = subprocess.run(
            [_python_exe(), "-m", "PyInstaller", "--version"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None


def install_pyinstaller(log_fn=None) -> bool:
    """Install PyInstaller via pip.  Returns True on success."""
    cmd = [_python_exe(), "-m", "pip", "install", "pyinstaller", "-q"]
    if log_fn:
        log_fn(f"$ {' '.join(cmd)}\n", "info")
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=120,
        )
        if result.returncode == 0:
            if log_fn:
                log_fn("PyInstaller installed successfully.\n", "success")
            return True
        if log_fn:
            log_fn(f"pip error:\n{result.stderr}\n", "error")
    except Exception as exc:
        if log_fn:
            log_fn(f"Install error: {exc}\n", "error")
    return False


def find_upx() -> Optional[str]:
    """
    Locate UPX binary.

    Search order:
    1. ``<SDR_DIR>/upx/`` local directory (flat or versioned subfolder)
    2. System PATH
    """
    # 1. Local directory — flat
    local_exe = os.path.join(
        UPX_LOCAL_DIR, "upx.exe" if os.name == "nt" else "upx"
    )
    if os.path.isfile(local_exe):
        return UPX_LOCAL_DIR  # PyInstaller wants the *directory*

    # 1b. Local directory — versioned subfolder (e.g. upx-4.2.4-win64/)
    if os.path.isdir(UPX_LOCAL_DIR):
        for entry in os.listdir(UPX_LOCAL_DIR):
            sub = os.path.join(UPX_LOCAL_DIR, entry)
            if os.path.isdir(sub):
                candidate = os.path.join(
                    sub, "upx.exe" if os.name == "nt" else "upx"
                )
                if os.path.isfile(candidate):
                    return sub

    # 2. System PATH
    system_upx = shutil.which("upx")
    if system_upx:
        return os.path.dirname(system_upx)

    return None


def get_upx_version(upx_dir: str) -> Optional[str]:
    """Run ``upx --version`` and return the first line."""
    exe = os.path.join(upx_dir, "upx.exe" if os.name == "nt" else "upx")
    if not os.path.isfile(exe):
        return None
    try:
        result = subprocess.run(
            [exe, "--version"], capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout:
            return result.stdout.splitlines()[0].strip()
    except Exception:
        pass
    return None


def download_upx(log_fn=None) -> Optional[str]:
    """
    Download UPX for Windows from GitHub releases.

    Extracts into ``<SDR_DIR>/upx/``.  Returns the directory path
    containing ``upx.exe`` or ``None`` on failure.
    """
    if platform.system() != "Windows":
        if log_fn:
            log_fn(
                "UPX auto-download is only supported on Windows.\n"
                "Install UPX manually and ensure it is in your PATH.\n",
                "warning",
            )
        return None

    if log_fn:
        log_fn(f"Downloading UPX v{UPX_VERSION} from GitHub...\n", "info")

    try:
        req = urllib.request.Request(
            UPX_WIN_URL, headers={"User-Agent": "EvilCrow-Builder"}
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            total = int(resp.headers.get("Content-Length", 0))
            data = bytearray()
            block = 8192
            while True:
                chunk = resp.read(block)
                if not chunk:
                    break
                data.extend(chunk)
                if log_fn and total:
                    pct = len(data) * 100 // total
                    log_fn(f"\r  Downloaded {pct}%", "dim")

        if log_fn:
            log_fn(f"\n  Size: {len(data) / 1048576:.1f} MB\n", "dim")

        # Extract zip
        os.makedirs(UPX_LOCAL_DIR, exist_ok=True)
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            zf.extractall(UPX_LOCAL_DIR)

        # Find the extracted upx.exe
        upx_path = find_upx()
        if upx_path:
            if log_fn:
                log_fn(f"UPX extracted to {upx_path}\n", "success")
            return upx_path

        if log_fn:
            log_fn(
                "UPX downloaded but executable not found in archive.\n",
                "error",
            )
    except Exception as exc:
        if log_fn:
            log_fn(f"UPX download failed: {exc}\n", "error")
    return None


# ── Build logic ────────────────────────────────────────────────

def build_pyinstaller_cmd(
    *,
    onefile: bool = True,
    windowed: bool = True,
    clean: bool = True,
    strip: bool = False,
    noconsole: bool = False,
    upx_dir: Optional[str] = None,
    icon_path: Optional[str] = None,
    extra_flags: Optional[List[str]] = None,
) -> List[str]:
    """
    Assemble the PyInstaller command list for ``sdr_launcher.py``.
    """
    cmd: List[str] = [_python_exe(), "-m", "PyInstaller"]

    if onefile:
        cmd.append("--onefile")
    if windowed:
        cmd.append("--windowed")
    if noconsole and not windowed:
        cmd.append("--noconsole")
    if clean:
        cmd.append("--clean")
    if strip and platform.system() != "Windows":
        cmd.append("--strip")
    if upx_dir:
        cmd.append(f"--upx-dir={upx_dir}")
    if icon_path and os.path.isfile(icon_path):
        cmd.append(f"--icon={icon_path}")
    if extra_flags:
        cmd.extend(extra_flags)

    # Target script (absolute path — works from any cwd)
    cmd.append(os.path.join(SDR_DIR, "sdr_launcher.py"))

    return cmd


# ══════════════════════════════════════════════════════════════
#  GUI
# ══════════════════════════════════════════════════════════════

# Colour palette (Material-dark inspired)
_C = {
    "bg":       "#121212",
    "surface":  "#1e1e1e",
    "card":     "#252525",
    "primary":  "#bb86fc",
    "accent":   "#03dac6",
    "error":    "#cf6679",
    "warning":  "#ffb74d",
    "success":  "#66bb6a",
    "info":     "#42a5f5",
    "text":     "#e0e0e0",
    "dimtext":  "#9e9e9e",
    "logbg":    "#0a0a0a",
    "logfg":    "#00e676",
}

FONT_FAMILY = "Segoe UI" if os.name == "nt" else "Helvetica"
MONO_FONT   = "Consolas"  if os.name == "nt" else "Courier"


class BuilderApp:
    """Main GUI application for the EvilCrow SDR Builder."""

    # ── init ───────────────────────────────────────────────────

    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title(f"EvilCrow SDR Builder v{BUILDER_VERSION}")
        self.root.configure(bg=_C["bg"])
        self.root.geometry("960x800")
        self.root.minsize(800, 620)

        self._build_running = False

        # Discovered state
        self._icon_path: Optional[str] = find_icon()
        self._upx_dir: Optional[str] = find_upx()
        self._pyinstaller_ok: bool = find_pyinstaller() is not None

        # Read module versions
        self._module_versions: Dict[str, str] = {}
        for fname in MODULE_FILES:
            fpath = os.path.join(SDR_DIR, fname)
            ver = read_module_version(fpath)
            self._module_versions[fname] = ver or "n/a"

        self._launcher_version = self._module_versions.get(
            "sdr_launcher.py", "1.0.0"
        )

        self._setup_styles()
        self._build_ui()

    # ── ttk styles ─────────────────────────────────────────────

    def _setup_styles(self) -> None:
        s = ttk.Style()
        s.theme_use("clam")

        s.configure(
            ".", background=_C["bg"], foreground=_C["text"],
            font=(FONT_FAMILY, 10),
        )
        s.configure("TFrame", background=_C["bg"])
        s.configure("Card.TFrame", background=_C["card"])

        # Labels
        s.configure("TLabel", background=_C["bg"], foreground=_C["text"])
        s.configure("Card.TLabel", background=_C["card"], foreground=_C["text"])
        s.configure(
            "Title.TLabel", font=(FONT_FAMILY, 18, "bold"),
            foreground=_C["primary"], background=_C["bg"],
        )
        s.configure(
            "Subtitle.TLabel", font=(FONT_FAMILY, 10),
            foreground=_C["dimtext"], background=_C["bg"],
        )
        s.configure(
            "Section.TLabel", font=(FONT_FAMILY, 11, "bold"),
            foreground=_C["accent"], background=_C["card"],
        )
        s.configure("Status.TLabel", font=(FONT_FAMILY, 9), background=_C["card"])
        s.configure("OK.Status.TLabel", foreground=_C["success"])
        s.configure("Warn.Status.TLabel", foreground=_C["warning"])
        s.configure("Err.Status.TLabel", foreground=_C["error"])

        # Buttons
        s.configure(
            "TButton", font=(FONT_FAMILY, 10, "bold"),
            padding=(12, 6), background="#333333", foreground="#ffffff",
        )
        s.map(
            "TButton",
            background=[("active", _C["primary"]), ("disabled", "#222222")],
            foreground=[("disabled", "#555555")],
        )
        s.configure(
            "Accent.TButton", background=_C["primary"], foreground="#000000",
        )
        s.map(
            "Accent.TButton",
            background=[("active", "#d0a0ff"), ("disabled", "#333333")],
        )
        s.configure(
            "Small.TButton", font=(FONT_FAMILY, 9),
            padding=(8, 3), background="#333333", foreground="#ffffff",
        )
        s.map(
            "Small.TButton",
            background=[("active", _C["primary"]), ("disabled", "#222222")],
            foreground=[("disabled", "#555555")],
        )

        # Checkbuttons
        s.configure(
            "TCheckbutton", background=_C["card"],
            foreground=_C["text"], font=(FONT_FAMILY, 9),
        )

        # Entry
        s.configure(
            "TEntry", fieldbackground=_C["surface"],
            foreground=_C["text"], insertcolor=_C["text"],
        )

        # LabelFrame
        s.configure("TLabelframe", background=_C["card"], foreground=_C["accent"])
        s.configure(
            "TLabelframe.Label", background=_C["card"],
            foreground=_C["accent"], font=(FONT_FAMILY, 10, "bold"),
        )

        # Progressbar
        s.configure(
            "Green.Horizontal.TProgressbar",
            troughcolor=_C["surface"], background=_C["success"],
        )

    # ── UI layout ──────────────────────────────────────────────

    def _build_ui(self) -> None:
        root = self.root
        root.grid_rowconfigure(5, weight=1)
        root.grid_columnconfigure(0, weight=1)

        pad = {"padx": 16, "pady": (0, 8)}

        # ── Row 0: Title ──────────────────────────────────────
        hdr = ttk.Frame(root)
        hdr.grid(row=0, column=0, sticky="ew", padx=16, pady=(16, 4))
        ttk.Label(
            hdr, text="\u26a1 EvilCrow SDR Builder", style="Title.TLabel",
        ).pack(side=tk.LEFT)
        ttk.Label(
            hdr, text=f"  v{BUILDER_VERSION}", style="Subtitle.TLabel",
        ).pack(side=tk.LEFT, pady=(6, 0))

        # ── Row 1: Environment card ───────────────────────────
        env_card = ttk.LabelFrame(root, text="  Environment  ", padding=10)
        env_card.grid(row=1, column=0, sticky="ew", **pad)
        env_card.grid_columnconfigure(1, weight=1)

        # Python row
        self._add_env_row(
            env_card, 0, "Python",
            f"{sys.version.split()[0]}  ({_python_exe()})",
        )

        # PyInstaller row
        pi_text, pi_style = self._pyinstaller_status_text()
        self._pi_label = self._add_env_status(
            env_card, 1, "PyInstaller", pi_text, pi_style,
        )
        self._pi_btn = ttk.Button(
            env_card, text="Install", style="Small.TButton",
            command=self._on_install_pyinstaller, width=10,
        )
        self._pi_btn.grid(row=1, column=2, padx=4)
        if self._pyinstaller_ok:
            self._pi_btn.configure(state=tk.DISABLED)

        # UPX row
        upx_text, upx_style = self._upx_status_text()
        self._upx_label = self._add_env_status(
            env_card, 2, "UPX", upx_text, upx_style,
        )
        self._upx_btn = ttk.Button(
            env_card, text="Download", style="Small.TButton",
            command=self._on_download_upx, width=10,
        )
        self._upx_btn.grid(row=2, column=2, padx=4)
        if self._upx_dir:
            self._upx_btn.configure(state=tk.DISABLED)

        # Icon row
        icon_text = (
            os.path.basename(self._icon_path) if self._icon_path else "not found"
        )
        icon_style = (
            "OK.Status.TLabel" if self._icon_path else "Warn.Status.TLabel"
        )
        self._icon_label = self._add_env_status(
            env_card, 3, "Icon", icon_text, icon_style,
        )
        ttk.Button(
            env_card, text="Browse", style="Small.TButton",
            command=self._on_browse_icon, width=10,
        ).grid(row=3, column=2, padx=4)

        # ── Row 2: Module versions ────────────────────────────
        ver_card = ttk.LabelFrame(
            root, text="  Module Versions  ", padding=10,
        )
        ver_card.grid(row=2, column=0, sticky="ew", **pad)
        cols = 4
        for i in range(cols):
            ver_card.grid_columnconfigure(i, weight=1)

        col = 0
        row_idx = 0
        for fname, ver in self._module_versions.items():
            c = col * 2
            ttk.Label(
                ver_card, text=fname, style="Card.TLabel",
                font=(MONO_FONT, 9),
            ).grid(row=row_idx, column=c, sticky="w", padx=(0, 4))
            ttk.Label(
                ver_card, text=ver, style="Card.TLabel",
                foreground=_C["accent"], font=(MONO_FONT, 9, "bold"),
            ).grid(row=row_idx, column=c + 1, sticky="w", padx=(0, 20))
            col += 1
            if col > 1:
                col = 0
                row_idx += 1

        # ── Row 3: Build settings ─────────────────────────────
        build_card = ttk.LabelFrame(
            root, text="  Build Settings  ", padding=10,
        )
        build_card.grid(row=3, column=0, sticky="ew", **pad)
        build_card.grid_columnconfigure(1, weight=1)

        # Version entry + bump buttons
        ttk.Label(
            build_card, text="Target version:", style="Card.TLabel",
        ).grid(row=0, column=0, sticky="w")

        self._ver_var = tk.StringVar(value=self._launcher_version)
        ver_entry = ttk.Entry(build_card, textvariable=self._ver_var, width=14)
        ver_entry.grid(row=0, column=1, sticky="w", padx=6)

        ver_btns = ttk.Frame(build_card, style="Card.TFrame")
        ver_btns.grid(row=0, column=2, sticky="w")
        for label, cmd in [
            ("Major +1", self._bump_major),
            ("Minor +1", self._bump_minor),
            ("Patch +1", self._bump_patch),
        ]:
            ttk.Button(
                ver_btns, text=label, command=cmd,
                style="Small.TButton", width=9,
            ).pack(side=tk.LEFT, padx=2)

        # Build option checkboxes
        opt_frame = ttk.Frame(build_card, style="Card.TFrame")
        opt_frame.grid(row=1, column=0, columnspan=4, sticky="w", pady=(8, 0))

        self._opt_onefile  = tk.BooleanVar(value=True)
        self._opt_windowed = tk.BooleanVar(value=True)
        self._opt_clean    = tk.BooleanVar(value=True)
        self._opt_strip    = tk.BooleanVar(value=False)
        self._opt_upx      = tk.BooleanVar(value=self._upx_dir is not None)
        self._opt_noconsole = tk.BooleanVar(value=False)

        opts = [
            ("--onefile",    "Single file executable",   self._opt_onefile),
            ("--windowed",   "No console window",        self._opt_windowed),
            ("--clean",      "Clean build artifacts",    self._opt_clean),
            ("--strip",      "Strip binary (Unix)",      self._opt_strip),
            ("Use UPX",      "Compress with UPX",        self._opt_upx),
            ("--noconsole",  "Suppress console",         self._opt_noconsole),
        ]
        for i, (label, tip, var) in enumerate(opts):
            cb = ttk.Checkbutton(
                opt_frame, text=f"{label}  ({tip})", variable=var,
            )
            cb.grid(row=i // 3, column=i % 3, sticky="w", padx=(0, 16), pady=2)

        # Sync all module versions checkbox
        self._opt_sync = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            opt_frame,
            text="Sync version across all modules on build",
            variable=self._opt_sync,
        ).grid(row=3, column=0, columnspan=3, sticky="w", pady=(4, 0))

        # ── Row 4: Actions + progress ─────────────────────────
        action_frame = ttk.Frame(root)
        action_frame.grid(row=4, column=0, sticky="ew", **pad)

        self._build_btn = ttk.Button(
            action_frame, text="  \u2692  Build EXE  ",
            style="Accent.TButton", command=self._on_build,
        )
        self._build_btn.pack(side=tk.LEFT)

        ttk.Button(
            action_frame, text="Open dist/",
            command=self._on_open_dist,
        ).pack(side=tk.LEFT, padx=8)

        ttk.Button(
            action_frame, text="Clear Log",
            command=self._clear_log,
        ).pack(side=tk.RIGHT)

        self._progress = ttk.Progressbar(
            action_frame, mode="indeterminate", length=220,
            style="Green.Horizontal.TProgressbar",
        )
        self._progress.pack(side=tk.RIGHT, padx=8)

        # ── Row 5: Log area ───────────────────────────────────
        log_label = ttk.Label(
            root, text="Build Output", style="Subtitle.TLabel",
        )
        log_label.grid(row=5, column=0, sticky="nw", padx=18, pady=(4, 0))

        self._log = scrolledtext.ScrolledText(
            root, height=14, bg=_C["logbg"], fg=_C["logfg"],
            insertbackground=_C["logfg"],
            font=(MONO_FONT, 9), wrap=tk.WORD,
        )
        self._log.grid(row=5, column=0, sticky="nsew", padx=16, pady=(4, 16))
        # Adjust weight so log expands with row 5
        root.grid_rowconfigure(5, weight=1)

        # Colour tags
        for tag in ("success", "error", "warning", "info", "dim"):
            self._log.tag_config(tag, foreground=_C.get(tag, _C["dimtext"]))

        # ── Welcome message ───────────────────────────────────
        self._log_msg(
            f"EvilCrow SDR Builder v{BUILDER_VERSION}\n"
            f"Python  : {sys.version.split()[0]}\n"
            f"Platform: {platform.system()} {platform.machine()}\n"
            f"Workdir : {SCRIPT_DIR}\n",
            "dim",
        )
        if self._icon_path:
            self._log_msg(f"Icon    : {self._icon_path}\n", "dim")
        if self._pyinstaller_ok:
            pv = get_pyinstaller_version() or "?"
            self._log_msg(f"PyInst  : v{pv}\n", "dim")
        if self._upx_dir:
            uv = get_upx_version(self._upx_dir) or self._upx_dir
            self._log_msg(f"UPX     : {uv}\n", "dim")
        self._log_msg("\nReady.\n", "success")

    # ── helpers for env rows ───────────────────────────────────

    @staticmethod
    def _add_env_row(parent, row, label, value):
        ttk.Label(
            parent, text=f"{label}:", style="Card.TLabel",
            font=(FONT_FAMILY, 9, "bold"),
        ).grid(row=row, column=0, sticky="w", padx=(0, 8))
        ttk.Label(
            parent, text=value, style="Card.TLabel",
            font=(MONO_FONT, 9),
        ).grid(row=row, column=1, sticky="w")

    @staticmethod
    def _add_env_status(parent, row, label, value, style):
        ttk.Label(
            parent, text=f"{label}:", style="Card.TLabel",
            font=(FONT_FAMILY, 9, "bold"),
        ).grid(row=row, column=0, sticky="w", padx=(0, 8))
        lbl = ttk.Label(
            parent, text=value, style=style, font=(MONO_FONT, 9),
        )
        lbl.grid(row=row, column=1, sticky="w")
        return lbl

    # ── status helpers ─────────────────────────────────────────

    def _pyinstaller_status_text(self):
        if self._pyinstaller_ok:
            ver = get_pyinstaller_version() or "found"
            return f"v{ver}", "OK.Status.TLabel"
        return "not installed", "Err.Status.TLabel"

    def _upx_status_text(self):
        if self._upx_dir:
            ver = get_upx_version(self._upx_dir)
            if ver:
                return f"{ver}", "OK.Status.TLabel"
            return f"found ({self._upx_dir})", "OK.Status.TLabel"
        return "not found (optional)", "Warn.Status.TLabel"

    # ── log ────────────────────────────────────────────────────

    def _log_msg(self, text: str, tag: str = "") -> None:
        """Append text to the build log (thread-safe)."""
        def _do():
            self._log.insert(tk.END, text, tag)
            self._log.see(tk.END)
        self.root.after(0, _do)

    def _clear_log(self) -> None:
        self._log.delete("1.0", tk.END)

    # ── version bumping ────────────────────────────────────────

    def _bump_major(self):
        try:
            ma, mi, pa = parse_semver(self._ver_var.get())
            self._ver_var.set(format_semver(ma + 1, 0, 0))
        except ValueError:
            messagebox.showerror("Error", "Invalid version format (expected x.y.z)")

    def _bump_minor(self):
        try:
            ma, mi, pa = parse_semver(self._ver_var.get())
            self._ver_var.set(format_semver(ma, mi + 1, 0))
        except ValueError:
            messagebox.showerror("Error", "Invalid version format (expected x.y.z)")

    def _bump_patch(self):
        try:
            ma, mi, pa = parse_semver(self._ver_var.get())
            self._ver_var.set(format_semver(ma, mi, pa + 1))
        except ValueError:
            messagebox.showerror("Error", "Invalid version format (expected x.y.z)")

    # ── callbacks ──────────────────────────────────────────────

    def _on_install_pyinstaller(self) -> None:
        if not messagebox.askyesno(
            "Install PyInstaller",
            "PyInstaller will be installed via pip into the current "
            "Python environment.\n\nProceed?",
        ):
            return

        self._pi_btn.configure(state=tk.DISABLED)

        def _worker():
            ok = install_pyinstaller(log_fn=self._log_msg)
            if ok:
                self._pyinstaller_ok = True
            self.root.after(0, self._refresh_env_status)

        threading.Thread(target=_worker, daemon=True).start()

    def _on_download_upx(self) -> None:
        if not messagebox.askyesno(
            "Download UPX",
            f"UPX v{UPX_VERSION} will be downloaded from GitHub\n"
            f"and extracted to:\n  {UPX_LOCAL_DIR}\n\nProceed?",
        ):
            return

        self._upx_btn.configure(state=tk.DISABLED)

        def _worker():
            path = download_upx(log_fn=self._log_msg)
            if path:
                self._upx_dir = path
            self.root.after(0, self._refresh_env_status)

        threading.Thread(target=_worker, daemon=True).start()

    def _on_browse_icon(self) -> None:
        path = filedialog.askopenfilename(
            initialdir=SDR_DIR,
            filetypes=[("Icon files", "*.ico"), ("All files", "*.*")],
        )
        if path:
            self._icon_path = os.path.abspath(path)
            self._icon_label.configure(
                text=os.path.basename(path), style="OK.Status.TLabel",
            )

    def _on_open_dist(self) -> None:
        dist = os.path.join(SDR_DIR, "dist")
        if os.path.isdir(dist):
            if os.name == "nt":
                os.startfile(dist)  # noqa: S606
            else:
                subprocess.Popen(["xdg-open", dist])  # noqa: S603
        else:
            messagebox.showinfo(
                "Info", "dist/ folder does not exist yet.\nRun a build first.",
            )

    def _refresh_env_status(self) -> None:
        """Re-read environment and update status labels."""
        # PyInstaller
        txt, sty = self._pyinstaller_status_text()
        self._pi_label.configure(text=txt, style=sty)
        self._pi_btn.configure(
            state=tk.DISABLED if self._pyinstaller_ok else tk.NORMAL,
        )

        # UPX
        txt, sty = self._upx_status_text()
        self._upx_label.configure(text=txt, style=sty)
        self._upx_btn.configure(
            state=tk.DISABLED if self._upx_dir else tk.NORMAL,
        )
        self._opt_upx.set(self._upx_dir is not None)

    # ── build ──────────────────────────────────────────────────

    def _on_build(self) -> None:
        if self._build_running:
            return

        # Validate version
        ver = self._ver_var.get().strip()
        try:
            parse_semver(ver)
        except ValueError:
            messagebox.showerror("Error", "Invalid version (expected x.y.z)")
            return

        # Check PyInstaller
        if not self._pyinstaller_ok:
            if messagebox.askyesno(
                "PyInstaller Missing",
                "PyInstaller is not installed.\nInstall it now?",
            ):
                self._on_install_pyinstaller()
                self._log_msg(
                    "Click 'Build EXE' again after installation completes.\n",
                    "warning",
                )
            return

        self._build_running = True
        self._build_btn.configure(state=tk.DISABLED)
        self._progress.start(12)

        threading.Thread(
            target=self._build_worker, args=(ver,), daemon=True,
        ).start()

    def _build_worker(self, version: str) -> None:
        """Run the full build pipeline in a background thread."""
        try:
            self._clear_log()
            self._log_msg(
                f"{'=' * 70}\n"
                f"  EvilCrow SDR Builder v{BUILDER_VERSION}\n"
                f"  Target version : {version}\n"
                f"  Working dir    : {SDR_DIR}\n"
                f"{'=' * 70}\n\n",
                "info",
            )

            # ── Step 1: Update version(s) ─────────────────────
            if self._opt_sync.get():
                self._log_msg(
                    "[1/4] Syncing version across all modules...\n", "info",
                )
                for fname in MODULE_FILES:
                    fpath = os.path.join(SDR_DIR, fname)
                    if os.path.isfile(fpath):
                        old = read_module_version(fpath) or "n/a"
                        try:
                            write_module_version(fpath, version)
                            self._log_msg(
                                f"  {fname}: {old} -> {version}\n", "success",
                            )
                        except RuntimeError as exc:
                            self._log_msg(f"  {fname}: {exc}\n", "warning")
            else:
                self._log_msg(
                    "[1/4] Updating sdr_launcher.py version...\n", "info",
                )
                fpath = os.path.join(SDR_DIR, "sdr_launcher.py")
                write_module_version(fpath, version)
                self._log_msg(
                    f"  sdr_launcher.py -> {version}\n", "success",
                )

            # ── Step 2: Resolve UPX ───────────────────────────
            upx_dir: Optional[str] = None
            if self._opt_upx.get():
                upx_dir = self._upx_dir or find_upx()
                if upx_dir:
                    self._log_msg(
                        f"[2/4] UPX compression enabled: {upx_dir}\n", "info",
                    )
                else:
                    self._log_msg(
                        "[2/4] UPX requested but not found — skipping.\n",
                        "warning",
                    )
            else:
                self._log_msg("[2/4] UPX compression: disabled.\n", "dim")

            # ── Step 3: Build command ─────────────────────────
            self._log_msg("[3/4] Preparing PyInstaller command...\n", "info")
            cmd = build_pyinstaller_cmd(
                onefile=self._opt_onefile.get(),
                windowed=self._opt_windowed.get(),
                clean=self._opt_clean.get(),
                strip=self._opt_strip.get(),
                noconsole=self._opt_noconsole.get(),
                upx_dir=upx_dir,
                icon_path=self._icon_path,
            )
            self._log_msg(f"  $ {' '.join(cmd)}\n\n", "dim")

            # ── Step 4: Run PyInstaller ───────────────────────
            self._log_msg(
                f"[4/4] Building executable...\n{'─' * 70}\n", "info",
            )

            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                cwd=SDR_DIR,
            )

            for line in iter(process.stdout.readline, ""):
                if not line:
                    break
                low = line.lower()
                if "error" in low:
                    tag = "error"
                elif "warning" in low or "warn" in low:
                    tag = "warning"
                elif "success" in low or "complete" in low:
                    tag = "success"
                else:
                    tag = ""
                self._log_msg(line, tag)

            process.wait()

            self._log_msg(f"{'─' * 70}\n", "info")

            if process.returncode == 0:
                exe_name = (
                    "sdr_launcher.exe" if os.name == "nt" else "sdr_launcher"
                )
                exe_path = os.path.join(SDR_DIR, "dist", exe_name)
                size_mb = ""
                if os.path.isfile(exe_path):
                    size_mb = (
                        f"  ({os.path.getsize(exe_path) / 1048576:.1f} MB)"
                    )

                self._log_msg(
                    f"\n  BUILD SUCCESSFUL\n"
                    f"  Version : {version}\n"
                    f"  Output  : {exe_path}{size_mb}\n\n",
                    "success",
                )
                self.root.after(
                    0,
                    lambda: messagebox.showinfo(
                        "Build Complete",
                        f"EXE built successfully!\n\n"
                        f"Version: {version}\n"
                        f"Output: {exe_path}{size_mb}",
                    ),
                )
            else:
                self._log_msg(
                    f"\n  BUILD FAILED  (exit code {process.returncode})\n"
                    f"  Check the log above for details.\n\n",
                    "error",
                )
                self.root.after(
                    0,
                    lambda: messagebox.showerror(
                        "Build Failed",
                        f"PyInstaller exited with code {process.returncode}.\n"
                        "Check the build log for details.",
                    ),
                )

        except Exception as exc:
            self._log_msg(f"\nEXCEPTION: {exc}\n", "error")
            self.root.after(
                0, lambda: messagebox.showerror("Error", str(exc)),
            )

        finally:
            # Re-read updated versions
            for fname in MODULE_FILES:
                fpath = os.path.join(SDR_DIR, fname)
                ver = read_module_version(fpath)
                self._module_versions[fname] = ver or "n/a"

            self.root.after(0, self._build_done)

    def _build_done(self) -> None:
        self._build_running = False
        self._build_btn.configure(state=tk.NORMAL)
        self._progress.stop()

    # ── mainloop ───────────────────────────────────────────────

    def run(self) -> None:
        self.root.mainloop()


# ══════════════════════════════════════════════════════════════
#  Entry point
# ══════════════════════════════════════════════════════════════

def main() -> None:
    app = BuilderApp()
    app.run()


if __name__ == "__main__":
    main()
