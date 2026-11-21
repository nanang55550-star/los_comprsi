#!/usr/bin/env python3
"""
tiktok_patcher.py / tools.py
============================
TikTok Compression Patcher - Anti-Compression HD Re-encoder v2.1.0

Mengoptimalkan video hasil render (Alight Motion, CapCut, Premiere, After Effects)
agar tahan terhadap kompresi agresif TikTok, mempertahankan ketajaman detail
pada 60fps menggunakan H.264 (High Profile) + AAC, dilengkapi dengan:
- Konfigurasi terpisah (config.py & config.json) yang tersimpan permanen
- Mode batch multi-video (proses banyak video sekaligus atau ALL)
- Filter Smart Sharpen (Unsharp mask) khusus anti-blur TikTok
- Audio Loudness Normalization (standar EBU R128 / TikTok)
- Deteksi Hardware Acceleration (NVENC, QSV, VideoToolbox, MediaCodec)
- Re-encode + Crop pintar (cover tanpa distorsi)
- Stabilisasi video canggih (L1 Trend Filtering / ADMM & Optical Flow)
- Video Inspector / Analisis detail spesifikasi video
- Preset kustom buatan pengguna yang bisa disimpan & dimuat kapan saja

Dependensi:
    - ffmpeg (tersedia di PATH sistem)
    - opencv-python, numpy, scipy (opsional, untuk stabilisasi video)
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Import modul konfigurasi terpisah
try:
    from config import (
        CONFIG_FILE_NAME,
        DEFAULT_ENCODE_SETTINGS,
        DEFAULT_STAB_SETTINGS,
        ENCODE_PRESETS,
        config_mgr,
    )
except ImportError:
    # Fallback jika dijalankan di direktori berbeda
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from config import (
        CONFIG_FILE_NAME,
        DEFAULT_ENCODE_SETTINGS,
        DEFAULT_STAB_SETTINGS,
        ENCODE_PRESETS,
        config_mgr,
    )

# =============================================================================
# STYLING TERMINAL - Cyberpunk ANSI palette
# =============================================================================


class C:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    CYAN = "\033[38;5;51m"
    MAGENTA = "\033[38;5;201m"
    GREEN = "\033[38;5;46m"
    YELLOW = "\033[38;5;226m"
    RED = "\033[38;5;196m"
    GRAY = "\033[38;5;244m"
    PURPLE = "\033[38;5;135m"
    BLUE = "\033[38;5;39m"
    WHITE = "\033[38;5;231m"


if not sys.stdout.isatty():
    for _attr in [a for a in dir(C) if not a.startswith("_")]:
        setattr(C, _attr, "")

ANSI_RE = re.compile(r"\033\[[0-9;]*m")
DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)")
TIME_RE = re.compile(r"time=(\d+):(\d+):(\d+\.\d+)")
SPEED_RE = re.compile(r"speed=\s*([\d\.]+)x")


def strip_ansi(s: str) -> str:
    return ANSI_RE.sub("", s)


def tag(label: str, color: str) -> str:
    return f"{color}{C.BOLD}[{label}]{C.RESET}"


def print_tag(label: str, message: str, color: str) -> None:
    print(f"{tag(label, color)} {message}")


def print_box(lines: List[str], color: str = C.MAGENTA) -> None:
    plain_lines = [strip_ansi(l) for l in lines]
    width = max([len(p) for p in plain_lines] + [38])
    print(f"{color}╔{'═' * (width + 2)}╗{C.RESET}")
    for line, plain in zip(lines, plain_lines):
        pad = width - len(plain)
        print(f"{color}║{C.RESET} {line}{' ' * pad} {color}║{C.RESET}")
    print(f"{color}╚{'═' * (width + 2)}╝{C.RESET}")


def print_banner() -> None:
    print_box(
        [
            f"{C.CYAN}{C.BOLD}T I K T O K   P A T C H E R   P R O{C.RESET}",
            f"{C.GRAY}Anti-Compression HD Re-encoder . H.264 + AAC . 60fps{C.RESET}",
            f"{C.PURPLE}v2.1.0 . Persistent Config . Batch Mode . Smart Filters{C.RESET}",
        ],
        color=C.MAGENTA,
    )


# =============================================================================
# UTILITAS UMUM
# =============================================================================


def human_size(num_bytes: float) -> str:
    n = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024.0:
            return f"{n:.2f} {unit}"
        n /= 1024.0
    return f"{n:.2f} TB"


def human_bitrate(bps: Optional[float]) -> str:
    if not bps:
        return "N/A"
    mbps = bps / 1_000_000
    if mbps >= 1:
        return f"{mbps:.2f} Mbps"
    return f"{bps / 1000:.1f} kbps"


def draw_progress(current: float, total: float, width: int = 24) -> str:
    if not total or total <= 0:
        pct = 0.0
    else:
        pct = min(max(current / total, 0.0), 1.0)
    filled = int(width * pct)
    bar = "█" * filled + "░" * (width - filled)
    return f"{C.MAGENTA}▐{bar}▌{C.RESET} {C.CYAN}{pct * 100:5.1f}%{C.RESET}"


def get_environment_label() -> str:
    if "TERMUX_VERSION" in os.environ:
        return f"Termux {os.environ.get('TERMUX_VERSION')}"
    if sys.platform == "darwin":
        return "macOS"
    if sys.platform.startswith("linux"):
        return "Linux"
    return sys.platform


def validate_input_file(path: Path) -> Tuple[bool, Optional[str]]:
    """Validasi keberadaan & izin baca file input. Return (ok, pesan_error)."""
    if not path.is_file():
        return False, f"File tidak ditemukan: {path}"
    if not os.access(path, os.R_OK):
        return False, f"File tidak dapat dibaca (izin ditolak): {path}"
    return True, None


# =============================================================================
# HARDWARE ACCELERATION & ENCODER DETECTION
# =============================================================================

KNOWN_ENCODERS = {
    "libx264": {"name": "libx264 (CPU Standard)", "type": "cpu"},
    "h264_nvenc": {"name": "NVIDIA NVENC (GPU)", "type": "nvidia"},
    "h264_qsv": {"name": "Intel QuickSync (QSV)", "type": "intel"},
    "h264_videotoolbox": {"name": "Apple VideoToolbox (Mac)", "type": "apple"},
    "h264_vaapi": {"name": "Linux VAAPI (GPU)", "type": "vaapi"},
    "h264_mediacodec": {"name": "Android MediaCodec (Hardware)", "type": "android"},
}


def detect_available_encoders(ffmpeg_bin: str) -> List[str]:
    """Mendeteksi encoder H.264 yang didukung oleh binary ffmpeg saat ini."""
    available = ["libx264"]
    try:
        res = subprocess.run(
            [ffmpeg_bin, "-encoders"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=5,
        )
        output = res.stdout
        for enc in ("h264_nvenc", "h264_qsv", "h264_videotoolbox", "h264_vaapi", "h264_mediacodec"):
            if enc in output:
                available.append(enc)
    except Exception:
        pass
    return available


# =============================================================================
# FFMPEG HELPERS & VIDEO INSPECTION
# =============================================================================


def check_ffmpeg() -> Optional[str]:
    return shutil.which("ffmpeg")


def get_ffmpeg_version(ffmpeg_bin: str) -> Optional[str]:
    try:
        result = subprocess.run(
            [ffmpeg_bin, "-version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=5,
        )
        lines = result.stdout.splitlines()
        return lines[0] if lines else None
    except Exception:
        return None


def get_duration_seconds(ffmpeg_bin: str, path: Path) -> Optional[float]:
    try:
        result = subprocess.run(
            [ffmpeg_bin, "-i", str(path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=10,
        )
        match = DURATION_RE.search(result.stdout)
        if match:
            h, m, s = match.groups()
            return int(h) * 3600 + int(m) * 60 + float(s)
    except Exception:
        pass
    return None


def get_file_stats(ffmpeg_bin: str, path: Path) -> dict:
    size = path.stat().st_size
    duration = get_duration_seconds(ffmpeg_bin, path)
    bitrate = (size * 8 / duration) if duration else None
    return {"size": size, "duration": duration, "bitrate": bitrate}


def get_video_dimensions(ffmpeg_bin: str, path: Path) -> Tuple[Optional[int], Optional[int]]:
    """Ambil width x height video dari metadata 'ffmpeg -i'."""
    try:
        result = subprocess.run(
            [ffmpeg_bin, "-i", str(path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=10,
        )
        match = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", result.stdout)
        if match:
            return int(match.group(1)), int(match.group(2))
    except Exception:
        pass
    return None, None


def inspect_video_detailed(ffmpeg_bin: str, path: Path) -> dict:
    """Membaca detail teknis video (codec, resolusi, fps, audio, bitrate)."""
    info = {
        "path": path,
        "name": path.name,
        "size": path.stat().st_size,
        "duration": None,
        "bitrate": None,
        "width": None,
        "height": None,
        "aspect": None,
        "fps": None,
        "v_codec": None,
        "pix_fmt": None,
        "a_codec": None,
        "a_rate": None,
        "a_channels": None,
    }
    try:
        res = subprocess.run(
            [ffmpeg_bin, "-i", str(path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=10,
        )
        out = res.stdout

        # Durasi & Total Bitrate
        dur_match = DURATION_RE.search(out)
        if dur_match:
            h, m, s = dur_match.groups()
            info["duration"] = int(h) * 3600 + int(m) * 60 + float(s)
            if info["duration"]:
                info["bitrate"] = (info["size"] * 8) / info["duration"]

        # Video stream
        v_match = re.search(r"Stream #\d+:\d+.*?: Video: ([^,\n]+), ([^,\n]+), (\d{2,5})x(\d{2,5})", out)
        if v_match:
            info["v_codec"] = v_match.group(1).strip()
            info["pix_fmt"] = v_match.group(2).strip()
            w, h = int(v_match.group(3)), int(v_match.group(4))
            info["width"], info["height"] = w, h
            if h > 0:
                gcd_val = _calc_gcd(w, h)
                info["aspect"] = f"{w // gcd_val}:{h // gcd_val}"

        fps_match = re.search(r"(\d+(?:\.\d+)?) fps", out)
        if fps_match:
            info["fps"] = float(fps_match.group(1))

        # Audio stream
        a_match = re.search(r"Stream #\d+:\d+.*?: Audio: ([^,\n]+), (\d+ Hz), ([^,\n]+)", out)
        if a_match:
            info["a_codec"] = a_match.group(1).strip()
            info["a_rate"] = a_match.group(2).strip()
            info["a_channels"] = a_match.group(3).strip()

    except Exception:
        pass
    return info


def _calc_gcd(a: int, b: int) -> int:
    while b:
        a, b = b, a % b
    return a if a > 0 else 1


# =============================================================================
# SETTING ENCODE & ARGUMEN FFMPEG
# =============================================================================

X264_PRESETS = {
    "ultrafast", "superfast", "veryfast", "faster", "fast",
    "medium", "slow", "slower", "veryslow",
}
X264_PROFILES = {"baseline", "main", "high"}


def parse_rate_value(raw: str) -> Optional[str]:
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([kKmM])", raw.strip())
    if not m:
        return None
    val, unit = m.group(1), m.group(2)
    return f"{val}{unit.upper() if unit in 'mM' else unit.lower()}"


def build_filter_chain(extra_vf: Optional[List[str]] = None) -> List[str]:
    """Membangun rantai filter video (-vf) berdasarkan setting aktif."""
    s = config_mgr.encode_settings
    vf_list = []
    if extra_vf:
        vf_list.extend(extra_vf)

    # Force framerate (mis. 60fps)
    force_fps = int(s.get("force_fps", 0))
    if force_fps > 0:
        vf_list.append(f"fps={force_fps}")

    # Smart sharpening (Unsharp filter anti-blur)
    if s.get("sharpen", False):
        st = s.get("sharpen_strength", "0.7")
        vf_list.append(f"unsharp=5:5:{st}:5:5:0.0")

    return vf_list


def build_audio_args() -> List[str]:
    """Membangun argumen audio termasuk Loudness Normalization jika aktif."""
    s = config_mgr.encode_settings
    args = ["-c:a", "aac", "-b:a", s.get("audio_bitrate", "256k")]
    if s.get("normalize_audio", False):
        args.extend(["-af", "loudnorm=I=-14:TP=-1.5:LRA=11"])
    return args


def h264_encode_args(extra_vf: Optional[List[str]] = None) -> List[str]:
    """Argumen encode H.264 + AAC lengkap untuk Patch & Crop.
    Mendukung konfigurasi tersimpan, hardware encoder, dan smart filter."""
    s = config_mgr.encode_settings
    encoder = s.get("encoder", "libx264")

    cmd = []

    # Filter video (-vf) jika ada
    vf_chain = build_filter_chain(extra_vf)
    if vf_chain:
        cmd.extend(["-vf", ",".join(vf_chain)])

    # Video Codec Arguments
    if encoder == "h264_nvenc":
        cmd.extend([
            "-c:v", "h264_nvenc",
            "-preset", "p5",
            "-profile:v", s.get("profile", "high"),
            "-pix_fmt", "yuv420p",
            "-b:v", s.get("bitrate", "30M"),
            "-maxrate", s.get("maxrate", "35M"),
            "-bufsize", s.get("bufsize", "70M"),
            "-g", str(s.get("gop", 60)),
        ])
    elif encoder == "h264_videotoolbox":
        cmd.extend([
            "-c:v", "h264_videotoolbox",
            "-profile:v", s.get("profile", "high"),
            "-pix_fmt", "yuv420p",
            "-b:v", s.get("bitrate", "30M"),
            "-maxrate", s.get("maxrate", "35M"),
            "-g", str(s.get("gop", 60)),
        ])
    elif encoder == "h264_qsv":
        cmd.extend([
            "-c:v", "h264_qsv",
            "-preset", "slow",
            "-profile:v", s.get("profile", "high"),
            "-pix_fmt", "nv12",
            "-b:v", s.get("bitrate", "30M"),
            "-maxrate", s.get("maxrate", "35M"),
            "-bufsize", s.get("bufsize", "70M"),
            "-g", str(s.get("gop", 60)),
        ])
    else:
        # Default libx264 (CPU - highest compatibility and compression efficiency)
        cmd.extend([
            "-c:v", "libx264",
            "-preset", s.get("preset", "slow"),
            "-profile:v", s.get("profile", "high"),
            "-level", s.get("level", "4.2"),
            "-pix_fmt", "yuv420p",
            "-b:v", s.get("bitrate", "30M"),
            "-maxrate", s.get("maxrate", "35M"),
            "-bufsize", s.get("bufsize", "70M"),
            "-g", str(s.get("gop", 60)),
            "-keyint_min", str(s.get("gop", 60)),
            "-sc_threshold", "0",
            "-bf", "2",
        ])

    # Audio Arguments
    cmd.extend(build_audio_args())

    # Metadata & Container optimization
    if s.get("strip_metadata", True):
        cmd.extend(["-map_metadata", "-1"])
    cmd.extend(["-movflags", "+faststart"])

    return cmd


def build_ffmpeg_cmd(ffmpeg_bin: str, input_path: Path, output_path: Path) -> List[str]:
    return [ffmpeg_bin, "-y", "-i", str(input_path), *h264_encode_args(), str(output_path)]


def run_ffmpeg_encode(cmd: List[str], duration: Optional[float]) -> str:
    log_lines = []
    start = time.time()
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
    )
    speed_str = ""
    try:
        for line in proc.stdout:
            log_lines.append(line)
            elapsed = time.time() - start
            match = TIME_RE.search(line)
            sp_match = SPEED_RE.search(line)
            if sp_match:
                speed_str = f" {C.YELLOW}{sp_match.group(1)}x{C.RESET}"

            if match and duration:
                h, m, s = match.groups()
                cur = int(h) * 3600 + int(m) * 60 + float(s)
                bar = draw_progress(cur, duration)
                sys.stdout.write(
                    f"\r{C.BOLD}{C.CYAN}[ENC]{C.RESET} {bar}{speed_str}  {C.GRAY}{elapsed:5.1f}s{C.RESET}   "
                )
            else:
                sys.stdout.write(
                    f"\r{C.BOLD}{C.CYAN}[ENC]{C.RESET} {C.GRAY}memproses...{C.RESET}{speed_str} {C.GRAY}{elapsed:5.1f}s{C.RESET}   "
                )
            sys.stdout.flush()
        proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        proc.wait()
        sys.stdout.write("\n")
        raise
    sys.stdout.write("\n")

    if proc.returncode != 0:
        raise subprocess.CalledProcessError(
            proc.returncode, cmd, output="".join(log_lines)
        )
    return "".join(log_lines)


def handle_ffmpeg_error(err: subprocess.CalledProcessError) -> None:
    print_tag("ERR", f"Proses ffmpeg gagal (exit code {err.returncode}).", C.RED)
    log = (err.output or "").strip()
    if log:
        tail = "\n".join(log.splitlines()[-15:])
        print(f"{C.GRAY}{'─' * 58}{C.RESET}")
        print(f"{C.RED}{tail}{C.RESET}")
        print(f"{C.GRAY}{'─' * 58}{C.RESET}")


# =============================================================================
# FOLDER PUSAT & GALLERY INDEXING
# =============================================================================

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".3gp", ".ts", ".flv"}


def get_central_dir() -> Path:
    """Folder pusat: diambil dari ConfigManager (bisa di-override user)."""
    return config_mgr.get_central_dir()


def list_video_files(directory: Path) -> List[Path]:
    """Daftar video mentah di folder pusat (file hasil encode disembunyikan)."""
    hidden_prefixes = ("encode_", "stabil_", "crop_")
    try:
        files = [
            f for f in directory.iterdir()
            if f.is_file()
            and f.suffix.lower() in VIDEO_EXTS
            and not f.name.lower().startswith(hidden_prefixes)
        ]
        return sorted(files, key=lambda f: f.name.lower())
    except Exception:
        return []


def trigger_media_scan(file_path: Path) -> Tuple[bool, Optional[str]]:
    """Memicu media scan agar file muncul di Galeri / File Manager Android."""
    if shutil.which("termux-media-scan"):
        try:
            subprocess.run(
                ["termux-media-scan", str(file_path)],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
            return True, "termux-media-scan"
        except Exception:
            pass

    if shutil.which("am"):
        try:
            subprocess.run(
                [
                    "am", "broadcast",
                    "-a", "android.intent.action.MEDIA_SCANNER_SCAN_FILE",
                    "-d", f"file://{file_path}",
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
            return True, "am broadcast"
        except Exception:
            pass

    return False, None


# =============================================================================
# RINGKASAN STATISTIK
# =============================================================================


def print_summary(input_path: Path, output_path: Path, in_stats: dict, out_stats: dict) -> None:
    if in_stats["size"]:
        delta_pct = ((out_stats["size"] - in_stats["size"]) / in_stats["size"]) * 100
    else:
        delta_pct = 0.0
    sign = "+" if delta_pct >= 0 else ""

    print()
    print_box([f"{C.CYAN}{C.BOLD}RINGKASAN HASIL ENCODING{C.RESET}"], color=C.CYAN)
    print(f"  {C.GRAY}File Asal    :{C.RESET} {input_path.name}")
    print(f"  {C.GRAY}File Hasil   :{C.RESET} {output_path.name}")
    print(f"  {C.GRAY}{'─' * 56}{C.RESET}")
    print(f"  {C.CYAN}Ukuran Asal  :{C.RESET} {human_size(in_stats['size'])}")
    print(f"  {C.MAGENTA}Ukuran Hasil :{C.RESET} {human_size(out_stats['size'])}")
    print(f"  {C.YELLOW}Perubahan    :{C.RESET} {sign}{delta_pct:.2f}%")
    print(f"  {C.GRAY}{'─' * 56}{C.RESET}")
    print(f"  {C.CYAN}Bitrate Asal :{C.RESET} {human_bitrate(in_stats['bitrate'])}")
    print(f"  {C.MAGENTA}Bitrate Hasil:{C.RESET} {human_bitrate(out_stats['bitrate'])}")
    print()


# =============================================================================
# PROSES UTAMA (PATCH VIDEO)
# =============================================================================


def process_video(ffmpeg_bin: str, input_path: Path, output_name: Optional[str] = None) -> Path:
    out_dir = get_central_dir()
    if output_name:
        out_name = output_name if output_name.lower().endswith(".mp4") else f"{output_name}.mp4"
    else:
        out_name = f"encode_{input_path.stem}.mp4"
    output_path = out_dir / out_name

    print()
    print_box([f"{C.CYAN}{C.BOLD}MEMULAI ENCODING TIKTOK PATCH{C.RESET}"], color=C.CYAN)
    print_tag("INFO", f"Input  : {input_path.name}", C.CYAN)
    print_tag("INFO", f"Output : {output_path.name}", C.CYAN)
    cfg = config_mgr.encode_settings
    print_tag(
        "CONF",
        f"Bitrate: {cfg.get('bitrate')} | FPS: {cfg.get('force_fps') or 'Asli'} | "
        f"Sharpen: {'ON' if cfg.get('sharpen') else 'OFF'} | "
        f"Normalize Audio: {'ON' if cfg.get('normalize_audio') else 'OFF'}",
        C.PURPLE,
    )
    print()

    in_stats = get_file_stats(ffmpeg_bin, input_path)
    cmd = build_ffmpeg_cmd(ffmpeg_bin, input_path, output_path)
    run_ffmpeg_encode(cmd, in_stats["duration"])

    if not output_path.is_file():
        raise RuntimeError("File output tidak ditemukan setelah proses ffmpeg selesai.")

    out_stats = get_file_stats(ffmpeg_bin, output_path)
    print_tag("OK", "Encoding selesai.", C.GREEN)
    print_summary(input_path, output_path, in_stats, out_stats)

    scanned, method = trigger_media_scan(output_path)
    if scanned:
        print_tag("OK", f"Media scan berhasil ({method}) - file muncul di Galeri.", C.GREEN)

    print_tag("OK", f"Tersimpan di: {output_path}", C.GREEN)
    return output_path


# =============================================================================
# RE-ENCODE + CROP RESOLUSI 1080p
# =============================================================================

CROP_RATIOS = {
    "16:9": {"w": 1920, "h": 1080, "label": "16:9  (Landscape - YouTube)", "slug": "16x9"},
    "9:16": {"w": 1080, "h": 1920, "label": "9:16  (Portrait - TikTok/Reels/Shorts)", "slug": "9x16"},
    "4:5":  {"w": 1080, "h": 1350, "label": "4:5   (Portrait - Instagram Feed)", "slug": "4x5"},
    "1:1":  {"w": 1080, "h": 1080, "label": "1:1   (Persegi - Instagram)", "slug": "1x1"},
    "4:3":  {"w": 1440, "h": 1080, "label": "4:3   (Klasik)", "slug": "4x3"},
}
CROP_RATIO_ORDER = ["16:9", "9:16", "4:5", "1:1", "4:3"]


def compute_crop_box(iw: int, ih: int, target_w: int, target_h: int) -> Tuple[int, int, int, int]:
    target_ratio = target_w / target_h
    input_ratio = iw / ih

    if abs(input_ratio - target_ratio) < 1e-6:
        return iw, ih, 0, 0

    if input_ratio > target_ratio:
        crop_h = ih
        crop_w = int(round(ih * target_ratio))
        crop_w -= crop_w % 2
        crop_x = (iw - crop_w) // 2
        crop_y = 0
    else:
        crop_w = iw
        crop_h = int(round(iw / target_ratio))
        crop_h -= crop_h % 2
        crop_x = 0
        crop_y = (ih - crop_h) // 2

    return crop_w, crop_h, crop_x, crop_y


def build_crop_encode_cmd(
    ffmpeg_bin: str,
    input_path: Path,
    output_path: Path,
    crop_w: int,
    crop_h: int,
    crop_x: int,
    crop_y: int,
    target_w: int,
    target_h: int,
) -> List[str]:
    crop_scale_vf = [f"crop={crop_w}:{crop_h}:{crop_x}:{crop_y}", f"scale={target_w}:{target_h}:flags=lanczos"]
    return [ffmpeg_bin, "-y", "-i", str(input_path), *h264_encode_args(extra_vf=crop_scale_vf), str(output_path)]


def crop_video(ffmpeg_bin: str, input_path: Path, ratio_key: str) -> Path:
    info = CROP_RATIOS[ratio_key]
    target_w, target_h = info["w"], info["h"]

    out_dir = get_central_dir()
    output_path = out_dir / f"crop_{info['slug']}_{input_path.stem}.mp4"

    iw, ih = get_video_dimensions(ffmpeg_bin, input_path)
    if not iw or not ih:
        raise RuntimeError("Tidak bisa membaca resolusi video input (file mungkin rusak/tidak didukung).")

    crop_w, crop_h, crop_x, crop_y = compute_crop_box(iw, ih, target_w, target_h)

    print()
    print_box(
        [f"{C.CYAN}{C.BOLD}RE-ENCODE + CROP{C.RESET}", f"{C.GRAY}Target: {info['label']}{C.RESET}"],
        color=C.CYAN,
    )
    print_tag("INFO", f"Input         : {input_path.name}", C.CYAN)
    print_tag("INFO", f"Resolusi Asal : {iw}x{ih}", C.CYAN)
    print_tag("INFO", f"Area Crop     : {crop_w}x{crop_h} (offset {crop_x},{crop_y})", C.CYAN)
    print_tag("INFO", f"Resolusi Hasil: {target_w}x{target_h}", C.CYAN)
    print()

    in_stats = get_file_stats(ffmpeg_bin, input_path)
    cmd = build_crop_encode_cmd(ffmpeg_bin, input_path, output_path, crop_w, crop_h, crop_x, crop_y, target_w, target_h)
    run_ffmpeg_encode(cmd, in_stats["duration"])

    if not output_path.is_file():
        raise RuntimeError("File output tidak ditemukan setelah proses ffmpeg selesai.")

    out_stats = get_file_stats(ffmpeg_bin, output_path)
    print_tag("OK", "Re-encode + crop selesai.", C.GREEN)
    print_summary(input_path, output_path, in_stats, out_stats)

    scanned, method = trigger_media_scan(output_path)
    if scanned:
        print_tag("OK", f"Media scan berhasil ({method}) - file muncul di Galeri.", C.GREEN)

    print_tag("OK", f"Tersimpan di: {output_path}", C.GREEN)
    return output_path


# =============================================================================
# STABILISASI VIDEO (ANTI-GOYANG)
# =============================================================================

STAB_L1_RHO = 1.0
STAB_L1_ITERS = 60

STAB_SMOOTHING_ORDER = ["ringan", "sedang", "kuat", "maksimal"]
STAB_SMOOTHING_PRESETS = {
    "ringan":   {"radius_sec": 0.25, "lambda_ratio": 0.006},
    "sedang":   {"radius_sec": 0.50, "lambda_ratio": 0.012},
    "kuat":     {"radius_sec": 1.00, "lambda_ratio": 0.024},
    "maksimal": {"radius_sec": 2.00, "lambda_ratio": 0.050},
}


def check_stabilization_deps() -> Tuple[bool, List[str]]:
    missing = []
    try:
        import numpy  # noqa: F401
    except ImportError:
        missing.append("numpy")
    try:
        import cv2  # noqa: F401
    except ImportError:
        missing.append("opencv-python")
    try:
        import scipy.sparse  # noqa: F401
        import scipy.sparse.linalg  # noqa: F401
    except ImportError:
        missing.append("scipy")
    return (not missing), missing


def stab_analysis_scale(w: int, h: int) -> float:
    max_dim = config_mgr.stab_settings.get("analysis_max_dim", 480)
    return min(1.0, float(max_dim) / float(max(w, h)))


def stab_encode_args() -> List[str]:
    s = config_mgr.stab_settings
    return [
        "-c:v", "libx264",
        "-preset", s.get("preset", "fast"),
        "-profile:v", "high",
        "-level", "4.2",
        "-pix_fmt", "yuv420p",
        "-b:v", s.get("bitrate", "16M"),
        "-maxrate", s.get("maxrate", "20M"),
        "-bufsize", s.get("bufsize", "40M"),
        "-g", "60",
        "-keyint_min", "60",
        "-sc_threshold", "0",
        "-bf", "2",
        "-c:a", "aac",
        "-b:a", s.get("audio_bitrate", "192k"),
        "-movflags", "+faststart",
    ]


def l1_trend_filter(Y, lam: float, rho: float = STAB_L1_RHO, n_iter: int = STAB_L1_ITERS):
    import numpy as np
    import scipy.sparse as sp
    import scipy.sparse.linalg as spla

    T = Y.shape[0]
    if T <= 4:
        return Y.copy()

    e = np.ones(T)
    D2 = sp.diags([e[:-2], -2 * e[:-1], e], [0, 1, 2], shape=(T - 2, T)).tocsr()
    A = (sp.eye(T) + rho * (D2.T @ D2)).tocsc()
    lu = spla.splu(A)

    X = Y.copy()
    Z = D2 @ X
    U = np.zeros_like(Z)
    thresh = lam / rho
    for _ in range(n_iter):
        X = lu.solve(Y + rho * (D2.T @ (Z - U)))
        DX = D2 @ X
        V = DX + U
        Z = np.sign(V) * np.maximum(np.abs(V) - thresh, 0.0)
        U = U + DX - Z
    return X


def moving_average_signal(curve, radius: int):
    import numpy as np
    window = 2 * radius + 1
    kernel = np.ones(window) / window
    padded = np.pad(curve, (radius, radius), mode="edge")
    return np.convolve(padded, kernel, mode="same")[radius:-radius]


def analyze_motion_fast(input_path: Path, w: int, h: int, n_frames: int, progress_cb):
    import cv2
    import numpy as np

    scale = stab_analysis_scale(w, h)
    aw, ah = max(2, int(w * scale)), max(2, int(h * scale))
    inv_scale = 1.0 / scale

    cap = cv2.VideoCapture(str(input_path))
    ok, prev = cap.read()
    if not ok:
        cap.release()
        raise RuntimeError("Tidak bisa membaca frame video.")
    prev_small = cv2.resize(prev, (aw, ah), interpolation=cv2.INTER_AREA)
    prev_gray = cv2.cvtColor(prev_small, cv2.COLOR_BGR2GRAY)

    transforms = []
    idx = 0
    while True:
        ok, cur = cap.read()
        if not ok:
            break
        cur_small = cv2.resize(cur, (aw, ah), interpolation=cv2.INTER_AREA)
        cur_gray = cv2.cvtColor(cur_small, cv2.COLOR_BGR2GRAY)

        prev_pts = cv2.goodFeaturesToTrack(
            prev_gray, maxCorners=200, qualityLevel=0.01, minDistance=max(4, int(15 * scale))
        )
        dx = dy = da = 0.0
        if prev_pts is not None and len(prev_pts) >= 10:
            cur_pts, status, _err = cv2.calcOpticalFlowPyrLK(prev_gray, cur_gray, prev_pts, None)
            status = status.reshape(-1)
            p0, p1 = prev_pts[status == 1], cur_pts[status == 1]
            if len(p0) >= 6:
                m, _inliers = cv2.estimateAffinePartial2D(p0, p1, method=cv2.RANSAC)
                if m is not None:
                    dx, dy = float(m[0, 2]) * inv_scale, float(m[1, 2]) * inv_scale
                    da = float(np.arctan2(m[1, 0], m[0, 0]))
        transforms.append((dx, dy, da))
        prev_gray = cur_gray
        idx += 1
        if progress_cb:
            progress_cb(idx, n_frames)

    cap.release()
    return np.array(transforms, dtype=np.float64)


def analyze_motion_mesh(input_path: Path, w: int, h: int, n_frames: int, progress_cb):
    import cv2
    import numpy as np

    scale = stab_analysis_scale(w, h)
    aw, ah = max(2, int(w * scale)), max(2, int(h * scale))
    inv_scale = 1.0 / scale

    grid = config_mgr.stab_settings.get("grid", 16)
    vx = np.linspace(0, w - 1, grid + 1)
    vy = np.linspace(0, h - 1, grid + 1)
    grid_x, grid_y = np.meshgrid(vx, vy)
    xs_a = (grid_x * scale).astype(int).clip(0, aw - 1)
    ys_a = (grid_y * scale).astype(int).clip(0, ah - 1)

    dis = cv2.DISOpticalFlow_create(cv2.DISOPTICAL_FLOW_PRESET_MEDIUM)

    cap = cv2.VideoCapture(str(input_path))
    ok, prev = cap.read()
    if not ok:
        cap.release()
        raise RuntimeError("Tidak bisa membaca frame video.")
    prev_small = cv2.resize(prev, (aw, ah), interpolation=cv2.INTER_AREA)
    prev_gray = cv2.cvtColor(prev_small, cv2.COLOR_BGR2GRAY)

    flows = []
    idx = 0
    while True:
        ok, cur = cap.read()
        if not ok:
            break
        cur_small = cv2.resize(cur, (aw, ah), interpolation=cv2.INTER_AREA)
        cur_gray = cv2.cvtColor(cur_small, cv2.COLOR_BGR2GRAY)
        flow = dis.calc(prev_gray, cur_gray, None)
        vertex_flow = flow[ys_a, xs_a] * inv_scale
        flows.append(vertex_flow)
        prev_gray = cur_gray
        idx += 1
        if progress_cb:
            progress_cb(idx, n_frames)

    cap.release()
    return np.array(flows, dtype=np.float64)


def dynamic_crop(diff_dx, diff_dy, w: int, h: int):
    import numpy as np
    max_dx = float(np.abs(diff_dx).max()) if diff_dx.size else 0.0
    max_dy = float(np.abs(diff_dy).max()) if diff_dy.size else 0.0
    max_crop_ratio = config_mgr.stab_settings.get("max_crop_ratio", 0.15)
    crop_w = int(min(max_crop_ratio * w, max_dx * 1.15))
    crop_h = int(min(max_crop_ratio * h, max_dy * 1.15))
    return max(0, crop_w), max(0, crop_h)


def build_stabilize_encode_cmd(ffmpeg_bin: str, input_path: Path, output_path: Path, w: int, h: int, fps: float) -> List[str]:
    return [
        ffmpeg_bin, "-y",
        "-f", "rawvideo", "-pixel_format", "bgr24",
        "-video_size", f"{w}x{h}", "-framerate", str(fps),
        "-i", "-",
        "-i", str(input_path),
        "-map", "0:v:0", "-map", "1:a:0?",
        *stab_encode_args(),
        "-shortest",
        str(output_path),
    ]


def pipe_frames_to_ffmpeg(cmd: List[str], frame_iter, total_frames: int, progress_label: str) -> None:
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    log_lines = []

    def _reader():
        for chunk in proc.stdout:
            log_lines.append(chunk)

    reader_thread = threading.Thread(target=_reader, daemon=True)
    reader_thread.start()

    start = time.time()
    count = 0
    try:
        for frame_bytes in frame_iter:
            proc.stdin.write(frame_bytes)
            count += 1
            elapsed = time.time() - start
            bar = draw_progress(count, total_frames)
            sys.stdout.write(
                f"\r{C.BOLD}{C.CYAN}[{progress_label}]{C.RESET} {bar}  {C.GRAY}{elapsed:5.1f}s{C.RESET}   "
            )
            sys.stdout.flush()
    except BrokenPipeError:
        pass
    except KeyboardInterrupt:
        try:
            proc.stdin.close()
        except Exception:
            pass
        proc.terminate()
        proc.wait()
        reader_thread.join()
        sys.stdout.write("\n")
        raise

    try:
        proc.stdin.close()
    except Exception:
        pass
    proc.wait()
    reader_thread.join()
    sys.stdout.write("\n")

    if proc.returncode != 0:
        raise subprocess.CalledProcessError(
            proc.returncode, cmd, output=b"".join(log_lines).decode(errors="replace")
        )


def render_stabilized_fast(ffmpeg_bin, input_path, output_path, w, h, fps, n_frames, difference, crop_w, crop_h):
    import cv2
    import numpy as np

    cap = cv2.VideoCapture(str(input_path))
    ok0, frame0 = cap.read()
    if not ok0:
        cap.release()
        raise RuntimeError("Tidak bisa membaca ulang video untuk rendering.")

    def frame_gen():
        yield frame0.tobytes()
        idx = 0
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            if idx < len(difference):
                dx, dy, da = difference[idx]
            else:
                dx = dy = da = 0.0
            mat = np.array(
                [[np.cos(da), -np.sin(da), dx], [np.sin(da), np.cos(da), dy]],
                dtype=np.float64,
            )
            stabilized = cv2.warpAffine(frame, mat, (w, h), borderMode=cv2.BORDER_REPLICATE)
            if crop_w > 0 or crop_h > 0:
                cropped = stabilized[crop_h:h - crop_h, crop_w:w - crop_w]
                stabilized = cv2.resize(cropped, (w, h))
            yield stabilized.tobytes()
            idx += 1
        cap.release()

    cmd = build_stabilize_encode_cmd(ffmpeg_bin, input_path, output_path, w, h, fps)
    pipe_frames_to_ffmpeg(cmd, frame_gen(), n_frames, "WARP")


def render_stabilized_mesh(ffmpeg_bin, input_path, output_path, w, h, fps, n_frames, difference, crop_w, crop_h):
    import cv2
    import numpy as np

    cap = cv2.VideoCapture(str(input_path))
    ok0, frame0 = cap.read()
    if not ok0:
        cap.release()
        raise RuntimeError("Tidak bisa membaca ulang video untuk rendering.")

    base_x, base_y = np.meshgrid(np.arange(w, dtype=np.float32), np.arange(h, dtype=np.float32))

    def frame_gen():
        yield frame0.tobytes()
        idx = 0
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            if idx < len(difference):
                di = difference[idx]
                dxf = cv2.resize(di[..., 0].astype(np.float32), (w, h), interpolation=cv2.INTER_LINEAR)
                dyf = cv2.resize(di[..., 1].astype(np.float32), (w, h), interpolation=cv2.INTER_LINEAR)
                map_x = base_x - dxf
                map_y = base_y - dyf
                stabilized = cv2.remap(frame, map_x, map_y, interpolation=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
            else:
                stabilized = frame
            if crop_w > 0 or crop_h > 0:
                cropped = stabilized[crop_h:h - crop_h, crop_w:w - crop_w]
                stabilized = cv2.resize(cropped, (w, h))
            yield stabilized.tobytes()
            idx += 1
        cap.release()

    cmd = build_stabilize_encode_cmd(ffmpeg_bin, input_path, output_path, w, h, fps)
    pipe_frames_to_ffmpeg(cmd, frame_gen(), n_frames, "WARP")


def stabilize_video(ffmpeg_bin: str, input_path: Path, mode: str) -> Path:
    import cv2
    import numpy as np

    out_dir = get_central_dir()
    output_path = out_dir / f"stabil_{input_path.stem}.mp4"

    probe = cv2.VideoCapture(str(input_path))
    w = int(probe.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(probe.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = probe.get(cv2.CAP_PROP_FPS) or 30.0
    n_frames = int(probe.get(cv2.CAP_PROP_FRAME_COUNT))
    probe.release()

    if w <= 0 or h <= 0:
        raise RuntimeError("Tidak bisa membaca dimensi video (file mungkin rusak/tidak didukung).")

    stab_cfg = config_mgr.stab_settings
    print()
    print_box(
        [f"{C.CYAN}{C.BOLD}STABILISASI VIDEO{C.RESET}", f"{C.GRAY}Mode: {mode.capitalize()}{C.RESET}"],
        color=C.CYAN,
    )
    print_tag("INFO", f"Input  : {input_path.name}", C.CYAN)
    print_tag("INFO", f"Output : {output_path.name}", C.CYAN)
    print_tag("INFO", f"Smoothing: {stab_cfg.get('smoothing')} | Grid: {stab_cfg.get('grid')}x{stab_cfg.get('grid')}", C.CYAN)
    if mode == "kualitas":
        print_tag("WARN", "Menganalisis optical flow padat per frame - mohon bersabar...", C.YELLOW)
    else:
        print_tag("INFO", "Memakai pelacakan fitur ringan (mode cepat).", C.CYAN)
    print()

    smoothing_preset = STAB_SMOOTHING_PRESETS[stab_cfg.get("smoothing", "sedang")]

    def make_progress_cb(label):
        def cb(cur, total):
            bar = draw_progress(cur, total)
            sys.stdout.write(f"\r{C.BOLD}{C.MAGENTA}[{label}]{C.RESET} {bar}   ")
            sys.stdout.flush()
        return cb

    if mode == "kualitas":
        flows = analyze_motion_mesh(input_path, w, h, n_frames, make_progress_cb("FLOW"))
        sys.stdout.write("\n")
        traj = np.cumsum(flows, axis=0)
        T = traj.shape[0]
        Y = traj.reshape(T, -1)
        lam = smoothing_preset["lambda_ratio"] * w
        smooth = l1_trend_filter(Y, lam=lam).reshape(traj.shape)
        difference = smooth - traj
        crop_w, crop_h = dynamic_crop(difference[..., 0], difference[..., 1], w, h)
        render_stabilized_mesh(ffmpeg_bin, input_path, output_path, w, h, fps, n_frames, difference, crop_w, crop_h)
    else:
        transforms = analyze_motion_fast(input_path, w, h, n_frames, make_progress_cb("FLOW"))
        sys.stdout.write("\n")
        traj = np.cumsum(transforms, axis=0)
        radius = max(8, round(fps * smoothing_preset["radius_sec"]))
        smooth = np.copy(traj)
        for k in range(3):
            smooth[:, k] = moving_average_signal(traj[:, k], radius)
        difference = smooth - traj
        crop_w, crop_h = dynamic_crop(difference[:, 0], difference[:, 1], w, h)
        render_stabilized_fast(ffmpeg_bin, input_path, output_path, w, h, fps, n_frames, difference, crop_w, crop_h)

    if not output_path.is_file():
        raise RuntimeError("File output tidak ditemukan setelah proses selesai.")

    in_stats = get_file_stats(ffmpeg_bin, input_path)
    out_stats = get_file_stats(ffmpeg_bin, output_path)
    print_tag("OK", "Stabilisasi selesai.", C.GREEN)
    print_summary(input_path, output_path, in_stats, out_stats)

    scanned, method = trigger_media_scan(output_path)
    if scanned:
        print_tag("OK", f"Media scan berhasil ({method}) - file muncul di Galeri.", C.GREEN)

    print_tag("OK", f"Tersimpan di: {output_path}", C.GREEN)
    return output_path


# =============================================================================
# BATCH SELECTION & INPUT HANDLER
# =============================================================================


def pick_videos_interactive(central_dir: Path) -> List[Path]:
    """Menampilkan daftar video di folder pusat dengan dukungan:
    - Satu file (misal: '1')
    - Beberapa file (misal: '1, 3, 5' atau '1-4')
    - Semua file ('all' atau 'a')
    - Path file eksternal langsung (drag and drop)
    """
    videos = list_video_files(central_dir)

    print()
    print_box(
        [
            f"{C.CYAN}{C.BOLD}PILIH VIDEO / INPUT{C.RESET}",
            f"{C.GRAY}Folder Pusat: {central_dir}{C.RESET}",
            f"{C.GRAY}Tips: Bisa pilih multiple (1,3,5), range (1-4), 'all', atau paste path{C.RESET}",
        ],
        color=C.PURPLE,
    )

    if not videos:
        print(f"  {C.YELLOW}(Folder pusat saat ini belum berisi video mentah){C.RESET}")
    else:
        for idx, f in enumerate(videos, start=1):
            size_label = human_size(f.stat().st_size)
            print(f"  {C.CYAN}[{idx}]{C.RESET} {f.name} {C.GRAY}({size_label}){C.RESET}")
    print()

    try:
        prompt_txt = (
            f"{C.MAGENTA}{C.BOLD}➤ Pilih nomor/nama/path ('all' untuk semua, 0 batal): {C.RESET}"
        )
        sel_raw = input(prompt_txt).strip()
    except EOFError:
        print()
        return []

    if sel_raw in ("", "0"):
        return []

    # Cek jika input adalah path langsung ke file di disk
    direct_p = Path(sel_raw.strip("'\"")).expanduser()
    if direct_p.is_file():
        ok, err = validate_input_file(direct_p)
        if ok:
            return [direct_p]
        else:
            print_tag("ERR", err, C.RED)
            return []

    if not videos:
        print_tag("WARN", "Tidak ada video di folder pusat untuk dipilih dengan nomor.", C.YELLOW)
        return []

    # Semua file
    if sel_raw.lower() in ("all", "a", "*"):
        return list(videos)

    # Parsing nomor, comma, range (mis. 1, 3, 5 atau 1-3)
    selected_indices = set()
    parts = re.split(r"[\s,]+", sel_raw)
    for part in parts:
        if not part:
            continue
        if "-" in part:
            m = re.match(r"^(\d+)-(\d+)$", part)
            if m:
                s_idx, e_idx = int(m.group(1)), int(m.group(2))
                for i in range(min(s_idx, e_idx), max(s_idx, e_idx) + 1):
                    if 1 <= i <= len(videos):
                        selected_indices.add(i - 1)
        elif part.isdigit():
            i = int(part)
            if 1 <= i <= len(videos):
                selected_indices.add(i - 1)
        else:
            # Cari berdasarkan substring nama file
            found = False
            for v_idx, v_file in enumerate(videos):
                if part.lower() in v_file.name.lower():
                    selected_indices.add(v_idx)
                    found = True
            if not found:
                print_tag("WARN", f"Pilihan '{part}' tidak dikenali.", C.YELLOW)

    if not selected_indices:
        print_tag("WARN", "Tidak ada video yang valid dipilih.", C.YELLOW)
        return []

    chosen = [videos[i] for i in sorted(selected_indices)]
    return chosen


# =============================================================================
# MENU SETTING ENCODE (PATCH & CROP)
# =============================================================================


def print_current_encode_settings() -> None:
    s = config_mgr.encode_settings
    fps_text = f"{s.get('force_fps')} fps" if s.get("force_fps") else "Pertahankan FPS Asli"
    sharpen_text = f"[AKTIF] (Kekuatan: {s.get('sharpen_strength')})" if s.get("sharpen") else "[NONAKTIF]"
    loudnorm_text = "[AKTIF]" if s.get("normalize_audio") else "[NONAKTIF]"
    meta_text = "[AKTIF]" if s.get("strip_metadata") else "[NONAKTIF]"

    print_box(
        [
            f"{C.CYAN}{C.BOLD}SETTING ENCODE - PATCH & CROP{C.RESET}",
            f"{C.GRAY}Encoder/Preset/Prof  : {s.get('encoder')} / {s.get('preset')} / {s.get('profile')} (Lvl {s.get('level')}){C.RESET}",
            f"{C.GRAY}Video Bitrate        : {s.get('bitrate')} (max {s.get('maxrate')}, buf {s.get('bufsize')}, GOP {s.get('gop')}){C.RESET}",
            f"{C.GRAY}Audio Bitrate        : {s.get('audio_bitrate')}{C.RESET}",
            f"{C.GRAY}Target Framerate     : {fps_text}{C.RESET}",
            f"{C.GRAY}Smart Sharpening     : {sharpen_text}{C.RESET}",
            f"{C.GRAY}Audio Loudnorm (EBU) : {loudnorm_text}{C.RESET}",
            f"{C.GRAY}Bersihkan Metadata   : {meta_text}{C.RESET}",
        ],
        color=C.PURPLE,
    )


def edit_encode_custom(ffmpeg_bin: str) -> None:
    s = config_mgr.encode_settings
    while True:
        print()
        print_current_encode_settings()
        print(f"  {C.CYAN}[1]{C.RESET} Ganti Encoder (libx264 / Hardware Acceleration)")
        print(f"  {C.CYAN}[2]{C.RESET} Preset Kecepatan x264")
        print(f"  {C.CYAN}[3]{C.RESET} Video Bitrate (otomatis update maxrate & bufsize)")
        print(f"  {C.CYAN}[4]{C.RESET} Maxrate (override manual)")
        print(f"  {C.CYAN}[5]{C.RESET} Bufsize (override manual)")
        print(f"  {C.CYAN}[6]{C.RESET} GOP / Keyframe Interval")
        print(f"  {C.CYAN}[7]{C.RESET} Audio Bitrate")
        print(f"  {C.CYAN}[8]{C.RESET} Target Framerate (FPS) - misal 60 fps atau 0 untuk asli")
        print(f"  {C.CYAN}[9]{C.RESET} Toggle Smart Sharpen (Anti-Blur TikTok)")
        print(f"  {C.CYAN}[10]{C.RESET} Toggle Audio Loudness Normalization (EBU R128)")
        print(f"  {C.CYAN}[11]{C.RESET} Toggle Bersihkan Metadata (Strip EXIF/Privacy)")
        print(f"  {C.CYAN}[0]{C.RESET} Selesai & Simpan")
        print()

        try:
            pick = input(f"{C.MAGENTA}{C.BOLD}➤ Ubah opsi: {C.RESET}").strip()
        except EOFError:
            print()
            return

        if pick == "1":
            avail = detect_available_encoders(ffmpeg_bin)
            print()
            print_box([f"{C.CYAN}{C.BOLD}PILIH ENCODER VIDEO{C.RESET}"], color=C.PURPLE)
            for i, enc in enumerate(avail, start=1):
                meta = KNOWN_ENCODERS.get(enc, {"name": enc})
                curr_mark = f" {C.GREEN}(aktif){C.RESET}" if s.get("encoder") == enc else ""
                print(f"  {C.CYAN}[{i}]{C.RESET} {meta['name']}{curr_mark}")
            try:
                ep = input(f"{C.MAGENTA}➤ Pilih encoder: {C.RESET}").strip()
                if ep.isdigit() and 1 <= int(ep) <= len(avail):
                    s["encoder"] = avail[int(ep) - 1]
                    config_mgr.save()
                    print_tag("OK", f"Encoder diubah ke {s['encoder']}.", C.GREEN)
            except EOFError:
                pass

        elif pick == "2":
            try:
                v = input(f"{C.MAGENTA}➤ Preset ({'/'.join(sorted(X264_PRESETS))}) [{s.get('preset')}]: {C.RESET}").strip().lower()
                if v in X264_PRESETS:
                    s["preset"] = v
                    config_mgr.save()
                    print_tag("OK", f"Preset diubah ke '{v}'.", C.GREEN)
            except EOFError:
                pass

        elif pick == "3":
            try:
                v = input(f"{C.MAGENTA}➤ Video bitrate (misal: 30M, 16M, 8M) [{s.get('bitrate')}]: {C.RESET}").strip()
                m = re.fullmatch(r"(\d+(?:\.\d+)?)([kKmM])", v)
                if m:
                    val, unit = float(m.group(1)), m.group(2).upper()
                    mbps = val if unit == "M" else val / 1000.0
                    s["bitrate"] = f"{mbps:.1f}M"
                    s["maxrate"] = f"{mbps * 1.1667:.1f}M"
                    s["bufsize"] = f"{mbps * 2.3333:.1f}M"
                    config_mgr.save()
                    print_tag("OK", f"Bitrate diubah ke {s['bitrate']} (maxrate & buffer disesuaikan).", C.GREEN)
            except EOFError:
                pass

        elif pick == "4":
            try:
                v = input(f"{C.MAGENTA}➤ Maxrate [{s.get('maxrate')}]: {C.RESET}").strip()
                parsed = parse_rate_value(v)
                if parsed:
                    s["maxrate"] = parsed
                    config_mgr.save()
                    print_tag("OK", f"Maxrate diubah ke {parsed}.", C.GREEN)
            except EOFError:
                pass

        elif pick == "5":
            try:
                v = input(f"{C.MAGENTA}➤ Bufsize [{s.get('bufsize')}]: {C.RESET}").strip()
                parsed = parse_rate_value(v)
                if parsed:
                    s["bufsize"] = parsed
                    config_mgr.save()
                    print_tag("OK", f"Bufsize diubah ke {parsed}.", C.GREEN)
            except EOFError:
                pass

        elif pick == "6":
            try:
                v = input(f"{C.MAGENTA}➤ GOP / keyframe [{s.get('gop')}]: {C.RESET}").strip()
                if v.isdigit() and int(v) > 0:
                    s["gop"] = int(v)
                    config_mgr.save()
                    print_tag("OK", f"GOP diubah ke {v}.", C.GREEN)
            except EOFError:
                pass

        elif pick == "7":
            try:
                v = input(f"{C.MAGENTA}➤ Audio bitrate [{s.get('audio_bitrate')}]: {C.RESET}").strip()
                if re.fullmatch(r"\d+[kK]", v):
                    s["audio_bitrate"] = v.lower()
                    config_mgr.save()
                    print_tag("OK", f"Audio bitrate diubah ke {v.lower()}.", C.GREEN)
            except EOFError:
                pass

        elif pick == "8":
            try:
                v = input(f"{C.MAGENTA}➤ Target FPS (misal: 60, 30, atau 0 untuk FPS asli) [{s.get('force_fps')}]: {C.RESET}").strip()
                if v.isdigit() and int(v) >= 0:
                    s["force_fps"] = int(v)
                    config_mgr.save()
                    lbl = f"{v} fps" if int(v) > 0 else "FPS asli"
                    print_tag("OK", f"Framerate diubah ke {lbl}.", C.GREEN)
            except EOFError:
                pass

        elif pick == "9":
            s["sharpen"] = not s.get("sharpen", False)
            if s["sharpen"]:
                try:
                    str_in = input(f"{C.MAGENTA}➤ Kekuatan unsharp (0.3 - 1.5) [{s.get('sharpen_strength', '0.7')}]: {C.RESET}").strip()
                    if str_in:
                        s["sharpen_strength"] = str_in
                except EOFError:
                    pass
            config_mgr.save()
            st = "AKTIF" if s["sharpen"] else "NONAKTIF"
            print_tag("OK", f"Smart Sharpening sekarang {st}.", C.GREEN)

        elif pick == "10":
            s["normalize_audio"] = not s.get("normalize_audio", False)
            config_mgr.save()
            st = "AKTIF" if s["normalize_audio"] else "NONAKTIF"
            print_tag("OK", f"Audio Loudnorm (EBU R128) sekarang {st}.", C.GREEN)

        elif pick == "11":
            s["strip_metadata"] = not s.get("strip_metadata", True)
            config_mgr.save()
            st = "AKTIF" if s["strip_metadata"] else "NONAKTIF"
            print_tag("OK", f"Pembersihan metadata sekarang {st}.", C.GREEN)

        elif pick == "0":
            config_mgr.save()
            print_tag("OK", "Semua pengaturan encode berhasil disimpan permanen.", C.GREEN)
            return


def settings_menu(ffmpeg_bin: str) -> None:
    while True:
        print()
        print_current_encode_settings()
        print(f"  {C.CYAN}[1]{C.RESET} Pilih Preset Cepat Bawaan")
        print(f"  {C.CYAN}[2]{C.RESET} Pengaturan Manual & Custom")
        print(f"  {C.CYAN}[3]{C.RESET} Reset Pengaturan Encode ke Default")
        print(f"  {C.CYAN}[0]{C.RESET} Kembali ke Menu Utama")
        print()

        try:
            choice = input(f"{C.MAGENTA}{C.BOLD}➤ Pilih: {C.RESET}").strip()
        except EOFError:
            print()
            return

        if choice == "1":
            print()
            for key in sorted(ENCODE_PRESETS.keys()):
                p = ENCODE_PRESETS[key]
                print(
                    f"  {C.CYAN}[{key}]{C.RESET} {p['name']} "
                    f"{C.GRAY}- video {p['bitrate']}, audio {p['audio_bitrate']}, preset {p['preset']}{C.RESET}"
                )
            print()
            try:
                pick = input(f"{C.MAGENTA}➤ Pilih preset (0 batal): {C.RESET}").strip()
            except EOFError:
                continue

            if pick in ENCODE_PRESETS:
                p = ENCODE_PRESETS[pick]
                for k, v in p.items():
                    if k != "name":
                        config_mgr.encode_settings[k] = v
                config_mgr.save()
                print_tag("OK", f"Setting diubah ke preset '{p['name']}' & disimpan permanen.", C.GREEN)
            elif pick != "0":
                print_tag("WARN", "Pilihan tidak valid.", C.YELLOW)

        elif choice == "2":
            edit_encode_custom(ffmpeg_bin)

        elif choice == "3":
            config_mgr.data["encode_settings"] = dict(DEFAULT_ENCODE_SETTINGS)
            config_mgr.save()
            print_tag("OK", "Pengaturan encode dikembalikan ke nilai default.", C.GREEN)

        elif choice == "0":
            return


# =============================================================================
# MENU SETTING STABILISASI VIDEO
# =============================================================================


def print_current_stab_settings() -> None:
    s = config_mgr.stab_settings
    print_box(
        [
            f"{C.CYAN}{C.BOLD}SETTING STABILISASI VIDEO{C.RESET}",
            f"{C.GRAY}Kekuatan Smoothing   : {s.get('smoothing')}{C.RESET}",
            f"{C.GRAY}Grid Mesh (Kualitas) : {s.get('grid')}x{s.get('grid')}{C.RESET}",
            f"{C.GRAY}Resolusi Analisis    : {s.get('analysis_max_dim')}px{C.RESET}",
            f"{C.GRAY}Batas Crop Maksimum  : {round(float(s.get('max_crop_ratio', 0.15)) * 100)}%{C.RESET}",
            f"{C.GRAY}Encode Hasil         : {s.get('preset')} / {s.get('bitrate')} video / {s.get('audio_bitrate')} audio{C.RESET}",
        ],
        color=C.PURPLE,
    )


def edit_stab_encode() -> None:
    s = config_mgr.stab_settings
    while True:
        print()
        print_box(
            [
                f"{C.CYAN}{C.BOLD}ENCODE HASIL STABILISASI{C.RESET}",
                f"{C.GRAY}preset  : {s.get('preset')}{C.RESET}",
                f"{C.GRAY}video   : {s.get('bitrate')} (maxrate {s.get('maxrate')}, buffer {s.get('bufsize')}){C.RESET}",
                f"{C.GRAY}audio   : {s.get('audio_bitrate')}{C.RESET}",
            ],
            color=C.PURPLE,
        )
        print(f"  {C.CYAN}[1]{C.RESET} Preset Kecepatan x264")
        print(f"  {C.CYAN}[2]{C.RESET} Video Bitrate")
        print(f"  {C.CYAN}[3]{C.RESET} Audio Bitrate")
        print(f"  {C.CYAN}[0]{C.RESET} Selesai")
        print()

        try:
            pick = input(f"{C.MAGENTA}{C.BOLD}➤ Ubah opsi: {C.RESET}").strip()
        except EOFError:
            return

        if pick == "1":
            try:
                v = input(f"{C.MAGENTA}➤ Preset [{s.get('preset')}]: {C.RESET}").strip().lower()
                if v in X264_PRESETS:
                    s["preset"] = v
                    config_mgr.save()
                    print_tag("OK", f"Preset stabilisasi diubah ke '{v}'.", C.GREEN)
            except EOFError:
                pass
        elif pick == "2":
            try:
                v = input(f"{C.MAGENTA}➤ Video bitrate [{s.get('bitrate')}]: {C.RESET}").strip()
                m = re.fullmatch(r"(\d+(?:\.\d+)?)([kKmM])", v)
                if m:
                    val, unit = float(m.group(1)), m.group(2).upper()
                    mbps = val if unit == "M" else val / 1000.0
                    s["bitrate"] = f"{mbps:.1f}M"
                    s["maxrate"] = f"{mbps * 1.25:.1f}M"
                    s["bufsize"] = f"{mbps * 2.5:.1f}M"
                    config_mgr.save()
                    print_tag("OK", f"Bitrate stabilisasi diubah ke {s['bitrate']}.", C.GREEN)
            except EOFError:
                pass
        elif pick == "3":
            try:
                v = input(f"{C.MAGENTA}➤ Audio bitrate [{s.get('audio_bitrate')}]: {C.RESET}").strip()
                if re.fullmatch(r"\d+[kK]", v):
                    s["audio_bitrate"] = v.lower()
                    config_mgr.save()
                    print_tag("OK", f"Audio bitrate stabilisasi diubah ke {v}.", C.GREEN)
            except EOFError:
                pass
        elif pick == "0":
            config_mgr.save()
            return


def stab_settings_menu() -> None:
    s = config_mgr.stab_settings
    while True:
        print()
        print_current_stab_settings()
        print(f"  {C.CYAN}[1]{C.RESET} Kekuatan Smoothing (Ringan/Sedang/Kuat/Maksimal)")
        print(f"  {C.CYAN}[2]{C.RESET} Ukuran Grid Mesh (Mode Kualitas)")
        print(f"  {C.CYAN}[3]{C.RESET} Resolusi Analisis Optical Flow (px)")
        print(f"  {C.CYAN}[4]{C.RESET} Batas Crop Maksimum (%)")
        print(f"  {C.CYAN}[5]{C.RESET} Setting Encode Khusus Stabilisasi")
        print(f"  {C.CYAN}[6]{C.RESET} Reset Setting Stabilisasi ke Default")
        print(f"  {C.CYAN}[0]{C.RESET} Kembali ke Menu Utama")
        print()

        try:
            choice = input(f"{C.MAGENTA}{C.BOLD}➤ Pilih: {C.RESET}").strip()
        except EOFError:
            return

        if choice == "1":
            print()
            for idx, key in enumerate(STAB_SMOOTHING_ORDER, start=1):
                p = STAB_SMOOTHING_PRESETS[key]
                print(
                    f"  {C.CYAN}[{idx}]{C.RESET} {key.capitalize()} "
                    f"{C.GRAY}(radius ~{p['radius_sec']}s, lambda {p['lambda_ratio']}){C.RESET}"
                )
            try:
                pick = input(f"{C.MAGENTA}➤ Pilih (0 batal): {C.RESET}").strip()
                if pick.isdigit() and 1 <= int(pick) <= len(STAB_SMOOTHING_ORDER):
                    s["smoothing"] = STAB_SMOOTHING_ORDER[int(pick) - 1]
                    config_mgr.save()
                    print_tag("OK", f"Smoothing diubah ke '{s['smoothing']}'.", C.GREEN)
            except EOFError:
                pass

        elif choice == "2":
            try:
                v = input(f"{C.MAGENTA}➤ Ukuran grid NxN (4-40) [{s.get('grid')}]: {C.RESET}").strip()
                if v.isdigit() and 4 <= int(v) <= 40:
                    s["grid"] = int(v)
                    config_mgr.save()
                    print_tag("OK", f"Grid diubah ke {v}x{v}.", C.GREEN)
            except EOFError:
                pass

        elif choice == "3":
            try:
                v = input(f"{C.MAGENTA}➤ Resolusi analisis px (160-1080) [{s.get('analysis_max_dim')}]: {C.RESET}").strip()
                if v.isdigit() and 160 <= int(v) <= 1080:
                    s["analysis_max_dim"] = int(v)
                    config_mgr.save()
                    print_tag("OK", f"Resolusi analisis diubah ke {v}px.", C.GREEN)
            except EOFError:
                pass

        elif choice == "4":
            try:
                v = input(f"{C.MAGENTA}➤ Batas crop maksimum % (1-40) [{round(float(s.get('max_crop_ratio', 0.15)) * 100)}]: {C.RESET}").strip()
                if v.isdigit() and 1 <= int(v) <= 40:
                    s["max_crop_ratio"] = int(v) / 100.0
                    config_mgr.save()
                    print_tag("OK", f"Batas crop diubah ke {v}%.", C.GREEN)
            except EOFError:
                pass

        elif choice == "5":
            edit_stab_encode()

        elif choice == "6":
            config_mgr.data["stab_settings"] = dict(DEFAULT_STAB_SETTINGS)
            config_mgr.save()
            print_tag("OK", "Pengaturan stabilisasi dikembalikan ke nilai default.", C.GREEN)

        elif choice == "0":
            return


# =============================================================================
# MENU PENGELOLAAN KONFIGURASI & PRESET KUSTOM
# =============================================================================


def config_management_menu() -> None:
    """Menu khusus untuk mengelola file config, folder pusat kustom, dan preset buatan pengguna."""
    while True:
        cfg_path = config_mgr.config_path
        presets = config_mgr.custom_presets
        central_override = config_mgr.data.get("central_dir_override", "")

        print()
        print_box(
            [
                f"{C.CYAN}{C.BOLD}PENGELOLAAN KONFIGURASI & PRESET KUSTOM{C.RESET}",
                f"{C.GRAY}File Config : {cfg_path}{C.RESET}",
                f"{C.GRAY}Folder Pusat: {config_mgr.get_central_dir()}{C.RESET}",
                f"{C.GRAY}Preset Kustom Tersimpan: {len(presets)}{C.RESET}",
            ],
            color=C.PURPLE,
        )
        print(f"  {C.CYAN}[1]{C.RESET} Simpan Pengaturan Saat Ini Sebagai Preset Baru")
        print(f"  {C.CYAN}[2]{C.RESET} Muat & Terapkan Preset Kustom")
        print(f"  {C.CYAN}[3]{C.RESET} Hapus Preset Kustom")
        print(f"  {C.CYAN}[4]{C.RESET} Ubah Folder Pusat Penyimpanan Video")
        print(f"  {C.CYAN}[5]{C.RESET} Muat Ulang (Reload) Konfigurasi dari File")
        print(f"  {C.CYAN}[6]{C.RESET} Reset TOTAL Semua Konfigurasi ke Pengaturan Awal")
        print(f"  {C.CYAN}[0]{C.RESET} Kembali ke Menu Utama")
        print()

        try:
            choice = input(f"{C.MAGENTA}{C.BOLD}➤ Pilih: {C.RESET}").strip()
        except EOFError:
            return

        if choice == "1":
            try:
                name = input(f"{C.MAGENTA}➤ Masukkan nama preset baru (mis. 'Anime_Sharp_60fps'): {C.RESET}").strip()
                if name:
                    config_mgr.add_custom_preset(name, config_mgr.encode_settings)
                    print_tag("OK", f"Preset '{name}' berhasil disimpan ke {CONFIG_FILE_NAME}.", C.GREEN)
                else:
                    print_tag("WARN", "Nama preset tidak boleh kosong.", C.YELLOW)
            except EOFError:
                pass

        elif choice == "2":
            if not presets:
                print_tag("WARN", "Belum ada preset kustom yang disimpan.", C.YELLOW)
                continue
            print()
            p_keys = list(presets.keys())
            for idx, k in enumerate(p_keys, start=1):
                p_data = presets[k]
                print(f"  {C.CYAN}[{idx}]{C.RESET} {k} {C.GRAY}(bitrate: {p_data.get('bitrate')}, fps: {p_data.get('force_fps')}){C.RESET}")
            try:
                sel = input(f"{C.MAGENTA}➤ Pilih nomor preset untuk diterapkan (0 batal): {C.RESET}").strip()
                if sel.isdigit() and 1 <= int(sel) <= len(p_keys):
                    selected_name = p_keys[int(sel) - 1]
                    config_mgr.data["encode_settings"] = dict(presets[selected_name])
                    config_mgr.save()
                    print_tag("OK", f"Preset '{selected_name}' aktif dan diterapkan.", C.GREEN)
            except EOFError:
                pass

        elif choice == "3":
            if not presets:
                print_tag("WARN", "Belum ada preset kustom untuk dihapus.", C.YELLOW)
                continue
            p_keys = list(presets.keys())
            for idx, k in enumerate(p_keys, start=1):
                print(f"  {C.CYAN}[{idx}]{C.RESET} {k}")
            try:
                sel = input(f"{C.MAGENTA}➤ Pilih nomor preset yang akan dihapus: {C.RESET}").strip()
                if sel.isdigit() and 1 <= int(sel) <= len(p_keys):
                    target_name = p_keys[int(sel) - 1]
                    config_mgr.delete_custom_preset(target_name)
                    print_tag("OK", f"Preset '{target_name}' telah dihapus.", C.GREEN)
            except EOFError:
                pass

        elif choice == "4":
            print(f"  {C.GRAY}Folder pusat saat ini: {config_mgr.get_central_dir()}{C.RESET}")
            try:
                new_p = input(f"{C.MAGENTA}➤ Masukkan path folder baru (kosongkan untuk reset ke default): {C.RESET}").strip()
                config_mgr.set_central_dir(new_p)
                print_tag("OK", f"Folder pusat diatur ke: {config_mgr.get_central_dir()}", C.GREEN)
            except EOFError:
                pass

        elif choice == "5":
            config_mgr.load()
            print_tag("OK", f"Konfigurasi dimuat ulang dari {cfg_path}.", C.GREEN)

        elif choice == "6":
            try:
                confirm = input(f"{C.RED}{C.BOLD}➤ Yakin ingin mereset semua setting ke default awal? (y/n): {C.RESET}").strip().lower()
                if confirm == "y":
                    config_mgr.reset_to_defaults()
                    print_tag("OK", "Konfigurasi telah direset ke nilai awal pabrik.", C.GREEN)
            except EOFError:
                pass

        elif choice == "0":
            return


# =============================================================================
# MENU VIDEO INSPECTOR (ANALISIS SPESIFIKASI VIDEO)
# =============================================================================


def video_inspector_menu(ffmpeg_bin: str) -> None:
    central_dir = get_central_dir()
    vids = pick_videos_interactive(central_dir)
    if not vids:
        return

    for target in vids:
        print()
        info = inspect_video_detailed(ffmpeg_bin, target)
        print_box(
            [
                f"{C.CYAN}{C.BOLD}DETAIL SPESIFIKASI VIDEO{C.RESET}",
                f"{C.WHITE}{info['name']}{C.RESET}",
            ],
            color=C.CYAN,
        )
        print(f"  {C.GRAY}Ukuran File     :{C.RESET} {human_size(info['size'])}")
        dur_str = f"{info['duration']:.2f} detik" if info["duration"] else "N/A"
        print(f"  {C.GRAY}Durasi          :{C.RESET} {dur_str}")
        print(f"  {C.GRAY}Total Bitrate   :{C.RESET} {human_bitrate(info['bitrate'])}")
        print(f"  {C.GRAY}{'─' * 50}{C.RESET}")
        res_str = f"{info['width']}x{info['height']} ({info['aspect']})" if info["width"] else "N/A"
        print(f"  {C.CYAN}Resolusi & Rasio:{C.RESET} {res_str}")
        fps_str = f"{info['fps']} fps" if info["fps"] else "N/A"
        print(f"  {C.CYAN}Framerate (FPS) :{C.RESET} {fps_str}")
        print(f"  {C.CYAN}Video Codec     :{C.RESET} {info['v_codec'] or 'N/A'}")
        print(f"  {C.CYAN}Format Pixel    :{C.RESET} {info['pix_fmt'] or 'N/A'}")
        print(f"  {C.GRAY}{'─' * 50}{C.RESET}")
        print(f"  {C.MAGENTA}Audio Codec     :{C.RESET} {info['a_codec'] or 'N/A'}")
        print(f"  {C.MAGENTA}Sample Rate     :{C.RESET} {info['a_rate'] or 'N/A'}")
        print(f"  {C.MAGENTA}Channel Audio   :{C.RESET} {info['a_channels'] or 'N/A'}")
        print()


# =============================================================================
# BATCH EXECUTION RUNNER
# =============================================================================


def run_batch_patch(ffmpeg_bin: str, targets: List[Path]) -> None:
    total = len(targets)
    success = 0
    start_all = time.time()

    print()
    print_box([f"{C.CYAN}{C.BOLD}MEMULAI PROSES BATCH ({total} VIDEO){C.RESET}"], color=C.PURPLE)

    for idx, vid_path in enumerate(targets, start=1):
        print(f"\n{C.YELLOW}{C.BOLD}▶ [{idx}/{total}] Memproses: {vid_path.name}{C.RESET}")
        try:
            process_video(ffmpeg_bin, vid_path)
            success += 1
        except subprocess.CalledProcessError as e:
            handle_ffmpeg_error(e)
        except KeyboardInterrupt:
            print_tag("WARN", "Batch dihentikan oleh pengguna.", C.YELLOW)
            break
        except Exception as e:
            print_tag("ERR", f"Gagal memproses {vid_path.name}: {e}", C.RED)

    total_time = time.time() - start_all
    print()
    print_box(
        [
            f"{C.GREEN}{C.BOLD}BATCH SELESAI{C.RESET}",
            f"{C.WHITE}Berhasil: {success} dari {total} video{C.RESET}",
            f"{C.GRAY}Total Waktu: {total_time:.1f} detik{C.RESET}",
        ],
        color=C.GREEN,
    )


def run_batch_crop(ffmpeg_bin: str, targets: List[Path], ratio_key: str) -> None:
    total = len(targets)
    success = 0
    start_all = time.time()

    print()
    print_box([f"{C.CYAN}{C.BOLD}MEMULAI BATCH CROP ({total} VIDEO) -> {ratio_key}{C.RESET}"], color=C.PURPLE)

    for idx, vid_path in enumerate(targets, start=1):
        print(f"\n{C.YELLOW}{C.BOLD}▶ [{idx}/{total}] Memproses Crop: {vid_path.name}{C.RESET}")
        try:
            crop_video(ffmpeg_bin, vid_path, ratio_key)
            success += 1
        except subprocess.CalledProcessError as e:
            handle_ffmpeg_error(e)
        except KeyboardInterrupt:
            print_tag("WARN", "Batch crop dihentikan oleh pengguna.", C.YELLOW)
            break
        except Exception as e:
            print_tag("ERR", f"Gagal crop {vid_path.name}: {e}", C.RED)

    total_time = time.time() - start_all
    print()
    print_box(
        [
            f"{C.GREEN}{C.BOLD}BATCH CROP SELESAI{C.RESET}",
            f"{C.WHITE}Berhasil: {success} dari {total} video{C.RESET}",
            f"{C.GRAY}Total Waktu: {total_time:.1f} detik{C.RESET}",
        ],
        color=C.GREEN,
    )


# =============================================================================
# MENU INTERAKTIF UTAMA
# =============================================================================


def interactive_menu(ffmpeg_bin: str) -> None:
    central_dir = get_central_dir()

    while True:
        central_dir = get_central_dir()
        s = config_mgr.encode_settings
        fps_info = f"{s.get('force_fps')}fps" if s.get("force_fps") else "Asli"

        print()
        print_box(
            [
                f"{C.CYAN}{C.BOLD}MENU UTAMA - TIKTOK PATCHER PRO{C.RESET}",
                f"{C.GRAY}ffmpeg : {ffmpeg_bin}{C.RESET}",
                f"{C.GRAY}env    : {get_environment_label()} | config: {CONFIG_FILE_NAME}{C.RESET}",
                f"{C.GRAY}folder : {central_dir}{C.RESET}",
                f"{C.GRAY}mode   : {s.get('encoder')} | {s.get('bitrate')} | {fps_info} | "
                f"Sharp: {'ON' if s.get('sharpen') else 'OFF'} | "
                f"Loudnorm: {'ON' if s.get('normalize_audio') else 'OFF'}{C.RESET}",
            ],
            color=C.PURPLE,
        )
        print(f"  {C.CYAN}[1]{C.RESET} Patch Video (Anti-Kompresi TikTok - Single / Batch)")
        print(f"  {C.CYAN}[2]{C.RESET} Re-encode + Crop ke Rasio Tertentu & 1080p")
        print(f"  {C.CYAN}[3]{C.RESET} Stabilisasi Video (Anti-Goyang Canggih)")
        print(f"  {C.CYAN}[4]{C.RESET} Pengaturan Encode (Bitrate, FPS, Filter Smart Sharpen, dll)")
        print(f"  {C.CYAN}[5]{C.RESET} Pengaturan Stabilisasi Video")
        print(f"  {C.CYAN}[6]{C.RESET} Analisis / Inspector Detail Video")
        print(f"  {C.CYAN}[7]{C.RESET} Manajemen Konfigurasi & Preset Kustom (Simpan/Muat)")
        print(f"  {C.CYAN}[8]{C.RESET} Cek Status ffmpeg, Hardware Encoder & Dependensi")
        print(f"  {C.CYAN}[0]{C.RESET} Keluar")
        print()

        try:
            choice = input(f"{C.MAGENTA}{C.BOLD}➤ Pilih menu: {C.RESET}").strip()
        except EOFError:
            print()
            break

        if choice == "1":
            targets = pick_videos_interactive(central_dir)
            if not targets:
                continue
            if len(targets) == 1:
                try:
                    process_video(ffmpeg_bin, targets[0])
                except subprocess.CalledProcessError as e:
                    handle_ffmpeg_error(e)
                except KeyboardInterrupt:
                    print_tag("WARN", "Proses dibatalkan oleh pengguna.", C.YELLOW)
                except Exception as e:
                    print_tag("ERR", f"Terjadi kesalahan: {e}", C.RED)
            else:
                run_batch_patch(ffmpeg_bin, targets)

        elif choice == "2":
            targets = pick_videos_interactive(central_dir)
            if not targets:
                continue

            print()
            print_box([f"{C.CYAN}{C.BOLD}PILIH RASIO TARGET{C.RESET}"], color=C.PURPLE)
            for idx, key in enumerate(CROP_RATIO_ORDER, start=1):
                print(f"  {C.CYAN}[{idx}]{C.RESET} {CROP_RATIOS[key]['label']}")
            print(f"  {C.CYAN}[0]{C.RESET} Batal")
            print()

            try:
                ratio_raw = input(f"{C.MAGENTA}{C.BOLD}➤ Pilih rasio: {C.RESET}").strip()
            except EOFError:
                continue

            if ratio_raw in ("", "0"):
                continue
            if not ratio_raw.isdigit() or not (1 <= int(ratio_raw) <= len(CROP_RATIO_ORDER)):
                print_tag("WARN", "Pilihan rasio tidak valid.", C.YELLOW)
                continue

            ratio_key = CROP_RATIO_ORDER[int(ratio_raw) - 1]
            if len(targets) == 1:
                try:
                    crop_video(ffmpeg_bin, targets[0], ratio_key)
                except subprocess.CalledProcessError as e:
                    handle_ffmpeg_error(e)
                except KeyboardInterrupt:
                    print_tag("WARN", "Proses dibatalkan oleh pengguna.", C.YELLOW)
                except Exception as e:
                    print_tag("ERR", f"Terjadi kesalahan: {e}", C.RED)
            else:
                run_batch_crop(ffmpeg_bin, targets, ratio_key)

        elif choice == "3":
            ok_deps, missing = check_stabilization_deps()
            if not ok_deps:
                print_tag(
                    "ERR",
                    "Fitur ini butuh modul Python tambahan: " + ", ".join(missing),
                    C.RED,
                )
                print(f"  {C.GRAY}Pasang dengan:{C.RESET}")
                print(f"  {C.GRAY}pip install opencv-python numpy scipy --break-system-packages{C.RESET}")
                continue

            targets = pick_videos_interactive(central_dir)
            if not targets:
                continue

            try:
                mode_raw = input(
                    f"{C.MAGENTA}{C.BOLD}➤ Pilih mode stabilisasi [1] Kualitas / [2] Cepat: {C.RESET}"
                ).strip()
            except EOFError:
                continue

            mode = {"1": "kualitas", "2": "cepat"}.get(mode_raw)
            if mode is None:
                print_tag("WARN", "Pilihan mode tidak valid.", C.YELLOW)
                continue

            for t_vid in targets:
                try:
                    stabilize_video(ffmpeg_bin, t_vid, mode)
                except subprocess.CalledProcessError as e:
                    handle_ffmpeg_error(e)
                except KeyboardInterrupt:
                    print_tag("WARN", "Proses dibatalkan pengguna.", C.YELLOW)
                    break
                except Exception as e:
                    print_tag("ERR", f"Gagal stabilisasi {t_vid.name}: {e}", C.RED)

        elif choice == "4":
            settings_menu(ffmpeg_bin)

        elif choice == "5":
            stab_settings_menu()

        elif choice == "6":
            video_inspector_menu(ffmpeg_bin)

        elif choice == "7":
            config_management_menu()

        elif choice == "8":
            print_tag("OK", f"ffmpeg terdeteksi di: {ffmpeg_bin}", C.GREEN)
            ver = get_ffmpeg_version(ffmpeg_bin)
            if ver:
                print(f"  {C.GRAY}{ver}{C.RESET}")

            encoders = detect_available_encoders(ffmpeg_bin)
            print_tag("INFO", "Encoder yang tersedia: " + ", ".join(encoders), C.CYAN)

            ok_deps, missing = check_stabilization_deps()
            if ok_deps:
                print_tag("OK", "Modul stabilisasi (opencv-python, numpy, scipy) siap.", C.GREEN)
            else:
                print_tag("WARN", "Modul stabilisasi belum lengkap: " + ", ".join(missing), C.YELLOW)

            print_tag("CONF", f"File Konfigurasi Aktif: {config_mgr.config_path}", C.PURPLE)

        elif choice == "0":
            print_tag("INFO", "Sampai jumpa!", C.CYAN)
            break

        else:
            print_tag("WARN", "Pilihan tidak valid.", C.YELLOW)


# =============================================================================
# ENTRY POINT
# =============================================================================


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="tiktok_patcher.py",
        description=(
            "TikTok Compression Patcher Pro - Anti-Compression HD Re-encoder v2.1.0. "
            "Mengoptimalkan video agar lolos dari kompresi agresif TikTok. "
            "Mendukung konfigurasi terpisah, batch multi-video, dan smart sharpening."
        ),
    )
    parser.add_argument("-i", "--input", type=str, help="Path file video input atau folder")
    parser.add_argument(
        "-m", "--mode", type=str, choices=["patch"], default="patch",
        help="Mode pemrosesan",
    )
    parser.add_argument(
        "-o", "--output", type=str, default=None,
        help="Nama file output kustom",
    )
    parser.add_argument(
        "-s", "--stabilize", type=str, choices=["kualitas", "cepat"], default=None,
        help="Jalankan stabilisasi video (anti-goyang)",
    )
    parser.add_argument(
        "-c", "--crop", type=str, choices=list(CROP_RATIOS.keys()), default=None,
        help="Re-encode + crop ke rasio target (1080p)",
    )
    parser.add_argument(
        "--inspect", action="store_true", help="Analisis detail spesifikasi video"
    )
    parser.add_argument(
        "--batch-all", action="store_true", help="Proses seluruh video di folder pusat"
    )
    parser.add_argument(
        "--sharpen", action="store_true", help="Paksa aktifkan filter smart sharpening"
    )
    parser.add_argument(
        "--normalize-audio", action="store_true", help="Paksa aktifkan audio loudnorm"
    )
    parser.add_argument(
        "--fps", type=int, default=None, help="Paksa target FPS (misal: 60)"
    )
    parser.add_argument(
        "--reset-config", action="store_true", help="Reset konfigurasi ke default awal"
    )
    args = parser.parse_args()

    if args.reset_config:
        config_mgr.reset_to_defaults()
        print_tag("OK", "Konfigurasi telah direset ke default awal.", C.GREEN)
        sys.exit(0)

    # Terapkan CLI flags ke config jika ada
    if args.sharpen:
        config_mgr.encode_settings["sharpen"] = True
    if args.normalize_audio:
        config_mgr.encode_settings["normalize_audio"] = True
    if args.fps is not None:
        config_mgr.encode_settings["force_fps"] = args.fps

    ffmpeg_bin = check_ffmpeg()
    if not ffmpeg_bin:
        print_tag("ERR", "Binary 'ffmpeg' tidak ditemukan di PATH sistem.", C.RED)
        print(f"  {C.GRAY}Pasang terlebih dahulu:{C.RESET}")
        print(f"  {C.GRAY}. Termux           : pkg install ffmpeg{C.RESET}")
        print(f"  {C.GRAY}. Debian/Ubuntu    : sudo apt install ffmpeg{C.RESET}")
        print(f"  {C.GRAY}. macOS (Homebrew) : brew install ffmpeg{C.RESET}")
        sys.exit(1)

    central_dir = get_central_dir()

    if args.inspect and args.input:
        inspect_path = Path(args.input).expanduser()
        video_inspector_menu(ffmpeg_bin)
        sys.exit(0)

    if args.batch_all:
        vids = list_video_files(central_dir)
        if not vids:
            print_tag("WARN", f"Tidak ada video di {central_dir}", C.YELLOW)
            sys.exit(0)
        run_batch_patch(ffmpeg_bin, vids)
        sys.exit(0)

    if args.input:
        input_path = Path(args.input).expanduser()
        ok, err_msg = validate_input_file(input_path)
        if not ok:
            print_tag("ERR", err_msg, C.RED)
            sys.exit(1)
        try:
            if args.crop:
                crop_video(ffmpeg_bin, input_path, args.crop)
            elif args.stabilize:
                ok_deps, missing = check_stabilization_deps()
                if not ok_deps:
                    print_tag("ERR", "Modul belum terpasang: " + ", ".join(missing), C.RED)
                    sys.exit(1)
                stabilize_video(ffmpeg_bin, input_path, args.stabilize)
            else:
                process_video(ffmpeg_bin, input_path, args.output)
        except subprocess.CalledProcessError as e:
            handle_ffmpeg_error(e)
            sys.exit(1)
        except KeyboardInterrupt:
            print()
            print_tag("WARN", "Proses dibatalkan oleh pengguna.", C.YELLOW)
            sys.exit(130)
        except Exception as e:
            print_tag("ERR", f"Terjadi kesalahan: {e}", C.RED)
            sys.exit(1)
    else:
        print()
        print_banner()
        print_tag("INFO", f"Folder pusat : {central_dir}", C.CYAN)
        interactive_menu(ffmpeg_bin)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
        print_tag("WARN", "Dibatalkan oleh pengguna.", C.YELLOW)
        sys.exit(130)
