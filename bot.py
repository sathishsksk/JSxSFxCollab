"""
🎵 Music Downloader Bot — Combined + Quality Selection
   • JioSaavn  : song / album / playlist URLs  → 128 or 320 kbps
   • Spotify   : track / album / playlist / artist URLs → 128 or 320 kbps
   • YouTube   : direct URLs → 128 or 320 kbps
   • Search    : plain song name → YouTube search → 128 or 320 kbps
Koyeb-ready: /health on PORT (default 8080)
"""

import os
import asyncio
import logging
import json
from pathlib import Path

from aiohttp import web

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from telegram.constants import ParseMode, ChatAction
from telegram.error import BadRequest

from config import BOT_TOKEN, BOT_ID, PORT, DOWNLOAD_DIR, MAX_PLAYLIST_SONGS
from helpers.jiosaavn import (
    detect_jiosaavn,
    fetch_song,
    fetch_album,
    fetch_playlist,
    download_song,
)
from helpers.spotify_handler import (
    detect_spotify,
    is_youtube,
    get_track_info,
    get_album_info,
    get_playlist_info,
    get_artist_top_tracks,
    download_from_youtube,
)

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
log = logging.getLogger(__name__)
os.makedirs(DOWNLOAD_DIR, exist_ok=True)


# ── Helpers ───────────────────────────────────────────────────────────────────
def human_duration(secs: int) -> str:
    m, s = divmod(int(secs), 60)
    return f"{m}:{s:02d}"


def cleanup(path: str | None):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


def quality_keyboard(payload: str) -> InlineKeyboardMarkup:
    """
    payload is a JSON string: {"type": "jio"|"spotify"|"youtube"|"search",
                                "url": "...", "kind": "..."}
    We encode it as callback_data split cleanly.
    callback_data format:  q|128|<payload>   or   q|320|<payload>
    Telegram limit: 64 bytes per callback_data — we keep payload short.
    """
    data_128 = f"q|128|{payload}"
    data_320 = f"q|320|{payload}"
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🎵 128 kbps", callback_data=data_128),
        InlineKeyboardButton("🎶 320 kbps", callback_data=data_320),
    ]])


async def send_audio_file(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    filepath: str,
    meta: dict,
    quality: str,
):
    """Send an mp3 file as Telegram audio with metadata caption."""
    title    = meta.get("title", "Unknown")
    artist   = meta.get("artist", "") or "Unknown"
    album    = meta.get("album", "") or ""
    duration = int(meta.get("duration", 0))

    caption = (
        f"🎵 **{title}**\n"
        f"👤 {artist}\n"
        + (f"💿 {album}\n" if album else "")
        + f"⏱ {human_duration(duration)}\n"
        f"📻 Quality: **{quality} kbps**\n\n"
        f"via @{BOT_ID}"
    )

    await context.bot.send_chat_action(chat_id, ChatAction.UPLOAD_VOICE)

    with open(filepath, "rb") as f:
        await context.bot.send_audio(
            chat_id=chat_id,
            audio=InputFile(f, filename=f"{artist} - {title}.mp3"),
            caption=caption,
            parse_mode=ParseMode.MARKDOWN,
            title=title,
            performer=artist,
            duration=duration,
        )


# ── Download + Send logic (shared by all sources) ────────────────────────────
async def process_jiosaavn(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    reply_msg_id: int,
    url: str,
    kind: str,
    quality: str,
):
    status = await context.bot.send_message(chat_id, "⏳ Fetching from JioSaavn…")
    try:
        if kind == "song":
            songs = await fetch_song(url, quality)
        elif kind == "album":
            songs = await fetch_album(url, quality)
        else:
            songs = await fetch_playlist(url, quality)

        if not songs:
            await status.edit_text("❌ No songs found. The link may be invalid.")
            return

        total = min(len(songs), MAX_PLAYLIST_SONGS)
        await status.edit_text(f"📥 Downloading {total} song(s) at {quality} kbps…")

        for song in songs[:MAX_PLAYLIST_SONGS]:
            try:
                path = await download_song(song)
                await send_audio_file(context, chat_id, path, song, quality)
                cleanup(path)
                if total > 1:
                    await asyncio.sleep(1)
            except Exception as e:
                log.error(f"JioSaavn send error: {e}")
                await context.bot.send_message(
                    chat_id, f"⚠️ Skipped: {song.get('title','?')} — {e}"
                )

        await status.delete()

    except Exception as e:
        log.error(f"JioSaavn error: {e}")
        await status.edit_text(f"❌ Error: {e}")


async def process_spotify(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    url: str,
    kind: str,
    quality: str,
):
    status = await context.bot.send_message(chat_id, "⏳ Fetching from Spotify…")
    try:
        if kind == "track":
            tracks = get_track_info(url)
        elif kind == "album":
            tracks = get_album_info(url)
        elif kind == "playlist":
            tracks = get_playlist_info(url)
        elif kind == "artist":
            tracks = get_artist_top_tracks(url)
        else:
            tracks = []

        if not tracks:
            await status.edit_text(
                "❌ Could not fetch Spotify info.\n"
                "Check your SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET."
            )
            return

        total = min(len(tracks), MAX_PLAYLIST_SONGS)
        await status.edit_text(
            f"📥 Downloading {total} track(s) at {quality} kbps via YouTube…"
        )

        for track in tracks[:MAX_PLAYLIST_SONGS]:
            search_q = track.get("search") or f"{track['title']} {track['artist']}"
            try:
                path = await download_from_youtube(search_q, meta=track, quality=quality)
                if not path:
                    raise Exception("Download failed — yt-dlp returned no file")
                await send_audio_file(context, chat_id, path, track, quality)
                cleanup(path)
                if total > 1:
                    await asyncio.sleep(1.5)
            except Exception as e:
                log.error(f"Spotify track error {track.get('title')}: {e}")
                await context.bot.send_message(
                    chat_id, f"⚠️ Skipped: {track.get('title','?')} — {e}"
                )

        await status.delete()

    except Exception as e:
        log.error(f"Spotify error: {e}")
        await status.edit_text(f"❌ Error: {e}")


async def process_youtube(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    url: str,
    quality: str,
):
    status = await context.bot.send_message(chat_id, "⏳ Downloading from YouTube…")
    try:
        path = await download_from_youtube(url, quality=quality)
        if not path:
            await status.edit_text("❌ Download failed. Video may be unavailable or age-restricted.")
            return
        meta = {
            "title":    Path(path).stem.replace(f"_{quality}kbps", ""),
            "artist":   "",
            "album":    "",
            "duration": 0,
        }
        await send_audio_file(context, chat_id, path, meta, quality)
        cleanup(path)
        await status.delete()
    except Exception as e:
        log.error(f"YouTube error: {e}")
        await status.edit_text(f"❌ Error: {e}")


async def process_search(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    query: str,
    quality: str,
):
    status = await context.bot.send_message(
        chat_id, f"🔍 Searching: *{query}* at {quality} kbps…",
        parse_mode=ParseMode.MARKDOWN
    )
    try:
        path = await download_from_youtube(query, quality=quality)
        if not path:
            await status.edit_text("❌ No results found. Try a different search term.")
            return
        meta = {
            "title":    query,
            "artist":   "",
            "album":    "",
            "duration": 0,
        }
        await send_audio_file(context, chat_id, path, meta, quality)
        cleanup(path)
        await status.delete()
    except Exception as e:
        log.error(f"Search error: {e}")
        await status.edit_text(f"❌ Error: {e}")


# ── Bot command handlers ──────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🎵 *Music Downloader Bot*\n\n"
        "I can download music from:\n"
        "🟠 *JioSaavn* — song / album / playlist links\n"
        "🟢 *Spotify* — track / album / playlist / artist links\n"
        "🔴 *YouTube* — direct video links\n"
        "🔍 *Search* — just type any song name!\n\n"
        "After you send a link or song name, choose quality:\n"
        "• *128 kbps* — smaller file\n"
        "• *320 kbps* — best quality\n\n"
        "Send a link or song name to get started! 🎶"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)


# ── Message router ────────────────────────────────────────────────────────────
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text:
        return

    chat_id = update.effective_chat.id

    # Determine source type
    jio_kind = detect_jiosaavn(text)
    sp_kind  = detect_spotify(text)
    yt       = is_youtube(text)

    if jio_kind:
        src_type = "jio"
        kind     = jio_kind
    elif sp_kind:
        src_type = "spotify"
        kind     = sp_kind
    elif yt:
        src_type = "youtube"
        kind     = ""
    else:
        src_type = "search"
        kind     = ""

    # Build compact payload for callback_data
    # Telegram callback_data limit = 64 bytes — store URL in context instead
    payload_id = f"{update.message.message_id}"
    context.bot_data[payload_id] = {
        "type": src_type,
        "url":  text,
        "kind": kind,
    }

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "🎵 128 kbps", callback_data=f"q|128|{payload_id}"
        ),
        InlineKeyboardButton(
            "🎶 320 kbps", callback_data=f"q|320|{payload_id}"
        ),
    ]])

    source_label = {
        "jio":     "🟠 JioSaavn",
        "spotify": "🟢 Spotify",
        "youtube": "🔴 YouTube",
        "search":  "🔍 Search",
    }.get(src_type, "")

    await update.message.reply_text(
        f"{source_label} detected!\nChoose download quality:",
        reply_markup=keyboard,
    )


# ── Quality button callback ───────────────────────────────────────────────────
async def quality_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    parts = query.data.split("|", 2)
    if len(parts) != 3:
        await query.edit_message_text("❌ Invalid callback. Please try again.")
        return

    _, quality, payload_id = parts
    chat_id = update.effective_chat.id

    payload = context.bot_data.get(payload_id)
    if not payload:
        await query.edit_message_text("❌ Session expired. Please send the link again.")
        return

    src_type = payload["type"]
    url      = payload["url"]
    kind     = payload["kind"]

    # Remove quality keyboard
    try:
        await query.edit_message_text(
            f"✅ Starting download at *{quality} kbps*…",
            parse_mode=ParseMode.MARKDOWN
        )
    except BadRequest:
        pass

    # Route to correct processor
    if src_type == "jio":
        await process_jiosaavn(context, chat_id, query.message.message_id, url, kind, quality)
    elif src_type == "spotify":
        await process_spotify(context, chat_id, url, kind, quality)
    elif src_type == "youtube":
        await process_youtube(context, chat_id, url, quality)
    else:
        await process_search(context, chat_id, url, quality)

    # Clean up stored payload
    context.bot_data.pop(payload_id, None)


# ── Health server (Koyeb requirement) ────────────────────────────────────────
async def health(_):
    return web.Response(text="OK")


async def run_health_server():
    app = web.Application()
    app.router.add_get("/health", health)
    app.router.add_get("/",       health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    log.info(f"✅ Health server on :{PORT}")


# ── Entry point ───────────────────────────────────────────────────────────────
async def main():
    await run_health_server()

    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help",  help_cmd))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )
    application.add_handler(
        CallbackQueryHandler(quality_callback, pattern=r"^q\|")
    )

    log.info("🤖 Bot starting…")
    async with application:
        await application.start()
        await application.updater.start_polling(drop_pending_updates=True)
        log.info("✅ Bot is running!")
        await asyncio.Event().wait()   # keep alive
        await application.updater.stop()
        await application.stop()


if __name__ == "__main__":
    asyncio.run(main())
