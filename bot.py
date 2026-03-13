"""
🎵 Music Downloader Bot — Combined & Koyeb Ready
Sources: JioSaavn | Spotify (via yt-dlp) | YouTube | Search
"""

import os
import asyncio
import logging
from pathlib import Path

from aiohttp import web

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile,
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters,
)
from telegram.constants import ParseMode, ChatAction
from telegram.error import BadRequest

from config import BOT_TOKEN, BOT_ID, PORT, DOWNLOAD_DIR, MAX_PLAYLIST_SONGS

from helpers.jiosaavn import (
    detect_jiosaavn,
    fetch_song, fetch_album, fetch_playlist, search_song,
    download_and_encode,
)
from helpers.spotify import (
    detect_spotify, is_youtube,
    spotify_track, spotify_album, spotify_playlist, spotify_artist,
    download_yt, get_lyrics,
)
from helpers.tagger import tag_mp3

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
log = logging.getLogger(__name__)
os.makedirs(DOWNLOAD_DIR, exist_ok=True)


# ── Utilities ─────────────────────────────────────────────────────────────────
def human_dur(secs: int) -> str:
    m, s = divmod(max(int(secs), 0), 60)
    return f"{m}:{s:02d}"


def cleanup(*paths):
    for p in paths:
        try:
            if p and os.path.exists(p):
                os.remove(p)
        except OSError:
            pass


async def send_audio(context, chat_id, filepath, meta, quality):
    title    = meta.get("title") or "Unknown"
    artist   = meta.get("artist") or ""
    album    = meta.get("album") or ""
    duration = int(meta.get("duration") or 0)
    caption  = (
        f"🎵 *{title}*\n"
        + (f"👤 {artist}\n" if artist else "")
        + (f"💿 {album}\n"  if album  else "")
        + (f"⏱ {human_dur(duration)}\n" if duration else "")
        + f"📻 *{quality} kbps*\n\nvia @{BOT_ID}"
    )
    await context.bot.send_chat_action(chat_id, ChatAction.UPLOAD_VOICE)
    fname = f"{artist} - {title}.mp3" if artist else f"{title}.mp3"
    with open(filepath, "rb") as f:
        await context.bot.send_audio(
            chat_id=chat_id,
            audio=InputFile(f, filename=fname),
            caption=caption,
            parse_mode=ParseMode.MARKDOWN,
            title=title,
            performer=artist,
            duration=duration,
        )


# ── JioSaavn pipeline ─────────────────────────────────────────────────────────
async def run_jiosaavn(context, chat_id, url, kind, quality):
    status = await context.bot.send_message(chat_id, "⏳ Fetching from JioSaavn…")
    try:
        if kind == "song":        songs = await fetch_song(url, quality)
        elif kind == "album":     songs = await fetch_album(url, quality)
        elif kind == "playlist":  songs = await fetch_playlist(url, quality)
        else:                     songs = await fetch_song(url, quality)

        if not songs:
            await status.edit_text("❌ JioSaavn API returned no results.\nThe link may be region-locked or invalid.")
            return

        total = min(len(songs), MAX_PLAYLIST_SONGS)
        await status.edit_text(f"📥 Downloading {total} song(s) at {quality} kbps…")

        for song in songs[:MAX_PLAYLIST_SONGS]:
            try:
                path = await download_and_encode(song)
                await tag_mp3(path, song, lyrics=song.get("lyrics", ""))
                await send_audio(context, chat_id, path, song, quality)
                cleanup(path)
                if total > 1:
                    await asyncio.sleep(1)
            except Exception as e:
                log.error(f"JioSaavn song error: {e}")
                await context.bot.send_message(
                    chat_id,
                    f"⚠️ Skipped *{song.get('title','?')}*\n`{type(e).__name__}: {e}`",
                    parse_mode=ParseMode.MARKDOWN,
                )
        await status.delete()
    except Exception as e:
        log.error(f"JioSaavn pipeline error: {e}")
        await status.edit_text(f"❌ Error: `{type(e).__name__}: {e}`", parse_mode=ParseMode.MARKDOWN)


# ── Spotify pipeline (via yt-dlp — no API keys needed) ───────────────────────
async def run_spotify(context, chat_id, url, kind, quality):
    status = await context.bot.send_message(chat_id, "⏳ Fetching from Spotify…")
    try:
        if kind == "track":       tracks, err = spotify_track(url)
        elif kind == "album":     tracks, err = spotify_album(url)
        elif kind == "playlist":  tracks, err = spotify_playlist(url)
        elif kind == "artist":    tracks, err = spotify_artist(url)
        else:                     tracks, err = [], "Unknown Spotify type"

        if err or not tracks:
            await status.edit_text(
                f"❌ Could not fetch Spotify info:\n`{err}`",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        total = min(len(tracks), MAX_PLAYLIST_SONGS)
        await status.edit_text(f"📥 Downloading {total} track(s) via YouTube at {quality} kbps…")

        for track in tracks[:MAX_PLAYLIST_SONGS]:
            query = track.get("search") or f"{track['title']} {track['artist']}"
            try:
                path = await download_yt(query, meta=track, quality=quality)
                if not path:
                    raise RuntimeError("yt-dlp returned no file")
                lyrics = await get_lyrics(track.get("title", ""), track.get("artist", ""))
                await tag_mp3(path, track, lyrics=lyrics)
                await send_audio(context, chat_id, path, track, quality)
                cleanup(path)
                if total > 1:
                    await asyncio.sleep(1.5)
            except Exception as e:
                log.error(f"Spotify track error '{track.get('title')}': {e}")
                await context.bot.send_message(
                    chat_id,
                    f"⚠️ Skipped *{track.get('title','?')}*\n`{type(e).__name__}: {e}`",
                    parse_mode=ParseMode.MARKDOWN,
                )
        await status.delete()
    except Exception as e:
        log.error(f"Spotify pipeline error: {e}")
        await status.edit_text(f"❌ Error: `{type(e).__name__}: {e}`", parse_mode=ParseMode.MARKDOWN)


# ── YouTube pipeline ──────────────────────────────────────────────────────────
async def run_youtube(context, chat_id, url, quality):
    status = await context.bot.send_message(chat_id, "⏳ Downloading from YouTube…")
    try:
        path = await download_yt(url, quality=quality)
        if not path:
            await status.edit_text("❌ Download failed. Video may be unavailable or region-locked.")
            return
        stem = Path(path).stem.replace(f"_{quality}kbps", "")
        meta = {"title": stem, "artist": "", "album": "", "duration": 0}
        await tag_mp3(path, meta)
        await send_audio(context, chat_id, path, meta, quality)
        cleanup(path)
        await status.delete()
    except Exception as e:
        log.error(f"YouTube pipeline error: {e}")
        await status.edit_text(f"❌ Error: `{type(e).__name__}: {e}`", parse_mode=ParseMode.MARKDOWN)


# ── Search pipeline ───────────────────────────────────────────────────────────
async def run_search(context, chat_id, query, quality):
    status = await context.bot.send_message(
        chat_id, f"🔍 Searching *{query}*…", parse_mode=ParseMode.MARKDOWN
    )
    try:
        # Strategy 1: JioSaavn search (best for Indian/Tamil songs)
        songs = await search_song(query, quality)
        if songs:
            song = songs[0]
            await status.edit_text(
                f"✅ Found on JioSaavn: *{song['title']}*\n📥 Downloading…",
                parse_mode=ParseMode.MARKDOWN
            )
            try:
                path = await download_and_encode(song)
                await tag_mp3(path, song, lyrics=song.get("lyrics", ""))
                await send_audio(context, chat_id, path, song, quality)
                cleanup(path)
                await status.delete()
                return
            except Exception as e:
                log.warning(f"JioSaavn fallback failed: {e} — trying YouTube…")

        # Strategy 2: YouTube
        await status.edit_text("🔍 Searching on YouTube…")
        path = await download_yt(query, quality=quality)

        # Strategy 3: YouTube + "audio"
        if not path:
            path = await download_yt(f"{query} audio", quality=quality)

        # Strategy 4: ASCII fallback for Tamil/regional
        if not path:
            import unicodedata
            ascii_q = unicodedata.normalize("NFKD", query).encode("ascii", "ignore").decode().strip()
            if ascii_q and ascii_q != query:
                log.info(f"ASCII fallback: {ascii_q}")
                path = await download_yt(ascii_q, quality=quality)

        if not path:
            await status.edit_text(
                "❌ No results found anywhere.\n"
                "Try a direct JioSaavn, Spotify, or YouTube link."
            )
            return

        meta = {"title": query, "artist": "", "album": "", "duration": 0}
        await tag_mp3(path, meta)
        await send_audio(context, chat_id, path, meta, quality)
        cleanup(path)
        await status.delete()
    except Exception as e:
        log.error(f"Search pipeline error: {e}")
        await status.edit_text(f"❌ Error: `{type(e).__name__}: {e}`", parse_mode=ParseMode.MARKDOWN)


# ── Commands ──────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎵 *Music Downloader Bot*\n\n"
        "Send me a link or song name:\n"
        "🟠 JioSaavn — song / album / playlist\n"
        "🟢 Spotify — track / album / playlist / artist\n"
        "🔴 YouTube — direct URL\n"
        "🔍 Any song name\n\n"
        "Choose *128 kbps* or *320 kbps* quality.\n"
        "Every file includes cover art, lyrics & metadata 🎶",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, context)


# ── Message router ────────────────────────────────────────────────────────────
async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text:
        return

    chat_id  = update.effective_chat.id
    jio_kind = detect_jiosaavn(text)
    sp_kind  = detect_spotify(text)
    yt       = is_youtube(text)

    if jio_kind:
        src_type, kind, label = "jio",     jio_kind, "🟠 JioSaavn"
    elif sp_kind:
        src_type, kind, label = "spotify", sp_kind,  "🟢 Spotify"
    elif yt:
        src_type, kind, label = "youtube", "",        "🔴 YouTube"
    else:
        src_type, kind, label = "search",  "",        "🔍 Search"

    key = str(update.message.message_id)
    context.bot_data[key] = {"type": src_type, "url": text, "kind": kind}

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🎵 128 kbps", callback_data=f"q|128|{key}"),
        InlineKeyboardButton("🎶 320 kbps", callback_data=f"q|320|{key}"),
    ]])
    await update.message.reply_text(
        f"{label} detected!\nChoose download quality:",
        reply_markup=keyboard,
    )


# ── Quality callback ──────────────────────────────────────────────────────────
async def on_quality(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    parts = (query.data or "").split("|", 2)
    if len(parts) != 3:
        await query.edit_message_text("❌ Invalid. Please send the link again.")
        return

    _, quality, key = parts
    chat_id = update.effective_chat.id

    payload = context.bot_data.pop(key, None)
    if not payload:
        await query.edit_message_text("❌ Session expired. Please send the link again.")
        return

    src_type, url, kind = payload["type"], payload["url"], payload["kind"]

    try:
        await query.edit_message_text(
            f"✅ Starting at *{quality} kbps*…", parse_mode=ParseMode.MARKDOWN
        )
    except BadRequest:
        pass

    if src_type == "jio":         await run_jiosaavn(context, chat_id, url, kind, quality)
    elif src_type == "spotify":   await run_spotify(context, chat_id, url, kind, quality)
    elif src_type == "youtube":   await run_youtube(context, chat_id, url, quality)
    else:                         await run_search(context, chat_id, url, quality)


# ── Health server ─────────────────────────────────────────────────────────────
async def _health(_):
    return web.Response(text="OK")


async def start_health_server():
    app = web.Application()
    app.router.add_get("/health", _health)
    app.router.add_get("/",       _health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    log.info(f"✅ Health server on :{PORT}")


# ── Main ──────────────────────────────────────────────────────────────────────
async def main():
    await start_health_server()

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help",  cmd_help))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    app.add_handler(CallbackQueryHandler(on_quality, pattern=r"^q\|"))

    log.info("🤖 Bot starting…")
    async with app:
        await app.start()
        await app.updater.start_polling(
            drop_pending_updates=True,
            allowed_updates=["message", "callback_query"],
        )
        log.info("✅ Bot is running!")
        await asyncio.Event().wait()
        await app.updater.stop()
        await app.stop()


if __name__ == "__main__":
    asyncio.run(main())
