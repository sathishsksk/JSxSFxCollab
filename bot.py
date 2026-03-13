"""
🎵 Music Downloader Bot

Sources:
  🟠 JioSaavn  — song / album / playlist URL
  🟢 Spotify   — track / album / playlist / artist URL  (no API keys needed)
  🔴 YouTube   — direct URL
  🔍 Search    — shows numbered results list like Deezer bot (JioSaavn + YouTube)

Search UI:
  User sends song name
  → Bot shows numbered results: 1. Artist – Song, 2. Artist – Song (Slowed) …
  → User taps one
  → Quality picker: 128 kbps / 320 kbps
  → Downloads with metadata + cover art + lyrics
"""

import os
import json
import asyncio
import logging
import unicodedata
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

from config import (
    BOT_TOKEN, BOT_ID, PORT, DOWNLOAD_DIR,
    MAX_PLAYLIST_SONGS, MAX_SEARCH_RESULTS,
)
from helpers.jiosaavn import (
    detect_jiosaavn,
    fetch_song, fetch_album, fetch_playlist,
    search_songs, download_and_encode,
)
from helpers.spotify_handler import (
    detect_spotify, is_youtube,
    spotify_track, spotify_album, spotify_playlist, spotify_artist,
    search_youtube, download_yt, get_lyrics,
)
from helpers.tagger import tag_mp3

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
log = logging.getLogger(__name__)
os.makedirs(DOWNLOAD_DIR, exist_ok=True)


# ── Helpers ───────────────────────────────────────────────────────────────────
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


# ── Download one song (any source) and send ───────────────────────────────────
async def _download_and_send(context, chat_id, song: dict, quality: str):
    """Download + tag + send a single song dict."""
    source = song.get("source", "youtube")

    if source == "jiosaavn":
        song["quality"] = quality
        path = await download_and_encode(song)
        lyrics = song.get("lyrics") or ""
    else:
        # Spotify / YouTube / Search — download via yt-dlp
        target = song.get("search") or f"{song.get('title','')} {song.get('artist','')}".strip()
        path   = await download_yt(target, meta=song, quality=quality)
        if not path:
            raise RuntimeError("yt-dlp returned no file")
        lyrics = await get_lyrics(song.get("title",""), song.get("artist",""))

    await tag_mp3(path, song, lyrics=lyrics)
    await send_audio(context, chat_id, path, song, quality)
    cleanup(path)


# ══════════════════════════════════════════════════════════════════════════════
# SEARCH RESULTS UI  (like Deezer bot in screenshot)
# ══════════════════════════════════════════════════════════════════════════════
async def show_search_results(context, chat_id, query: str, results: list[dict], msg_id: str):
    """
    Show numbered results list as inline keyboard.
    Each button: "N. Artist – Title  ⏱ dur"
    """
    if not results:
        await context.bot.send_message(chat_id, "❌ No results found. Try a different search.")
        return

    # Store results indexed by msg_id
    context.bot_data[f"sr:{msg_id}"] = results

    buttons = []
    for i, s in enumerate(results, 1):
        dur  = f"  ⏱{human_dur(s['duration'])}" if s.get("duration") else ""
        src  = "🟠" if s.get("source") == "jiosaavn" else "🔴"
        label = f"{i}. {s['artist']} – {s['title']}{dur} {src}"
        # Truncate to Telegram button limit (64 chars)
        if len(label) > 60:
            label = label[:57] + "…"
        buttons.append([InlineKeyboardButton(label, callback_data=f"sr|{msg_id}|{i-1}")])

    # Source filter row (informational — shows what sources are in results)
    sources = {s.get("source") for s in results}
    src_info = "  ".join(
        (["🟠 JioSaavn"] if "jiosaavn" in sources else []) +
        (["🔴 YouTube"]  if "youtube"  in sources else [])
    )

    buttons.append([InlineKeyboardButton("❌ Close", callback_data=f"sr|{msg_id}|close")])

    kbd = InlineKeyboardMarkup(buttons)
    await context.bot.send_message(
        chat_id,
        f"🔍 *Results for:* `{query}`\n_{src_info}_\n\nTap a song to download:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kbd,
    )


async def on_search_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle user tapping a search result → show quality picker."""
    query = update.callback_query
    await query.answer()

    parts = (query.data or "").split("|", 2)
    if len(parts) != 3:
        return
    _, msg_id, idx = parts

    if idx == "close":
        await query.message.delete()
        return

    results = context.bot_data.get(f"sr:{msg_id}")
    if not results:
        await query.edit_message_text("❌ Session expired. Search again.")
        return

    try:
        song = results[int(idx)]
    except (IndexError, ValueError):
        await query.edit_message_text("❌ Invalid selection.")
        return

    # Store the selected song for quality picker
    pick_key = f"pick:{msg_id}:{idx}"
    context.bot_data[pick_key] = song
    context.bot_data.pop(f"sr:{msg_id}", None)  # free results memory

    title  = song.get("title", "?")
    artist = song.get("artist", "")
    label  = f"{artist} – {title}" if artist else title

    kbd = InlineKeyboardMarkup([[
        InlineKeyboardButton("🎵 128 kbps", callback_data=f"q|128|{pick_key}"),
        InlineKeyboardButton("🎶 320 kbps", callback_data=f"q|320|{pick_key}"),
    ]])
    await query.edit_message_text(
        f"✅ *{label}*\n\nChoose quality:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kbd,
    )


# ══════════════════════════════════════════════════════════════════════════════
# PIPELINES
# ══════════════════════════════════════════════════════════════════════════════
async def run_jiosaavn(context, chat_id, url, kind, quality):
    status = await context.bot.send_message(chat_id, "⏳ Fetching from JioSaavn…")
    try:
        if kind == "song":       songs = await fetch_song(url, quality)
        elif kind == "album":    songs = await fetch_album(url, quality)
        elif kind == "playlist": songs = await fetch_playlist(url, quality)
        else:                    songs = await fetch_song(url, quality)

        if not songs:
            await status.edit_text("❌ JioSaavn returned no results. Link may be invalid.")
            return

        total = min(len(songs), MAX_PLAYLIST_SONGS)
        await status.edit_text(f"📥 Downloading {total} song(s) at {quality} kbps…")

        for song in songs[:MAX_PLAYLIST_SONGS]:
            try:
                await _download_and_send(context, chat_id, song, quality)
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


async def run_spotify(context, chat_id, url, kind, quality):
    status = await context.bot.send_message(chat_id, "⏳ Fetching from Spotify…")
    try:
        if kind == "track":       tracks, err = spotify_track(url)
        elif kind == "album":     tracks, err = spotify_album(url)
        elif kind == "playlist":  tracks, err = spotify_playlist(url)
        elif kind == "artist":    tracks, err = spotify_artist(url)
        else:                     tracks, err = [], "Unknown type"

        if err or not tracks:
            await status.edit_text(
                f"❌ Spotify error:\n`{err}`\n\n"
                "Note: No Spotify API keys needed — this uses yt-dlp internally.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        total = min(len(tracks), MAX_PLAYLIST_SONGS)
        await status.edit_text(f"📥 Downloading {total} track(s) via YouTube at {quality} kbps…")

        for track in tracks[:MAX_PLAYLIST_SONGS]:
            try:
                await _download_and_send(context, chat_id, track, quality)
                if total > 1:
                    await asyncio.sleep(1.5)
            except Exception as e:
                log.error(f"Spotify track error: {e}")
                await context.bot.send_message(
                    chat_id,
                    f"⚠️ Skipped *{track.get('title','?')}*\n`{type(e).__name__}: {e}`",
                    parse_mode=ParseMode.MARKDOWN,
                )
        await status.delete()
    except Exception as e:
        log.error(f"Spotify pipeline error: {e}")
        await status.edit_text(f"❌ Error: `{type(e).__name__}: {e}`", parse_mode=ParseMode.MARKDOWN)


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


async def run_search(context, chat_id, query: str, msg_id: str):
    """
    Show search results list (Deezer-style).
    Searches JioSaavn first, then YouTube to fill remaining slots.
    """
    status = await context.bot.send_message(
        chat_id, f"🔍 Searching *{query}*…", parse_mode=ParseMode.MARKDOWN
    )

    results = []

    # 1️⃣ JioSaavn search
    try:
        jio_results = await search_songs(query, limit=MAX_SEARCH_RESULTS)
        results.extend(jio_results)
        log.info(f"JioSaavn search '{query}': {len(jio_results)} results")
    except Exception as e:
        log.warning(f"JioSaavn search failed: {e}")

    # 2️⃣ YouTube search — fill remaining slots
    yt_limit = max(0, MAX_SEARCH_RESULTS - len(results))
    if yt_limit > 0:
        try:
            # ASCII fallback for Tamil/regional scripts
            yt_query = query
            ascii_q  = unicodedata.normalize("NFKD", query).encode("ascii", "ignore").decode().strip()
            if ascii_q and ascii_q != query:
                yt_query = ascii_q
            yt_results = await search_youtube(yt_query, limit=yt_limit)
            results.extend(yt_results)
            log.info(f"YouTube search '{yt_query}': {len(yt_results)} results")
        except Exception as e:
            log.warning(f"YouTube search failed: {e}")

    await status.delete()

    if not results:
        await context.bot.send_message(
            chat_id,
            "❌ No results found.\nTry sending a direct JioSaavn, Spotify or YouTube link.",
        )
        return

    await show_search_results(context, chat_id, query, results, msg_id)


# ══════════════════════════════════════════════════════════════════════════════
# HANDLERS
# ══════════════════════════════════════════════════════════════════════════════
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎵 *Music Downloader Bot*\n\n"
        "Send me a link or song name:\n\n"
        "🟠 *JioSaavn* — song / album / playlist URL\n"
        "🟢 *Spotify* — track / album / playlist / artist URL\n"
        "🔴 *YouTube* — direct URL\n"
        "🔍 *Song name* — shows search results to pick from\n\n"
        "Each download includes cover art, lyrics & metadata 🎶",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, context)


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text    = (update.message.text or "").strip()
    if not text:
        return
    chat_id = update.effective_chat.id
    msg_id  = str(update.message.message_id)

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
        # Plain text — go straight to search results picker
        await run_search(context, chat_id, text, msg_id)
        return

    # URL detected — ask quality first
    context.bot_data[msg_id] = {"type": src_type, "url": text, "kind": kind}
    kbd = InlineKeyboardMarkup([[
        InlineKeyboardButton("🎵 128 kbps", callback_data=f"q|128|{msg_id}"),
        InlineKeyboardButton("🎶 320 kbps", callback_data=f"q|320|{msg_id}"),
    ]])
    await update.message.reply_text(
        f"{label} detected!\nChoose download quality:",
        reply_markup=kbd,
    )


async def on_quality(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Quality button handler — works for both URL downloads and search picks."""
    cb = update.callback_query
    await cb.answer()

    parts = (cb.data or "").split("|", 2)
    if len(parts) != 3:
        await cb.edit_message_text("❌ Invalid. Please send the link again.")
        return

    _, quality, key = parts
    chat_id = update.effective_chat.id

    payload = context.bot_data.pop(key, None)
    if not payload:
        await cb.edit_message_text("❌ Session expired. Please send the link again.")
        return

    try:
        await cb.edit_message_text(
            f"✅ Starting download at *{quality} kbps*…",
            parse_mode=ParseMode.MARKDOWN,
        )
    except BadRequest:
        pass

    # payload is either a URL dict or a song dict (from search pick)
    if isinstance(payload, dict) and "type" in payload:
        # URL download
        src_type = payload["type"]
        url      = payload["url"]
        kind     = payload["kind"]
        if src_type == "jio":       await run_jiosaavn(context, chat_id, url, kind, quality)
        elif src_type == "spotify": await run_spotify(context, chat_id, url, kind, quality)
        elif src_type == "youtube": await run_youtube(context, chat_id, url, quality)
    else:
        # Search pick — payload is a song dict
        song = payload
        status = await context.bot.send_message(
            chat_id, f"📥 Downloading *{song.get('title','?')}* at {quality} kbps…",
            parse_mode=ParseMode.MARKDOWN,
        )
        try:
            await _download_and_send(context, chat_id, song, quality)
            await status.delete()
        except Exception as e:
            log.error(f"Search-pick download error: {e}")
            await status.edit_text(f"❌ Error: `{type(e).__name__}: {e}`",
                                   parse_mode=ParseMode.MARKDOWN)


# ── Health server ─────────────────────────────────────────────────────────────
async def _health(_):
    return web.Response(text="OK")


async def start_health():
    app = web.Application()
    app.router.add_get("/health", _health)
    app.router.add_get("/",       _health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    log.info(f"✅ Health server on :{PORT}")


# ── Main ──────────────────────────────────────────────────────────────────────
async def main():
    await start_health()

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help",  cmd_help))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    app.add_handler(CallbackQueryHandler(on_search_pick, pattern=r"^sr\|"))
    app.add_handler(CallbackQueryHandler(on_quality,     pattern=r"^q\|"))

    log.info("🤖 Bot starting…")
    async with app:
        await app.start()
        await app.updater.start_polling(
            drop_pending_updates=True,
            allowed_updates=["message", "callback_query"],
        )
        log.info("✅ Bot running!")
        await asyncio.Event().wait()
        await app.updater.stop()
        await app.stop()


if __name__ == "__main__":
    asyncio.run(main())
