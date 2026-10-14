# Bot Telegram Absen SIPRAKERIN

Kelola jurnal kehadiran PKL langsung dari Telegram: **Absen, Libur, Edit,
Hapus, Riwayat**. Bisa untuk tanggal lama (otomatis backfill) karena server
SIPRAKERIN hanya mengizinkan pembuatan jurnal untuk hari ini.

## Struktur proyek

| File / Folder | Isi |
|---|---|
| `bot.py` | Bot Telegram (inline keyboard, HTML escape aman) |
| `automation.py` | API jurnal, backfill Supabase, edit/hapus via browser, riwayat |
| `config.py` | Semua konfigurasi dibaca dari `.env` |
| `assets/` | File pendukung (mis. foto surat sakit) |
| `run.bat` | Shortcut jalanin bot di Windows |

## Syarat

- **Python 3.12** (wajib — `python-telegram-bot` belum mendukung Python 3.13
  ke atas, jadi versi lain tidak bisa dipakai; cek: `python --version`)
- **Git** (cek: `git --version`)
- Akun SIPRAKERIN (NISN + password) dan akun Telegram

## Langkah-langkah (dari clone sampai jalan)

### 1. Clone repo

```bash
git clone <URL-REPO-KAMU>
cd siprakerin-bot
```

### 2. Buat virtual environment + install dependensi

Windows (PowerShell):

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install chromium
```

Linux / macOS:

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

> `playwright install chromium` wajib — bot membuka browser di belakang
> layar untuk login dan edit/hapus.

### 3. Buat bot Telegram

1. Chat **@BotFather** di Telegram → kirim `/newbot` → ikuti instruksi
   sampai dapat **token** (contoh: `123456:AAExxx...`).
2. Chat **@userinfobot** → catat **user ID** kamu (bot dikunci hanya untuk ID ini).

### 4. Isi file `.env`

```bash
cp .env.example .env
```

Lalu isi di `.env`:

| Variabel | Cara isi |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Token dari @BotFather |
| `ALLOWED_TELEGRAM_USER_ID` | ID dari @userinfobot |
| `NISN` | NISN kamu |
| `SIPRAKERIN_EMAIL` | `NISN@siprakerin.com` |
| `SIPRAKERIN_PASSWORD` | NISN kamu (default password = NISN) |
| `SIPRAKERIN_ID_KELAS` | Lihat cara cari di bawah |
| `SIPRAKERIN_ID_INDUSTRI` | Lihat cara cari di bawah |

**Cara mencari `ID_KELAS` / `ID_INDUSTRI` (sekali saja):**

1. Buka https://www.siprakerin.com di Chrome → login → buka DevTools (`F12`) → tab **Network**.
2. Tambah satu jurnal manual lewat tombol **+**, lalu cari request
   `POST .../api/siswa/jurnal` → lihat **Payload**:
   ```json
   {"tanggal": "...", "kegiatan": "...", "keterangan": "hadir",
    "id_kelas": 1, "id_industri": "xxxxxxxx-xxxx-..."}
   ```
3. Salin `id_kelas` dan `id_industri` ke `.env`.

### 5. Jalankan bot

```bash
python bot.py
```

atau di Windows klik dua kali `run.bat`. Biarkan terminal tetap terbuka
(selama proses ini jalan, bot online). Kalau mau jalan 24/7, jalankan di
VPS / server kecil.

### 6. Pakai dari Telegram

Chat bot kamu → `/start`, lalu pilih menu:

| Perintah | Fungsi |
|---|---|
| `/absen [tgl] [kegiatan]` | Catat hadir (contoh: `/absen kemarin Slicing halaman home`) |
| `/libur [tgl] [alasan]` | Catat libur |
| `/edit [tgl] [kegiatan baru]` | Ubah jurnal yang sudah ada |
| `/hapus [tgl]` | Hapus jurnal (ada konfirmasi) |
| `/riwayat` | 10 jurnal terakhir |
| `/batal` | Batalkan proses yang sedang berjalan |
| `/help` | Bantuan |

Format tanggal: `hari ini`, `kemarin`, `besok`, `25-12-2025`,
`25/12/2025`, `2025-12-25`.

## Utilitas tanpa bot (CLI)

```bash
python automation.py api [YYYY-MM-DD] [Hadir|Libur] [kegiatan]
python automation.py riwayat [n]
python automation.py shot
```

## Troubleshooting

| Gejala | Solusi |
|---|---|
| `TELEGRAM_BOT_TOKEN belum diisi` | `.env` belum dibuat/diisi — ulangi langkah 4 |
| Login gagal / password ditolak | Cek `SIPRAKERIN_EMAIL` & `SIPRAKERIN_PASSWORD` di `.env` |
| `ID_KELAS / ID_INDUSTRI` kosong | Isi sesuai langkah 4 (cari via Network tab) |
| Browser error saat pertama jalan | Pastikan `playwright install chromium` sudah dijalankan |
| Jurnal tanggal lama "tidak masuk" | Server hanya mengizinkan hari ini — bot otomatis backfill; kalau hari ini sudah terisi, pakai `/edit` |

## Catatan teknis (server)

Server menolak pembuatan jurnal selain hari ini (`Date Too Old`) di tiga
lapisan: API, form UI (readonly), dan trigger database. Bot mengakalinya
dengan backfill via Supabase (CREATE baris hari ini + PATCH ke tanggal
target) sehingga tanggal lama tetap tersimpan dengan tanggal aslinya.
