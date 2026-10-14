"""Bot Telegram SIPRAKERIN — absen, libur, edit, hapus, riwayat.

UI memakai inline keyboard (tidak memenuhi layar), semua teks user
di-escape (parse_mode HTML) agar aman dari karakter Markdown.
Pekerjaan browser/API dijalankan di thread terpisah agar polling tetap responsif.
"""

from __future__ import annotations

import asyncio
import html
import logging
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from automation import (
    edit_kehadiran,
    hapus_kehadiran,
    list_riwayat,
    parse_tanggal_input,
    submit_jurnal_api,
)
from config import ALLOWED_USER_ID, BOT_TOKEN, missing_bot_config

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------- state

DATE, TEXT, MANUAL_DATE, CONFIRM_DEL = range(4)

# ---------------------------------------------------------------- keyboard

MENU_KB = InlineKeyboardMarkup(
    [
        [
            InlineKeyboardButton("📝 Absen", callback_data="m:absen"),
            InlineKeyboardButton("🏖️ Libur", callback_data="m:libur"),
        ],
        [
            InlineKeyboardButton("✏️ Edit", callback_data="m:edit"),
            InlineKeyboardButton("🗑️ Hapus", callback_data="m:hapus"),
        ],
        [
            InlineKeyboardButton("📋 Riwayat", callback_data="m:riwayat"),
            InlineKeyboardButton("ℹ️ Bantuan", callback_data="m:help"),
        ],
    ]
)

DATE_KB = InlineKeyboardMarkup(
    [
        [
            InlineKeyboardButton("📅 Hari ini", callback_data="d:today"),
            InlineKeyboardButton("⏪ Kemarin", callback_data="d:yesterday"),
        ],
        [InlineKeyboardButton("⌨️ Ketik tanggal manual", callback_data="d:manual")],
        [InlineKeyboardButton("❌ Batal", callback_data="d:cancel")],
    ]
)

BACK_KB = InlineKeyboardMarkup(
    [[InlineKeyboardButton("🏠 Menu utama", callback_data="m:menu")]]
)

CANCEL_KB = InlineKeyboardMarkup(
    [[InlineKeyboardButton("❌ Batal", callback_data="d:cancel")]]
)

DEL_KB = InlineKeyboardMarkup(
    [
        [InlineKeyboardButton("✅ Ya, hapus", callback_data="del:yes")],
        [InlineKeyboardButton("↩️ Jangan hapus", callback_data="del:no")],
    ]
)

WELCOME = (
    "<b>👋 SIPRAKERIN Bot</b>\n\n"
    "Kelola jurnal kehadiran langsung dari Telegram.\n"
    "Pilih menu di bawah:"
)

HELP = (
    "<b>ℹ️ Bantuan</b>\n\n"
    "📝 <b>Absen</b> — catat kegiatan (Hadir), bisa tanggal lama.\n"
    "🏖️ <b>Libur</b> — catat alasan libur.\n"
    "✏️ <b>Edit</b> — ubah kegiatan yang sudah tersimpan.\n"
    "🗑️ <b>Hapus</b> — hapus jurnal per tanggal.\n"
    "📋 <b>Riwayat</b> — 10 jurnal terakhir.\n\n"
    "Format tanggal: <code>hari ini</code>, <code>kemarin</code>, "
    "<code>25-12-2025</code>, <code>25/12/2025</code>, <code>2025-12-25</code>.\n\n"
    "Command: <code>/absen [tgl] [kegiatan]</code> • <code>/libur</code> • "
    "<code>/edit</code> • <code>/hapus</code> • <code>/riwayat</code>"
)

PROMPT = {
    "absen": ("📝 <b>Absen</b>\n\nPilih tanggal jurnal:", "💬 Tulis kegiatan yang dicatat:"),
    "libur": ("🏖️ <b>Libur</b>\n\nPilih tanggal libur:", "💬 Tulis alasan libur:"),
    "edit": ("✏️ <b>Edit</b>\n\nPilih tanggal jurnal yang diubah:", "💬 Tulis kegiatan pengganti:"),
    "hapus": ("🗑️ <b>Hapus</b>\n\nPilih tanggal jurnal yang dihapus:", ""),
}

# ---------------------------------------------------------------- helper


def _allowed(update: Update) -> bool:
    if not ALLOWED_USER_ID:
        return True
    return str(update.effective_user.id) == str(ALLOWED_USER_ID)


async def _deny(update: Update) -> None:
    await update.message.reply_text(
        "🔒 <b>Akses ditolak.</b> Akun ini tidak diizinkan.",
        parse_mode=ParseMode.HTML,
    )


def _esc(s: str | None) -> str:
    return html.escape(s or "")


async def _menu(update: Update, text: str = WELCOME) -> None:
    if update.callback_query:
        await update.callback_query.edit_message_text(
            text, parse_mode=ParseMode.HTML, reply_markup=MENU_KB
        )
    else:
        await update.message.reply_text(
            text, parse_mode=ParseMode.HTML, reply_markup=MENU_KB
        )


def _args_tanggal_kegiatan(args: list[str]) -> tuple[str, str | None]:
    """Pecah args command -> (tanggal_text, kegiatan|None)."""
    if not args:
        return "hari ini", None
    if parse_tanggal_input(args[0]):
        return args[0], (" ".join(args[1:]) or None)
    return "hari ini", " ".join(args)


# ---------------------------------------------------------------- command dasar


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _allowed(update):
        return await _deny(update)
    context.user_data.clear()
    await update.message.reply_text(WELCOME, parse_mode=ParseMode.HTML, reply_markup=MENU_KB)


async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        f"🆔 ID Telegram kamu: <code>{update.effective_user.id}</code>",
        parse_mode=ParseMode.HTML,
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _allowed(update):
        return await _deny(update)
    await update.message.reply_text(HELP, parse_mode=ParseMode.HTML, reply_markup=BACK_KB)


async def riwayat_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _allowed(update):
        return await _deny(update)
    msg = await update.message.reply_text("⏳ Mengambil riwayat…")
    rows, err = await asyncio.to_thread(list_riwayat, 10)
    if err:
        await msg.edit_text(f"❌ {_esc(err.message)}", reply_markup=BACK_KB)
        return
    if not rows:
        await msg.edit_text("📋 Belum ada jurnal.", reply_markup=BACK_KB)
        return
    lines = ["<b>📋 10 Jurnal Terakhir</b>\n"]
    for r in rows:
        keg = _esc(r.kegiatan[:60] + ("…" if len(r.kegiatan) > 60 else ""))
        lines.append(f"• <b>{_esc(r.tanggal)}</b> [{_esc(r.keterangan)}]\n  {keg}")
    await msg.edit_text(
        "\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=BACK_KB
    )


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await _menu(update, "↩️ Dibatalkan. Pilih menu:")
    return ConversationHandler.END


# ---------------------------------------------------------------- alur utama

async def _start_mode(
    update: Update, context: ContextTypes.DEFAULT_TYPE, mode: str
) -> int:
    context.user_data.clear()
    context.user_data["mode"] = mode
    title, _ = PROMPT[mode]
    args = getattr(context, "args", []) or []

    if args and update.message:
        tanggal_text, kegiatan = _args_tanggal_kegiatan(args)
        parsed = parse_tanggal_input(tanggal_text)
        if not parsed:
            await update.message.reply_text(
                "⚠️ Tanggal tidak dikenali.", reply_markup=DATE_KB
            )
            return DATE
        return await _set_tanggal(update, context, parsed, kegiatan)

    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(
            title, parse_mode=ParseMode.HTML, reply_markup=DATE_KB
        )
    else:
        await update.message.reply_text(
            title, parse_mode=ParseMode.HTML, reply_markup=DATE_KB
        )
    return DATE


async def _set_tanggal(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    parsed: tuple[str, str],
    kegiatan: str | None = None,
) -> int:
    iso, display = parsed
    context.user_data["tanggal_iso"] = iso
    context.user_data["tanggal_display"] = display
    mode = context.user_data.get("mode", "absen")

    target = update.callback_query or update.message
    if mode == "hapus":
        await _send(
            target,
            f"⚠️ <b>Konfirmasi hapus</b>\n\nJurnal <b>{_esc(display)}</b> akan "
            "dihapus permanen. Lanjutkan?",
            DEL_KB,
        )
        return CONFIRM_DEL

    if kegiatan:
        return await _run_action(update, context, kegiatan)

    _, ask = PROMPT[mode]
    await _send(
        target,
        f"📅 <b>{_esc(display)}</b>\n\n{ask}\n(Ketik /batal untuk membatalkan)",
        CANCEL_KB,
    )
    return TEXT


async def _send(target, text: str, kb=None) -> None:
    if hasattr(target, "edit_message_text"):
        try:
            await target.edit_message_text(
                text, parse_mode=ParseMode.HTML, reply_markup=kb
            )
            return
        except Exception:  # noqa: BLE001 — fallback kirim baru
            pass
    await target.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


async def on_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not _allowed(update):
        await query.answer("Akses ditolak", show_alert=True)
        return ConversationHandler.END
    await query.answer()
    action = query.data.split(":", 1)[1]
    if action == "menu":
        await _menu(update)
        return ConversationHandler.END
    if action == "riwayat":
        rows, err = await asyncio.to_thread(list_riwayat, 10)
        if err:
            await query.edit_message_text(f"❌ {_esc(err.message)}", reply_markup=BACK_KB)
        elif not rows:
            await query.edit_message_text("📋 Belum ada jurnal.", reply_markup=BACK_KB)
        else:
            lines = ["<b>📋 10 Jurnal Terakhir</b>\n"]
            for r in rows:
                keg = _esc(r.kegiatan[:60] + ("…" if len(r.kegiatan) > 60 else ""))
                lines.append(f"• <b>{_esc(r.tanggal)}</b> [{_esc(r.keterangan)}]\n  {keg}")
            await query.edit_message_text(
                "\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=BACK_KB
            )
        return ConversationHandler.END
    if action == "help":
        await query.edit_message_text(HELP, parse_mode=ParseMode.HTML, reply_markup=BACK_KB)
        return ConversationHandler.END
    return await _start_mode(update, context, action)


async def on_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    key = query.data.split(":", 1)[1]

    if key == "cancel":
        context.user_data.clear()
        await query.edit_message_text("↩️ Dibatalkan.", reply_markup=MENU_KB)
        return ConversationHandler.END
    if key == "manual":
        await query.edit_message_text(
            "⌨️ Ketik tanggalnya, contoh:\n<code>25-12-2025</code> atau <code>kemarin</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=CANCEL_KB,
        )
        return MANUAL_DATE

    parsed = parse_tanggal_input({"today": "hari ini", "yesterday": "kemarin"}[key])
    assert parsed
    return await _set_tanggal(update, context, parsed)


async def on_manual_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    parsed = parse_tanggal_input(update.message.text.strip())
    if not parsed:
        await update.message.reply_text(
            "⚠️ Tanggal tidak dikenali. Contoh: <code>25-12-2025</code>, "
            "<code>kemarin</code>, <code>hari ini</code>.",
            parse_mode=ParseMode.HTML,
            reply_markup=CANCEL_KB,
        )
        return MANUAL_DATE
    # ganti pesan user dengan ringkasan rapi
    return await _set_tanggal(update, context, parsed)


async def _run_action(
    update: Update, context: ContextTypes.DEFAULT_TYPE, text: str
) -> int:
    mode = context.user_data.pop("mode", "absen")
    iso = context.user_data.pop("tanggal_iso", None)
    display = context.user_data.pop("tanggal_display", "Hari ini")
    src = update.callback_query or update.message

    if mode == "hapus":
        await _send(src, f"🗑️ Menghapus jurnal <b>{_esc(display)}</b>…")
        t0 = time.monotonic()
        res = await asyncio.to_thread(hapus_kehadiran, display, iso)
        elapsed = time.monotonic() - t0
    elif mode == "edit":
        await _send(
            src,
            f"✏️ Memperbarui jurnal <b>{_esc(display)}</b>…\n📌 Baru: {_esc(text)}",
        )
        t0 = time.monotonic()
        res = await asyncio.to_thread(edit_kehadiran, text, "Hadir", display, iso)
        elapsed = time.monotonic() - t0
    else:
        status = "Libur" if mode == "libur" else "Hadir"
        icon = "🏖️" if mode == "libur" else "📝"
        label = "Libur" if mode == "libur" else "Absen"
        await _send(
            src,
            f"{icon} Memproses <b>{label}</b>…\n📅 {_esc(display)}\n📌 {_esc(text)}",
        )
        t0 = time.monotonic()
        res = await asyncio.to_thread(submit_jurnal_api, iso, text, status)
        elapsed = time.monotonic() - t0

    prefix = "✅" if res.success else "❌"
    await _send(src, f"{prefix} {_esc(res.message)}\n\n<i>⏱️ {elapsed:.0f} detik</i>", BACK_KB)
    if not res.success:
        logger.warning("aksi %s gagal: %s", mode, res.message)
    return ConversationHandler.END


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()
    if not text:
        await update.message.reply_text("⚠️ Tulis keterangannya dulu.")
        return TEXT
    return await _run_action(update, context, text)


async def on_del_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    display = context.user_data.pop("tanggal_display", "Hari ini")
    iso = context.user_data.pop("tanggal_iso", None)
    context.user_data.pop("mode", None)

    if query.data == "del:yes":
        await query.edit_message_text(f"🗑️ Menghapus jurnal <b>{_esc(display)}</b>…")
        res = await asyncio.to_thread(hapus_kehadiran, display, iso)
        prefix = "✅" if res.success else "❌"
        await query.edit_message_text(
            f"{prefix} {_esc(res.message)}", reply_markup=BACK_KB
        )
    else:
        await query.edit_message_text(
            "↩️ Penghapusan dibatalkan. Jurnal tetap tersimpan.", reply_markup=MENU_KB
        )
    return ConversationHandler.END


# entry command per fitur (mendukung args: /absen [tgl] [kegiatan])

async def absen_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _allowed(update):
        return await _deny(update) or ConversationHandler.END
    return await _start_mode(update, context, "absen")


async def libur_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _allowed(update):
        return await _deny(update) or ConversationHandler.END
    return await _start_mode(update, context, "libur")


async def edit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _allowed(update):
        return await _deny(update) or ConversationHandler.END
    return await _start_mode(update, context, "edit")


async def hapus_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _allowed(update):
        return await _deny(update) or ConversationHandler.END
    return await _start_mode(update, context, "hapus")


# ---------------------------------------------------------------- main


def build_app() -> Application:
    if missing_bot_config():
        raise SystemExit("TELEGRAM_BOT_TOKEN belum diisi di .env")

    app = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("absen", absen_cmd),
            CommandHandler("libur", libur_cmd),
            CommandHandler("edit", edit_cmd),
            CommandHandler("hapus", hapus_cmd),
            CallbackQueryHandler(on_menu, pattern=r"^m:(absen|libur|edit|hapus|riwayat|help|menu)$"),
        ],
        states={
            DATE: [CallbackQueryHandler(on_date, pattern=r"^d:(today|yesterday|manual|cancel)$")],
            MANUAL_DATE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, on_manual_date)
            ],
            TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_text)],
            CONFIRM_DEL: [CallbackQueryHandler(on_del_confirm, pattern=r"^del:(yes|no)$")],
        },
        fallbacks=[
            CommandHandler("batal", cancel_cmd),
            CommandHandler("start", start),
            CallbackQueryHandler(cancel_cmd, pattern=r"^m:menu$"),
        ],
        per_message=False,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("id", whoami))
    app.add_handler(CommandHandler("riwayat", riwayat_cmd))
    app.add_handler(CommandHandler("batal", cancel_cmd))
    app.add_handler(conv)
    # tombol Batal di luar conversation (fallback global)
    app.add_handler(CallbackQueryHandler(cancel_cmd, pattern=r"^d:cancel$"))
    return app


def main() -> None:
    app = build_app()
    logger.info("Bot berjalan…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
