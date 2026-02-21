# 🎬 YouTube Shorts Uploader Web Studio

Web interface lokal berbasis Flask untuk upload YouTube Shorts dengan tampilan mewah dan banyak animasi!

---

## ✨ Fitur
- 📹 Upload video dengan drag & drop ke bar
- 📱 Preview video seperti YouTube Shorts asli (frame HP)
- 🎬 Edit judul, deskripsi, kategori, tag langsung di web
- 💾 Semua settings tersimpan ke `about.json`
- 🚀 Upload ke YouTube 1 klik dengan progress bar
- 🔔 Notifikasi berbeda (sukses/error/duplikat) + suara berbeda
- 🎵 Musik notifikasi saat pertama buka web
- 📋 Riwayat semua video yang sudah diupload (dengan thumbnail)
- 🔍 Deteksi duplikat — kalau video sama sudah pernah diupload, langsung dikasih linknya
- 🎮 Default kategori: Gaming (20)

---

## 🛠️ Setup

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Setup Google API Credentials
1. Buka [Google Cloud Console](https://console.cloud.google.com)
2. Buat project baru / pilih existing
3. Enable **YouTube Data API v3**
4. Buat OAuth 2.0 Client ID (Desktop app)
5. Download JSON → rename jadi `credentials.json`
6. Taruh `credentials.json` di folder yang sama dengan `app.py`

### 3. Generate token.json (pertama kali)
Jalankan script auth sekali untuk generate token:
```bash
python auth_setup.py
```
Atau bisa juga jalankan app.py dan ikuti instruksi di terminal.

### 4. Jalankan web
```bash
python app.py
```
Buka browser: **http://localhost:5000**

---

## 📁 Struktur Folder
```
ytuploader/
├── app.py               ← Flask backend
├── requirements.txt     ← Dependencies
├── credentials.json     ← Google OAuth (kamu buat sendiri)
├── token.json           ← Auto-generated setelah auth
├── about.json           ← Settings title/desc/tags/category
├── riwayat.json         ← Log semua video yang diupload
├── uploads/             ← Temp folder video
├── riwayat/             ← Folder backup riwayat per video
└── templates/
    └── index.html       ← Web UI
```

---

## 🎵 Suara
- **Welcome** — Ting tung saat web dibuka
- **Upload bar** — Swoosh saat video masuk
- **Notifikasi** — Ding saat notif muncul
- **Sukses** — Arpeggio meriah saat upload berhasil
- **Error** — Buzz descending saat error
- **Duplikat** — Nada warning khusus
- **Save** — Ting ringan saat save settings

---

## ⚠️ Catatan
- File video dihapus otomatis dari folder `uploads/` setelah berhasil diupload ke YouTube
- Hash SHA-256 digunakan untuk deteksi duplikat
- Maksimal upload: 500MB per video
- Auto interval upload **sudah dihapus** sesuai permintaan
