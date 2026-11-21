# TikTok Compression Patcher Pro (v2.1.0)
Anti-Compression HD Re-encoder untuk TikTok & Reels (Alight Motion / CapCut / AMV / Jedag-Jedug)

---

## 🌟 Yang Baru di Versi Upgrade (v2.1.0)

### 1. ⚙️ Konfigurasi Terpisah & Tersimpan Permanen (`config.py` & `config.json`)
- **Tersimpan Otomatis**: Setiap kali kamu mengubah bitrate, preset, framerate, atau fitur lainnya di menu, pengaturannya otomatis tersimpan ke `config.json`.
- **Tidak Hilang Saat Keluar**: Ketika skrip ditutup atau terminal di-restart, konfigurasi yang kamu atur tetap tersimpan.
- **Preset Kustom**: Kamu bisa menyimpan preset racikan sendiri (misal: *"Preset Jedag Jedug 60fps"*, *"Preset Anime Anti-Blur"*) dan memuatnya kapan saja melalui menu **[7]**.
- **Folder Pusat Fleksibel**: Kamu bisa mengubah folder penyimpanan video sesuai keinginan tanpa harus mengedit kode.

### 2. ⚡ Mode Pemrosesan Batch (Multi-Video Sekaligus)
- Kamu tidak perlu lagi memilih video satu per satu!
- Dukungan pilihan:
  - Pilih satu video: `1`
  - Pilih beberapa video: `1, 3, 5` atau `1 3 5`
  - Pilih rentang video: `1-4`
  - Pilih SEMUA video sekaligus: ketik `all` atau `a`
  - Drag-and-drop / paste path video eksternal langsung ke terminal.

### 3. ✨ Filter Smart Sharpen (Anti-Blur TikTok)
- TikTok mengompres video dengan algoritma blurring/downscaling yang membuat teks dan efek Alight Motion terlihat buram.
- Fitur **Smart Sharpening (Unsharp Mask)** meningkatkan ketajaman micro-contrast pada sudut dan garis objek sehingga video tetap terlihat tajam (*ultra-crisp*) setelah dikompresi TikTok.

### 4. 🔊 Audio Loudness Normalization (EBU R128)
- Menormalkan audio video ke standar TikTok (-14 LUFS, True Peak -1.5 dB).
- Mencegah audio video terlalu kecil saat ditonton di TikTok atau jomplang dengan sound tren lainnya.

### 5. 🎯 Paksa 60 FPS (Target Framerate)
- Opsi untuk memaksa framerate ke 60 FPS halus atau mempertahankan framerate asli video renderan.

### 6. 🚀 Deteksi Hardware Acceleration (GPU Encode)
- Otomatis mendeteksi encoder GPU yang didukung di perangkat:
  - **libx264** (CPU - Kualitas & Kompatibilitas Tertinggi)
  - **h264_nvenc** (NVIDIA GPU)
  - **h264_qsv** (Intel QuickSync)
  - **h264_videotoolbox** (Apple Mac / M1 / M2 / M3)
  - **h264_mediacodec** (Android Hardware)

### 7. 🔍 Video Inspector / Analisis Detail
- Menu **[6]** menampilkan informasi mendalam mengenai file video: resolusi asli, rasio aspek, durasi, framerate (FPS), codec video & audio, format pixel, dan bitrate.

---

## 📂 Struktur File

| File | Deskripsi |
|---|---|
| `tools.py` | Program utama dengan CLI dan antarmuka menu interaktif. |
| `config.py` | Modul pengelola konfigurasi (`ConfigManager`), schema, dan handler I/O. |
| `config.json` | File konfigurasi format JSON tempat semua pengaturan tersimpan permanen. |

---

## 🚀 Cara Menjalankan

### Mode Menu Interaktif
```bash
python tools.py
```

### Mode Command Line (CLI)
```bash
# Patch video biasa
python tools.py -i video_input.mp4

# Re-encode + Crop ke rasio 9:16 (TikTok / Reels)
python tools.py -i video_input.mp4 -c 9:16

# Stabilisasi video (anti-goyang)
python tools.py -i video_input.mp4 -s cepat

# Patch dengan Smart Sharpening & Audio Loudnorm aktif
python tools.py -i video_input.mp4 --sharpen --normalize-audio --fps 60

# Proses seluruh video yang ada di folder pusat
python tools.py --batch-all

# Reset pengaturan ke default awal pabrik
python tools.py --reset-config
```
