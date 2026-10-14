"""Otomasi SIPRAKERIN: submit jurnal via API, backfill tanggal lama,
edit/hapus via browser, dan baca riwayat via Supabase.

Alur utama (submit_jurnal_api):
  1. Coba POST /api/siswa/jurnal dengan tanggal yang diminta.
  2. Kalau server menolak "date too old" (jurnal hanya untuk hari ini),
     otomatis backfill via Supabase: CREATE baris hari ini + PATCH ke
     tanggal target (INSERT langsung tanggal lama ditolak DB).
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import logging
from dataclasses import dataclass, field

import requests
from playwright.async_api import async_playwright
from playwright.async_api import TimeoutError as PWTimeout

from config import (
    BASE_URL,
    BULAN_ID,
    DEFAULT_KEGIATAN,
    EMAIL,
    HEADLESS,
    ID_INDUSTRI,
    ID_KELAS,
    JURNAL_API_URL,
    KEHADIRAN_URL,
    LOGIN_URL,
    PASSWORD,
    REQUEST_RETRIES,
    REQUEST_TIMEOUT,
    SUPABASE_ANON_KEY,
    SUPABASE_URL,
    missing_siprakerin_config,
)

logger = logging.getLogger(__name__)

__all__ = [
    "BULAN_ID",
    "ActionResult",
    "parse_tanggal_input",
    "submit_jurnal_api",
    "edit_kehadiran",
    "hapus_kehadiran",
    "list_riwayat",
    "JurnalRow",
]


# ---------------------------------------------------------------- util umum

@dataclass
class ActionResult:
    success: bool
    message: str


@dataclass
class JurnalRow:
    id_jurnal: int
    tanggal: str
    kegiatan: str
    keterangan: str


def _req(method: str, url: str, **kw):
    """requests + retry untuk error jaringan transien.

    Timeout per-request pendek (config REQUEST_TIMEOUT) supaya gagal-cepat,
    diulang REQUEST_RETRIES kali. Error terakhir dilempar bila tetap gagal.
    """
    kw.setdefault("timeout", REQUEST_TIMEOUT)
    last: Exception | None = None
    for attempt in range(REQUEST_RETRIES + 1):
        try:
            return requests.request(method, url, **kw)
        except (requests.Timeout, requests.ConnectionError) as e:
            last = e
            logger.warning(
                "retry %s %s (%d/%d): %s",
                method.upper(), url, attempt + 1, REQUEST_RETRIES + 1, e,
            )
            if attempt < REQUEST_RETRIES:
                import time as _time

                _time.sleep(2)
    assert last is not None
    raise last


def _check_env() -> ActionResult | None:
    missing = missing_siprakerin_config()
    if missing:
        return ActionResult(False, f"Belum diisi di .env: {', '.join(missing)}")
    return None


def today_iso() -> str:
    return _dt.date.today().strftime("%Y-%m-%d")


def display_of(d: _dt.date) -> str:
    return f"{d.day} {BULAN_ID[d.month - 1]} {d.year}"


def parse_tanggal_input(text: str) -> tuple[str, str] | None:
    """Parse input tanggal user -> (iso YYYY-MM-DD, display) atau None."""
    t = text.strip().lower()
    today = _dt.date.today()

    d: _dt.date | None = None
    if "hari ini" in t or t in ("today", "skrg", "sekarang"):
        d = today
    elif "kemarin" in t or t == "yesterday":
        d = today - _dt.timedelta(days=1)
    elif "besok" in t or t == "tomorrow":
        d = today + _dt.timedelta(days=1)
    else:
        for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d %m %Y"):
            try:
                d = _dt.datetime.strptime(text.strip(), fmt).date()
                break
            except ValueError:
                continue

    if d is None:
        return None
    return d.strftime("%Y-%m-%d"), display_of(d)


# ---------------------------------------------------------------- Supabase

@dataclass
class _SbSession:
    token: str
    id_siswa: str
    headers: dict = field(default_factory=dict)

    @property
    def auth_headers(self) -> dict:
        return {**self.headers, "Authorization": f"Bearer {self.token}"}


def _sb_login() -> tuple[_SbSession | None, ActionResult | None]:
    """Login Supabase Auth -> session (token + id_siswa)."""
    if missing := _check_env():
        return None, missing
    try:
        r_auth = _req("post", 
            f"{SUPABASE_URL}/auth/v1/token?grant_type=password",
            headers={"apikey": SUPABASE_ANON_KEY, "Content-Type": "application/json"},
            json={"email": EMAIL, "password": PASSWORD},
            timeout=REQUEST_TIMEOUT,
        )
        if r_auth.status_code != 200:
            return None, ActionResult(False, "Login Supabase gagal, cek email/password.")
        token = r_auth.json().get("access_token")
        if not token:
            return None, ActionResult(False, "Login Supabase tanpa access_token.")

        # id_siswa = sub pada JWT
        payload_b64 = token.split(".")[1] + "=="
        import base64

        sub = json.loads(base64.urlsafe_b64decode(payload_b64).decode()).get("sub")
        if not sub:
            return None, ActionResult(False, "Token Supabase tidak memuat id_siswa.")
        sess = _SbSession(
            token=token,
            id_siswa=sub,
            headers={"apikey": SUPABASE_ANON_KEY},
        )
        return sess, None
    except Exception as e:  # noqa: BLE001 - sampaikan ringkas ke user
        logger.exception("Supabase login gagal")
        return None, ActionResult(False, f"Login Supabase gagal: {e}")


def list_riwayat(limit: int = 10) -> tuple[list[JurnalRow], ActionResult | None]:
    """Ambil N jurnal terbaru milik siswa (read-only, aman)."""
    sess, err = _sb_login()
    if err:
        return [], err
    try:
        r = _req("get", 
            f"{SUPABASE_URL}/rest/v1/daftar_jurnal",
            headers=sess.auth_headers,
            params={
                "select": "id_jurnal,tanggal,kegiatan,keterangan",
                "id_siswa": f"eq.{sess.id_siswa}",
                "order": "tanggal.desc",
                "limit": str(limit),
            },
            timeout=REQUEST_TIMEOUT,
        )
        if r.status_code != 200:
            return [], ActionResult(False, "Gagal membaca riwayat jurnal.")
        rows = [
            JurnalRow(
                id_jurnal=row.get("id_jurnal", 0),
                tanggal=row.get("tanggal", "?"),
                kegiatan=(row.get("kegiatan") or "-"),
                keterangan=(row.get("keterangan") or "?"),
            )
            for row in r.json()
        ]
        return rows, None
    except Exception as e:  # noqa: BLE001
        logger.exception("list_riwayat gagal")
        return [], ActionResult(False, f"Gagal membaca riwayat: {e}")


def _sb_kelas_industri(sess: _SbSession) -> tuple[str | None, str | None]:
    """Tebak id_kelas/id_industri dari jurnal terbaru bila .env kosong."""
    try:
        r = _req("get", 
            f"{SUPABASE_URL}/rest/v1/daftar_jurnal",
            headers=sess.auth_headers,
            params={
                "select": "id_kelas,id_industri",
                "id_siswa": f"eq.{sess.id_siswa}",
                "order": "tanggal.desc",
                "limit": "1",
            },
            timeout=REQUEST_TIMEOUT,
        )
        rows = r.json() if r.status_code == 200 else []
        if rows:
            return rows[0].get("id_kelas"), rows[0].get("id_industri")
    except Exception:  # noqa: BLE001
        logger.debug("tebak kelas/industri gagal", exc_info=True)
    return None, None


def _backfill_tanggal_lama(
    tanggal_iso: str, kegiatan: str, keterangan: str
) -> ActionResult:
    """Backfill jurnal tanggal lama: CREATE baris hari ini + PATCH ke target.

    INSERT langsung tanggal lama ditolak DB (201 tapi 0 baris), sedangkan
    PATCH lolos. Dijalankan di thread terpisah (blocking requests).
    """
    sess, err = _sb_login()
    if err:
        return err

    id_kelas, id_industri = ID_KELAS, ID_INDUSTRI
    if not id_kelas or not id_industri:
        k, i = _sb_kelas_industri(sess)
        id_kelas = id_kelas or k
        id_industri = id_industri or i
    if not id_kelas or not id_industri:
        return ActionResult(
            False,
            "ID_KELAS / ID_INDUSTRI kosong dan tak bisa ditebak. Isi di .env.",
        )

    hari_ini = today_iso()
    h_json = {
        **sess.auth_headers,
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }
    try:
        # Target sudah ada? Jangan timpa diam-diam.
        r_exist = _req("get", 
            f"{SUPABASE_URL}/rest/v1/daftar_jurnal",
            headers=sess.auth_headers,
            params={
                "select": "id_jurnal",
                "id_siswa": f"eq.{sess.id_siswa}",
                "tanggal": f"eq.{tanggal_iso}",
                "keterangan": f"eq.{keterangan}",
            },
            timeout=REQUEST_TIMEOUT,
        )
        if r_exist.status_code == 200 and r_exist.json():
            return ActionResult(
                False,
                f"Jurnal tanggal {tanggal_iso} SUDAH ADA "
                f"(id: {r_exist.json()[0].get('id_jurnal')}). Pakai Edit.",
            )

        # Cari slot keterangan sementara hari ini (hindari unique constraint).
        r_today = _req("get", 
            f"{SUPABASE_URL}/rest/v1/daftar_jurnal",
            headers=sess.auth_headers,
            params={
                "select": "keterangan",
                "id_siswa": f"eq.{sess.id_siswa}",
                "tanggal": f"eq.{hari_ini}",
            },
            timeout=REQUEST_TIMEOUT,
        )
        used = {
            r.get("keterangan")
            for r in (r_today.json() if r_today.status_code == 200 else [])
        }
        temp_ket = next(
            (k for k in ("izin", "sakit", "hadir", "libur", "alpa") if k not in used),
            None,
        )
        if temp_ket is None:
            return ActionResult(
                False,
                f"Hari ini ({hari_ini}) penuh, tak ada slot sementara. "
                "Hapus salah satu baris hari ini dulu.",
            )

        r_create = _req("post", 
            f"{SUPABASE_URL}/rest/v1/daftar_jurnal",
            headers=h_json,
            json={
                "id_siswa": sess.id_siswa,
                "tanggal": hari_ini,
                "kegiatan": f"[temp backfill {tanggal_iso}] {kegiatan}",
                "keterangan": temp_ket,
                "id_kelas": int(str(id_kelas).strip()),
                "id_industri": str(id_industri).strip(),
            },
            timeout=REQUEST_TIMEOUT,
        )
        if r_create.status_code not in (200, 201) or not r_create.json():
            return ActionResult(
                False, f"Gagal membuat baris sementara: {r_create.text[:300]}"
            )
        jid = r_create.json()[0].get("id_jurnal")
        if not jid:
            return ActionResult(False, "Baris sementara tanpa id_jurnal.")

        def _cleanup() -> None:
            try:
                _req("delete", 
                    f"{SUPABASE_URL}/rest/v1/daftar_jurnal",
                    headers=sess.auth_headers,
                    params={"id_jurnal": f"eq.{jid}"},
                    timeout=REQUEST_TIMEOUT,
                )
            except Exception:  # noqa: BLE001
                logger.warning("cleanup backfill gagal", exc_info=True)

        r_patch = _req("patch", 
            f"{SUPABASE_URL}/rest/v1/daftar_jurnal",
            headers=sess.auth_headers,
            params={"id_jurnal": f"eq.{jid}"},
            json={"tanggal": tanggal_iso, "keterangan": keterangan, "kegiatan": kegiatan},
            timeout=REQUEST_TIMEOUT,
        )
        if r_patch.status_code not in (200, 204):
            _cleanup()
            return ActionResult(
                False, f"Gagal memindahkan ke tanggal lama: {r_patch.text[:300]}"
            )

        r_get = _req("get", 
            f"{SUPABASE_URL}/rest/v1/daftar_jurnal",
            headers=sess.auth_headers,
            params={"select": "tanggal", "id_jurnal": f"eq.{jid}"},
            timeout=REQUEST_TIMEOUT,
        )
        rows = r_get.json() if r_get.status_code == 200 else []
        if rows and rows[0].get("tanggal") == tanggal_iso:
            return ActionResult(
                True,
                f"Jurnal tanggal {tanggal_iso} ({keterangan}) tersimpan (id: {jid}).",
            )
        _cleanup()
        return ActionResult(False, "Verifikasi backfill gagal, dibersihkan.")
    except Exception as e:  # noqa: BLE001
        logger.exception("backfill gagal")
        return ActionResult(False, f"Backfill error: {e}")


# ---------------------------------------------------------------- API jurnal

def _site_session() -> tuple[requests.Session, dict | None]:
    """Login API situs via requests (cepat, tanpa browser).

    Mengembalikan (session_dengan_cookie, info_user|None).
    """
    s = requests.Session()
    r = s.post(
        f"{BASE_URL}/api/auth/login",
        json={"email": EMAIL, "password": PASSWORD},
        timeout=REQUEST_TIMEOUT,
    )
    if r.status_code != 200:
        return s, None
    try:
        return s, r.json().get("user")
    except Exception:  # noqa: BLE001
        return s, None


def _site_post_jurnal(
    tanggal_iso: str, kegiatan: str, keterangan: str
) -> tuple[int, dict]:
    """POST jurnal via requests session (detik, tanpa browser)."""
    s, _ = _site_session()
    r = s.post(
        JURNAL_API_URL,
        json={
            "tanggal": tanggal_iso,
            "kegiatan": kegiatan,
            "keterangan": keterangan,
            "id_kelas": int(str(ID_KELAS).strip()),
            "id_industri": str(ID_INDUSTRI).strip(),
        },
        timeout=REQUEST_TIMEOUT,
    )
    try:
        return r.status_code, r.json()
    except Exception:  # noqa: BLE001
        return r.status_code, {"raw": r.text}


def _display_to_iso(display: str) -> str | None:
    """'14 Oktober 2026' -> '2026-10-14'. None bila tak cocok."""
    try:
        parts = display.strip().split()
        if len(parts) != 3:
            return None
        day, month_name, year = parts
        month = BULAN_ID.index(month_name) + 1
        return f"{int(year):04d}-{month:02d}-{int(day):02d}"
    except Exception:  # noqa: BLE001
        return None


async def _post_jurnal_api(
    page, tanggal_iso: str, kegiatan: str, keterangan: str
) -> tuple[int, dict]:
    payload = {
        "tanggal": tanggal_iso,
        "kegiatan": kegiatan,
        "keterangan": keterangan,
        "id_kelas": int(ID_KELAS),
        "id_industri": ID_INDUSTRI,
    }
    response = await page.request.post(
        JURNAL_API_URL,
        data=json.dumps(payload),
        headers={"content-type": "application/json", "accept": "application/json"},
    )
    try:
        body = await response.json()
    except Exception:  # noqa: BLE001
        body = {"raw": await response.text()}
    return response.status, body


async def _submit_via_browser(
    tanggal_iso: str, kegiatan: str, keterangan: str
) -> tuple[int, dict]:
    """POST jurnal memakai cookie browser Playwright (fallback lambat)."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=HEADLESS)
        context = await browser.new_context()
        page = await context.new_page()
        try:
            login_result = await _login(page)
            if not login_result.success:
                return -1, {"error": login_result.message}
            return await _post_jurnal_api(page, tanggal_iso, kegiatan, keterangan)
        finally:
            try:
                await browser.close()
            except Exception:  # noqa: BLE001
                pass


async def submit_jurnal_api(
    tanggal_iso: str | None = None, kegiatan: str | None = None, status: str = "Hadir"
) -> ActionResult:
    """Submit jurnal; otomatis backfill bila server menolak tanggal lama.

    Jalur cepat (requests, hitungan detik) dicoba dulu; browser Playwright
    hanya dipakai sebagai fallback bila jalur cepat gagal total.
    """
    if missing := _check_env():
        return missing

    status = status.strip().capitalize()
    if status not in ("Hadir", "Libur"):
        return ActionResult(False, "Status harus 'Hadir' atau 'Libur'.")

    if tanggal_iso:
        try:
            _dt.datetime.strptime(tanggal_iso, "%Y-%m-%d")
        except ValueError:
            return ActionResult(
                False, f"Format tanggal '{tanggal_iso}' tidak valid (YYYY-MM-DD)."
            )
    else:
        tanggal_iso = today_iso()

    kegiatan = kegiatan or DEFAULT_KEGIATAN
    keterangan = status.lower()

    # 1. Jalur cepat dulu (tanpa browser).
    try:
        status_code, body = await asyncio.to_thread(
            _site_post_jurnal, tanggal_iso, kegiatan, keterangan
        )
    except Exception:  # noqa: BLE001 — jaringan gagal total -> fallback browser
        logger.warning("jalur cepat gagal, pakai browser", exc_info=True)
        status_code, body = await _submit_via_browser(
            tanggal_iso, kegiatan, keterangan
        )

    if status_code == -1:
        return ActionResult(False, f"Login gagal: {body.get('error')}")

    if status_code in (200, 201):
        return ActionResult(True, f"Jurnal tanggal {tanggal_iso} ({status}) tersimpan.")
    if status_code == 409:
        existing = body.get("existing", {}) if isinstance(body, dict) else {}
        return ActionResult(
            False,
            f"Jurnal tanggal {tanggal_iso} SUDAH ADA "
            f"({existing.get('keterangan', '?')}, id: {existing.get('id_jurnal', '?')}). "
            "Pakai Edit untuk mengubahnya.",
        )
    if status_code == 400 and "date too old" in str(body).lower():
        # API menolak tanggal lama -> backfill via Supabase
        # (CREATE hari ini + PATCH ke tanggal target).
        return await asyncio.to_thread(
            _backfill_tanggal_lama, tanggal_iso, kegiatan, keterangan
        )
    return ActionResult(False, f"API menolak ({status_code}): {body}")


# ---------------------------------------------------------------- Browser UI

async def _find_first(page, locators):
    for locator in locators:
        try:
            if await locator.count() > 0:
                return locator.first
        except Exception:  # noqa: BLE001
            continue
    return None


async def _login(page) -> ActionResult:
    if missing := _check_env():
        return missing
    await page.goto(LOGIN_URL, wait_until="networkidle")

    email_input = await _find_first(
        page,
        [
            page.get_by_placeholder("Email", exact=False),
            page.locator('input[type="email"]'),
            page.locator('input[name="email"]'),
        ],
    )
    password_input = await _find_first(
        page,
        [
            page.get_by_placeholder("Password", exact=False),
            page.locator('input[type="password"]'),
            page.locator('input[name="password"]'),
        ],
    )
    if email_input is None or password_input is None:
        return ActionResult(False, "Form login tidak ditemukan.")

    await email_input.fill(EMAIL)
    await password_input.fill(PASSWORD)

    submit_btn = await _find_first(
        page,
        [
            page.get_by_role("button", name="Log in", exact=False),
            page.locator('button[type="submit"]'),
        ],
    )
    if submit_btn is None:
        return ActionResult(False, "Tombol login tidak ditemukan.")

    await submit_btn.click()
    try:
        await page.wait_for_url(lambda url: "login" not in url, timeout=15000)
    except PWTimeout:
        pass

    if "login" in page.url:
        return ActionResult(False, "Login gagal: email/password ditolak.")
    return ActionResult(True, "Login berhasil.")


async def _find_row_by_tanggal(page, tanggal_display: str):
    rows = page.locator("table tbody tr", has_text=tanggal_display)
    for i in range(await rows.count()):
        row = rows.nth(i)
        try:
            if await row.is_visible():
                return row
        except Exception:  # noqa: BLE001
            continue
    return None


async def _fill_kegiatan_status(page, kegiatan: str, status: str) -> ActionResult:
    """Isi textarea kegiatan + dropdown keterangan pada form yang TERBUKA."""
    desc = await _find_first(
        page,
        [
            page.get_by_placeholder("Kegiatan", exact=False),
            page.get_by_placeholder("Isi keterangan", exact=False),
            page.locator("textarea").last,
        ],
    )
    if desc is None:
        return ActionResult(False, "Field kegiatan tidak ditemukan.")
    await desc.fill(kegiatan)
    await page.wait_for_timeout(300)
    if (await desc.input_value()).strip() != kegiatan.strip():
        return ActionResult(False, "Gagal mengisi field kegiatan.")

    select_el = page.locator('select[name="keterangan"]')
    if await select_el.count() == 0:
        select_el = page.locator("select").last
    try:
        if await select_el.count() > 0:
            options = await select_el.locator("option").all_text_contents()
            match = next(
                (o for o in options if o.strip().lower().startswith(status.lower())),
                None,
            )
            if match:
                await select_el.select_option(label=match)
                return ActionResult(True, "ok")
    except Exception:  # noqa: BLE001
        logger.debug("pilih status gagal", exc_info=True)
    return ActionResult(False, f"Pilihan status '{status}' tidak ditemukan.")


async def _click_save(page) -> ActionResult:
    save_btn = await _find_first(
        page,
        [
            page.get_by_role("button", name="Simpan", exact=False),
            page.locator('button[type="submit"]').last,
        ],
    )
    if save_btn is None:
        return ActionResult(False, "Tombol simpan tidak ditemukan.")
    await save_btn.click()
    await page.wait_for_timeout(2000)
    text = (await page.content()).lower()
    if any(k in text for k in ("berhasil", "sukses", "tersimpan")):
        return ActionResult(True, "Tersimpan.")
    if any(k in text for k in ("gagal", "error", "date too old")):
        return ActionResult(False, "Server menolak penyimpanan.")
    return ActionResult(False, "Tidak ada konfirmasi sukses dari server.")


def _sb_rows_on_date(sess: _SbSession, tanggal_iso: str) -> list[dict] | None:
    """Baris jurnal milik siswa pada tanggal tsb. None bila request gagal."""
    try:
        r = _req("get", 
            f"{SUPABASE_URL}/rest/v1/daftar_jurnal",
            headers=sess.auth_headers,
            params={
                "select": "id_jurnal,tanggal,kegiatan,keterangan",
                "id_siswa": f"eq.{sess.id_siswa}",
                "tanggal": f"eq.{tanggal_iso}",
            },
            timeout=REQUEST_TIMEOUT,
        )
        return r.json() if r.status_code == 200 else None
    except Exception:  # noqa: BLE001
        return None


def _edit_via_supabase(
    tanggal_iso: str, kegiatan_baru: str, status_baru: str
) -> ActionResult | None:
    """PATCH cepat. None = tak bisa diputuskan cepat -> pakai browser.

    Hanya dipakai bila tepat 1 baris (aman, tak ambigu).
    """
    sess, err = _sb_login()
    if err:
        return None
    rows = _sb_rows_on_date(sess, tanggal_iso)
    if not rows:
        return ActionResult(False, f"Tidak ada jurnal tanggal {tanggal_iso}.")
    if len(rows) != 1:
        return None
    jid = rows[0]["id_jurnal"]
    try:
        r = _req("patch", 
            f"{SUPABASE_URL}/rest/v1/daftar_jurnal",
            headers=sess.auth_headers,
            params={"id_jurnal": f"eq.{jid}"},
            json={"kegiatan": kegiatan_baru, "keterangan": status_baru.lower()},
            timeout=REQUEST_TIMEOUT,
        )
        if r.status_code not in (200, 204):
            return None
        verify = _sb_rows_on_date(sess, tanggal_iso)
        if verify and verify[0].get("kegiatan") == kegiatan_baru:
            return ActionResult(True, f"Jurnal tanggal {tanggal_iso} diperbarui.")
        return None
    except Exception:  # noqa: BLE001
        return None


def _hapus_via_supabase(tanggal_iso: str) -> ActionResult | None:
    """DELETE cepat. None = tak bisa diputuskan cepat -> pakai browser.

    Hanya dipakai bila tepat 1 baris (aman, tak ambigu).
    """
    sess, err = _sb_login()
    if err:
        return None
    rows = _sb_rows_on_date(sess, tanggal_iso)
    if not rows:
        return ActionResult(False, f"Tidak ada jurnal tanggal {tanggal_iso}.")
    if len(rows) != 1:
        return None
    jid = rows[0]["id_jurnal"]
    try:
        r = _req("delete", 
            f"{SUPABASE_URL}/rest/v1/daftar_jurnal",
            headers=sess.auth_headers,
            params={"id_jurnal": f"eq.{jid}"},
            timeout=REQUEST_TIMEOUT,
        )
        if r.status_code not in (200, 204):
            return None
        if not _sb_rows_on_date(sess, tanggal_iso):
            return ActionResult(True, f"Jurnal tanggal {tanggal_iso} dihapus.")
        return None
    except Exception:  # noqa: BLE001
        return None


async def edit_kehadiran(
    kegiatan_baru: str,
    status_baru: str = "Hadir",
    tanggal_cari_display: str | None = None,
    tanggal_cari_iso: str | None = None,
) -> ActionResult:
    """Edit jurnal pada tanggal tertentu (jalur cepat dulu, browser fallback)."""
    if missing := _check_env():
        return missing

    tanggal_cari_display = tanggal_cari_display or display_of(_dt.date.today())
    status_baru = status_baru.strip().capitalize()
    if status_baru not in ("Hadir", "Libur"):
        return ActionResult(False, "Status harus 'Hadir' atau 'Libur'.")

    iso = tanggal_cari_iso or _display_to_iso(tanggal_cari_display)
    if iso:
        fast = await asyncio.to_thread(_edit_via_supabase, iso, kegiatan_baru, status_baru)
        if fast is not None:
            return fast

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=HEADLESS)
        context = await browser.new_context()
        page = await context.new_page()
        try:
            login_result = await _login(page)
            if not login_result.success:
                return login_result

            await page.goto(KEHADIRAN_URL, wait_until="networkidle")
            await page.wait_for_timeout(1000)

            row = await _find_row_by_tanggal(page, tanggal_cari_display)
            if row is None:
                return ActionResult(
                    False, f"Tidak ada jurnal '{tanggal_cari_display}'."
                )
            buttons = row.locator("td").last.locator("button")
            if await buttons.count() < 1:
                return ActionResult(False, "Tombol edit tidak ditemukan.")
            await buttons.nth(0).click()
            await page.wait_for_timeout(1000)

            fill = await _fill_kegiatan_status(page, kegiatan_baru, status_baru)
            if not fill.success:
                return fill
            saved = await _click_save(page)
            if not saved.success:
                return saved
            return ActionResult(
                True, f"Jurnal '{tanggal_cari_display}' berhasil diperbarui."
            )
        finally:
            await browser.close()


async def hapus_kehadiran(
    tanggal_display: str | None = None, tanggal_iso: str | None = None
) -> ActionResult:
    """Hapus jurnal pada tanggal tertentu (jalur cepat dulu, browser fallback)."""
    if missing := _check_env():
        return missing

    tanggal_display = tanggal_display or display_of(_dt.date.today())

    iso = tanggal_iso or _display_to_iso(tanggal_display)
    if iso:
        fast = await asyncio.to_thread(_hapus_via_supabase, iso)
        if fast is not None:
            return fast

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=HEADLESS)
        context = await browser.new_context()
        page = await context.new_page()
        page.on("dialog", lambda d: asyncio.ensure_future(d.accept()))
        try:
            login_result = await _login(page)
            if not login_result.success:
                return login_result

            await page.goto(KEHADIRAN_URL, wait_until="networkidle")
            await page.wait_for_timeout(1000)

            row = await _find_row_by_tanggal(page, tanggal_display)
            if row is None:
                return ActionResult(
                    False, f"Tidak ada jurnal '{tanggal_display}'."
                )
            buttons = row.locator("td").last.locator("button")
            if await buttons.count() < 2:
                return ActionResult(False, "Tombol hapus tidak ditemukan.")
            await buttons.nth(1).click()
            await page.wait_for_timeout(800)

            confirm = await _find_first(
                page,
                [
                    page.get_by_role("button", name="Ya, hapus", exact=False),
                    page.get_by_role("button", name="Ya", exact=False),
                    page.get_by_role("button", name="Hapus", exact=True),
                ],
            )
            if confirm is not None:
                await confirm.click()
                await page.wait_for_timeout(1000)

            if await _find_row_by_tanggal(page, tanggal_display) is None:
                return ActionResult(
                    True, f"Jurnal '{tanggal_display}' berhasil dihapus."
                )
            return ActionResult(
                False, f"Jurnal '{tanggal_display}' masih ada setelah dihapus."
            )
        finally:
            await browser.close()


async def screenshot_kehadiran_page(path: str = "kehadiran_page.png") -> ActionResult:
    if missing := _check_env():
        return missing
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=HEADLESS)
        context = await browser.new_context()
        page = await context.new_page()
        try:
            login_result = await _login(page)
            if not login_result.success:
                return login_result
            await page.goto(KEHADIRAN_URL, wait_until="networkidle")
            await page.wait_for_timeout(1000)
            await page.screenshot(path=path, full_page=True)
            return ActionResult(True, f"Screenshot disimpan: {path}")
        finally:
            await browser.close()


# ---------------------------------------------------------------- CLI

if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO)

    def _run(coro):
        print(("OK " if (r := asyncio.run(coro)).success else "GAGAL ") + r.message)

    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print("pakai: api [YYYY-MM-DD] [Hadir|Libur] [kegiatan] | riwayat [n] | shot")
    elif args[0] == "api":
        _run(submit_jurnal_api(
            args[1] if len(args) > 1 else None,
            args[3] if len(args) > 3 else None,
            args[2] if len(args) > 2 else "Hadir",
        ))
    elif args[0] == "riwayat":
        rows, err = asyncio.run(asyncio.to_thread(list_riwayat, int(args[1]) if len(args) > 1 else 10))
        if err:
            print("GAGAL " + err.message)
        else:
            for row in rows:
                print(f"{row.tanggal} [{row.keterangan}] {row.kegiatan[:80]}")
    elif args[0] == "shot":
        _run(screenshot_kehadiran_page())
    else:
        print("perintah tak dikenal. pakai --help")
