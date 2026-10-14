"""Konfigurasi terpusat: baca .env sekali, validasi, dan sediakan konstanta.

Jangan taruh kredensial langsung di kode — semua lewat environment.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()

BASE_URL = "https://www.siprakerin.com"
LOGIN_URL = f"{BASE_URL}/login"
KEHADIRAN_URL = f"{BASE_URL}/siswa/kehadiran"
JURNAL_API_URL = f"{BASE_URL}/api/siswa/jurnal"

SUPABASE_URL = os.getenv("SUPABASE_URL", "https://supabase.siprakerin.com")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY") or os.getenv("SUPABASE_KEY", "")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
ALLOWED_USER_ID = os.getenv("ALLOWED_TELEGRAM_USER_ID", "")

EMAIL = os.getenv("SIPRAKERIN_EMAIL", "")
PASSWORD = os.getenv("SIPRAKERIN_PASSWORD", "")
DEFAULT_KEGIATAN = os.getenv("DEFAULT_KEGIATAN", "Kegiatan PKL harian")

ID_KELAS = os.getenv("SIPRAKERIN_ID_KELAS", "")
ID_INDUSTRI = os.getenv("SIPRAKERIN_ID_INDUSTRI", "")

HEADLESS = os.getenv("HEADLESS", "true").lower() != "false"

REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "15"))
REQUEST_RETRIES = int(os.getenv("REQUEST_RETRIES", "2"))

BULAN_ID = [
    "Januari", "Februari", "Maret", "April", "Mei", "Juni",
    "Juli", "Agustus", "September", "Oktober", "November", "Desember",
]


def missing_bot_config() -> list[str]:
    missing = []
    if not BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    return missing


def missing_siprakerin_config() -> list[str]:
    missing = []
    if not EMAIL:
        missing.append("SIPRAKERIN_EMAIL")
    if not PASSWORD:
        missing.append("SIPRAKERIN_PASSWORD")
    if not ID_KELAS:
        missing.append("SIPRAKERIN_ID_KELAS")
    if not ID_INDUSTRI:
        missing.append("SIPRAKERIN_ID_INDUSTRI")
    return missing
