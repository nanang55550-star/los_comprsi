#!/usr/bin/env python3
"""
config.py
=========
Modul Manajemen Konfigurasi Terpisah untuk TikTok Compression Patcher.

Menyediakan:
- Penyimpanan persisten otomatis ke file JSON (config.json)
- Validasi data & migrasi konfigurasi otomatis
- Pengelolaan preset bawaan & preset kustom pengguna
- Penyesuaian folder pusat (central directory)
"""

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

# Nama file konfigurasi default
CONFIG_FILE_NAME = "config.json"

# Versi skema konfigurasi saat ini
CONFIG_SCHEMA_VERSION = "2.1.0"

# =============================================================================
# DEFAULT CONFIGURATIONS
# =============================================================================

DEFAULT_ENCODE_SETTINGS: Dict[str, Any] = {
    "encoder": "libx264",       # libx264, h264_nvenc, h264_qsv, h264_videotoolbox, dll.
    "preset": "slow",
    "profile": "high",
    "level": "4.2",
    "bitrate": "30M",
    "maxrate": "35M",
    "bufsize": "70M",
    "gop": 60,
    "audio_bitrate": "256k",
    "force_fps": 60,            # 0 = pertahankan fps asli, 60 = paksa 60fps
    "sharpen": False,           # Smart sharpening unsharp mask (anti-blur TikTok)
    "sharpen_strength": "0.7",  # Kekuatan unsharp filter
    "normalize_audio": False,   # EBU R128 loudnorm untuk audio TikTok konsisten
    "strip_metadata": True,     # Bersihkan EXIF/metadata untuk privasi & upload bersih
}

DEFAULT_STAB_SETTINGS: Dict[str, Any] = {
    "grid": 16,                 # Ukuran mesh grid NxN untuk mode Kualitas
    "analysis_max_dim": 480,    # Resolusi analisis optical flow (px)
    "smoothing": "sedang",      # ringan / sedang / kuat / maksimal
    "max_crop_ratio": 0.15,     # Batas atas crop tepi (15%)
    "preset": "fast",
    "bitrate": "16M",
    "maxrate": "20M",
    "bufsize": "40M",
    "audio_bitrate": "192k",
}

ENCODE_PRESETS: Dict[str, Dict[str, Any]] = {
    "1": {
        "name": "Hemat",
        "preset": "fast",
        "profile": "high",
        "level": "4.2",
        "bitrate": "8M",
        "maxrate": "10M",
        "bufsize": "20M",
        "gop": 60,
        "audio_bitrate": "128k",
        "force_fps": 0,
        "sharpen": False,
        "normalize_audio": False,
    },
    "2": {
        "name": "Standar",
        "preset": "medium",
        "profile": "high",
        "level": "4.2",
        "bitrate": "16M",
        "maxrate": "20M",
        "bufsize": "40M",
        "gop": 60,
        "audio_bitrate": "192k",
        "force_fps": 60,
        "sharpen": False,
        "normalize_audio": False,
    },
    "3": {
        "name": "Tinggi (Default Anti-Kompresi TikTok)",
        "preset": "slow",
        "profile": "high",
        "level": "4.2",
        "bitrate": "30M",
        "maxrate": "35M",
        "bufsize": "70M",
        "gop": 60,
        "audio_bitrate": "256k",
        "force_fps": 60,
        "sharpen": False,
        "normalize_audio": False,
    },
    "4": {
        "name": "Maksimal (Ultra Quality)",
        "preset": "veryslow",
        "profile": "high",
        "level": "4.2",
        "bitrate": "50M",
        "maxrate": "60M",
        "bufsize": "120M",
        "gop": 60,
        "audio_bitrate": "320k",
        "force_fps": 60,
        "sharpen": True,
        "normalize_audio": True,
    },
    "5": {
        "name": "AMV / Alight Motion Sharp (Anti-Blur)",
        "preset": "slow",
        "profile": "high",
        "level": "4.2",
        "bitrate": "32M",
        "maxrate": "38M",
        "bufsize": "75M",
        "gop": 60,
        "audio_bitrate": "256k",
        "force_fps": 60,
        "sharpen": True,
        "normalize_audio": True,
    },
}

DEFAULT_CONFIG: Dict[str, Any] = {
    "version": CONFIG_SCHEMA_VERSION,
    "central_dir_override": "",
    "encode_settings": DEFAULT_ENCODE_SETTINGS,
    "stab_settings": DEFAULT_STAB_SETTINGS,
    "custom_presets": {},
}


# =============================================================================
# HELPER PATHS
# =============================================================================

def get_default_config_path() -> Path:
    """Mendapatkan path file config.json.
    Prioritas:
    1. Folder script saat ini (jika writable)
    2. ~/.config/tiktok_patcher/config.json
    """
    script_dir = Path(__file__).resolve().parent
    local_cfg = script_dir / CONFIG_FILE_NAME
    if os.access(script_dir, os.W_OK) or local_cfg.is_file():
        return local_cfg

    home_cfg_dir = Path.home() / ".config" / "tiktok_patcher"
    try:
        home_cfg_dir.mkdir(parents=True, exist_ok=True)
        return home_cfg_dir / CONFIG_FILE_NAME
    except Exception:
        return local_cfg


# =============================================================================
# CONFIG MANAGER CLASS
# =============================================================================

class ConfigManager:
    """Kelas pengelola konfigurasi terpisah dengan persistensi JSON otomatis."""

    def __init__(self, config_file: Optional[Path] = None):
        self.config_path = config_file or get_default_config_path()
        self.data: Dict[str, Any] = {}
        self.load()

    def _deep_merge(self, base: dict, override: dict) -> dict:
        """Merge rekursif memastikan semua kunci default tetap ada."""
        res = dict(base)
        for k, v in override.items():
            if k in res and isinstance(res[k], dict) and isinstance(v, dict):
                res[k] = self._deep_merge(res[k], v)
            else:
                res[k] = v
        return res

    def load(self) -> None:
        """Memuat konfigurasi dari file JSON. Jika file belum ada, buat baru dari default."""
        if self.config_path.is_file():
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    # Merge dengan default agar jika ada field baru di versi ini, tetap terisi
                    self.data = self._deep_merge(DEFAULT_CONFIG, loaded)
                    return
            except Exception as e:
                # Jika file rusak, cadangkan file lama lalu gunakan default
                backup_path = self.config_path.with_suffix(".json.bak")
                try:
                    self.config_path.rename(backup_path)
                except Exception:
                    pass
                print(f"[WARN] File konfigurasi rusak ({e}), dicadangkan ke {backup_path.name}.")

        self.data = json.loads(json.dumps(DEFAULT_CONFIG))
        self.save()

    def save(self) -> bool:
        """Menyimpan data konfigurasi saat ini ke file JSON."""
        try:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=4, ensure_ascii=False)
            return True
        except Exception as e:
            print(f"[ERR] Gagal menyimpan konfigurasi ke {self.config_path}: {e}")
            return False

    def reset_to_defaults(self) -> bool:
        """Mereset konfigurasi ke nilai default bawaan."""
        custom_saved = self.data.get("custom_presets", {})
        self.data = json.loads(json.dumps(DEFAULT_CONFIG))
        self.data["custom_presets"] = custom_saved  # preset kustom tetap disimpan
        return self.save()

    @property
    def encode_settings(self) -> Dict[str, Any]:
        return self.data.setdefault("encode_settings", dict(DEFAULT_ENCODE_SETTINGS))

    @property
    def stab_settings(self) -> Dict[str, Any]:
        return self.data.setdefault("stab_settings", dict(DEFAULT_STAB_SETTINGS))

    @property
    def custom_presets(self) -> Dict[str, Dict[str, Any]]:
        return self.data.setdefault("custom_presets", {})

    def get_central_dir(self) -> Path:
        """Menentukan folder pusat penyimpanan video."""
        override = self.data.get("central_dir_override", "").strip()
        if override:
            p = Path(override).expanduser()
            p.mkdir(parents=True, exist_ok=True)
            return p

        android_base = Path("/storage/emulated/0")
        if android_base.is_dir():
            central = android_base / "Movies" / "pusat-video_encode"
        else:
            central = Path.cwd() / "pusat-video_encode"
        central.mkdir(parents=True, exist_ok=True)
        return central

    def set_central_dir(self, new_dir: str) -> bool:
        """Menyetel override folder pusat kustom."""
        self.data["central_dir_override"] = str(new_dir).strip()
        return self.save()

    def add_custom_preset(self, name: str, settings: Dict[str, Any]) -> bool:
        """Menyimpan preset kustom baru buatan pengguna."""
        self.custom_presets[name] = dict(settings)
        return self.save()

    def delete_custom_preset(self, name: str) -> bool:
        """Menghapus preset kustom pengguna."""
        if name in self.custom_presets:
            del self.custom_presets[name]
            return self.save()
        return False


# Singleton instance untuk kemudahan import langsung
config_mgr = ConfigManager()
