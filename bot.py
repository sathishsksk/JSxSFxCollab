"""
🎵 Music Downloader Bot — JSxSFxCollab

Sources:
  🟠 JioSaavn  — song / album / playlist URL
  🟢 Spotify   — track / album / playlist / artist URL  (web scraper, NO API keys)
  🔴 YouTube   — direct URL
  🔍 Search    — numbered results with JioSaavn / YouTube source filter buttons
"""

import os
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
    spotify_scrape,
    download_spotify_track,
    search_youtube, download_yt, get_lyrics,
)
from helpers.tagger import tag_mp3

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
log = logging.getLogger(__name__)
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

SRC_JIO = "jiosaavn"
SRC_YT  = "youtube"
SRC_EMO = {SRC_JIO: "🟠", SRC_YT: "🔴"}
SRC_LBL = {SRC_JIO: "JioSaavn", SRC_YT: "YouTube"}


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


# ── Per-source downloaders ────────────────────────────────────────────────────
async def _dl_send_jio(context, chat_id, song: dict, quality: str):
    song["quality"] = quality
    path   = await download_and_encode(song)
    lyrics = song.get("lyrics") or ""
    await tag_mp3(path, song, lyrics=lyrics)
    await send_audio(context, chat_id, path, song, quality)
    cleanup(path)


async def _dl_send_yt(context, chat_id, song: dict, quality: str):
    target = song.get("search") or f"{song.get('title','')} {song.get('artist','')}".strip()
    path   = await download_yt(target, meta=song, quality=quality)
    if not path:
        raise RuntimeError("yt-dlp returned no file — video unavailable")
    lyrics = await get_lyrics(song.get("title",""), song.get("artist",""))
    await tag_mp3(path, song, lyrics=lyrics)
    await send_audio(context, chat_id, path, song, quality)
    cleanup(path)


async def _dl_send_spotify(context, chat_id, song: dict, quality: str):
    """
    Download a Spotify track and send with full metadata.
    download_spotify_track returns (path, enriched_meta) where enriched_meta
    has JioSaavn artist/album/lyrics merged with Spotify's high-res image.
    """
    path, enriched = await download_spotify_track(song, quality=quality)
    lyrics = enriched.get("lyrics") or await get_lyrics(
        enriched.get("title", ""), enriched.get("artist", "")
    )
    await tag_mp3(path, enriched, lyrics=lyrics)
    await send_audio(context, chat_id, path, enriched, quality)
    cleanup(path)


# ══════════════════════════════════════════════════════════════════════════════
#  SEARCH RESULTS UI — source filter buttons
# ══════════════════════════════════════════════════════════════════════════════
def _build_kbd(results: list[dict], sid: str, active: set) -> InlineKeyboardMarkup:
    buttons = []
    for i, s in enumerate(results):
        src    = s.get("source", SRC_YT)
        emoji  = SRC_EMO.get(src, "🎵")
        dur    = f" ⏱{human_dur(s['duration'])}" if s.get("duration") else ""
        artist = s.get("artist") or ""
        title  = s.get("title") or "Unknown"
        label  = f"{i+1}. {artist} – {title}{dur} {emoji}" if artist else f"{i+1}. {title}{dur} {emoji}"
        if len(label) > 62:
            label = label[:59] + "…"
        buttons.append([InlineKeyboardButton(label, callback_data=f"pick|{sid}|{i}")])

    filter_row = []
    for src in [SRC_JIO, SRC_YT]:
        tick  = "✅" if src in active else "☑️"
        label = f"{SRC_EMO[src]} {SRC_LBL[src]} {tick}"
        filter_row.append(InlineKeyboardButton(label, callback_data=f"srcf|{sid}|{src}"))
    buttons.append(filter_row)
    buttons.append([InlineKeyboardButton("❌ Close", callback_data=f"pick|{sid}|close")])
    return InlineKeyboardMarkup(buttons)


async def _fetch_source(query: str, source: str, limit: int) -> list[dict]:
    try:
        if source == SRC_JIO:
            return await search_songs(query, limit=limit)
        elif source == SRC_YT:
            ascii_q = unicodedata.normalize("NFKD", query).encode("ascii","ignore").decode().strip()
            q = ascii_q if ascii_q and ascii_q != query else query
            return await search_youtube(q, limit=limit)
    except Exception as e:
        log.warning(f"Fetch source {source} error: {e}")
    return []


async def _render(context, chat_id, query, sid, msg_id=None):
    sess   = context.bot_data.get(f"s:{sid}", {})
    active = set(sess.get("active", [SRC_JIO, SRC_YT]))
    cache  = sess.get("cache", {})

    for src in active:
        if src not in cache:
            cache[src] = await _fetch_source(query, src, MAX_SEARCH_RESULTS)
            sess["cache"] = cache
            context.bot_data[f"s:{sid}"] = sess

    visible = []
    for src in [SRC_JIO, SRC_YT]:
        if src in active:
            visible.extend(cache.get(src, []))
    visible = visible[:MAX_SEARCH_RESULTS]

    sess["visible"] = visible
    context.bot_data[f"s:{sid}"] = sess

    src_line = "  ".join(f"{SRC_EMO[s]} {SRC_LBL[s]}" for s in [SRC_JIO, SRC_YT] if s in active)
    text = (
        f"🔍 *Results for:* `{query}`\n_{src_line}_\n\nTap a song to download:"
        if visible else
        f"🔍 *{query}*\n\n❌ No results from selected sources."
    )
    kbd = _build_kbd(visible, sid, active)

    if msg_id:
        try:
            m = await context.bot.edit_message_text(
                chat_id=chat_id, message_id=msg_id,
                text=text, parse_mode=ParseMode.MARKDOWN, reply_markup=kbd,
            )
            return m.message_id
        except BadRequest:
            pass

    m = await context.bot.send_message(chat_id, text, parse_mode=ParseMode.MARKDOWN, reply_markup=kbd)
    return m.message_id


async def run_search(context, chat_id, query, sid):
    status = await context.bot.send_message(chat_id, f"🔍 Searching *{query}*…", parse_mode=ParseMode.MARKDOWN)
    context.bot_data[f"s:{sid}"] = {"query": query, "active": [SRC_JIO, SRC_YT], "cache": {}, "visible": []}
    await status.delete()
    await _render(context, chat_id, query, sid)


async def on_source_filter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cb = update.callback_query
    await cb.answer()
    _, sid, src = (cb.data or "").split("|", 2)
    sess = context.bot_data.get(f"s:{sid}")
    if not sess:
        await cb.edit_message_text("❌ Session expired. Search again.")
        return
    active = set(sess.get("active", [SRC_JIO, SRC_YT]))
    active = (active - {src}) if src in active else (active | {src})
    if not active:
        active = {src}
    sess["active"] = list(active)
    context.bot_data[f"s:{sid}"] = sess
    await _render(context, update.effective_chat.id, sess["query"], sid, msg_id=cb.message.message_id)


async def on_search_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cb = update.callback_query
    await cb.answer()
    parts = (cb.data or "").split("|", 2)
    if len(parts) != 3:
        return
    _, sid, idx = parts

    if idx == "close":
        context.bot_data.pop(f"s:{sid}", None)
        try: await cb.message.delete()
        except Exception: pass
        return

    sess = context.bot_data.get(f"s:{sid}")
    if not sess:
        await cb.edit_message_text("❌ Session expired. Search again.")
        return

    visible = sess.get("visible") or []
    try:
        song = visible[int(idx)]
    except (IndexError, ValueError):
        return

    pick_key = f"p:{sid}:{idx}"
    context.bot_data[pick_key] = song
    context.bot_data.pop(f"s:{sid}", None)

    title  = song.get("title","?")
    artist = song.get("artist","")
    emoji  = SRC_EMO.get(song.get("source", SRC_YT), "🎵")
    label  = f"{artist} – {title}" if artist else title

    kbd = InlineKeyboardMarkup([[
        InlineKeyboardButton("🎵 128 kbps", callback_data=f"q|128|{pick_key}"),
        InlineKeyboardButton("🎶 320 kbps", callback_data=f"q|320|{pick_key}"),
    ]])
    await cb.edit_message_text(
        f"{emoji} *{label}*\n\nChoose quality:",
        parse_mode=ParseMode.MARKDOWN, reply_markup=kbd,
    )


# ══════════════════════════════════════════════════════════════════════════════
#  PIPELINES
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
                await _dl_send_jio(context, chat_id, song, quality)
                if total > 1: await asyncio.sleep(1)
            except Exception as e:
                log.error(f"JioSaavn song error: {e}")
                await context.bot.send_message(
                    chat_id,
                    f"⚠️ Skipped *{song.get('title','?')}*\n`{type(e).__name__}: {e}`",
                    parse_mode=ParseMode.MARKDOWN,
                )
        await status.delete()
    except Exception as e:
        log.error(f"JioSaavn pipeline: {e}")
        await status.edit_text(f"❌ `{type(e).__name__}: {e}`", parse_mode=ParseMode.MARKDOWN)


async def run_spotify(context, chat_id, url, kind, quality):
    """
    Spotify pipeline — NO API KEYS NEEDED.
    Scrapes Spotify web page for metadata, downloads audio via YouTube Music match.
    """
    status = await context.bot.send_message(
        chat_id,
        "⏳ Reading Spotify metadata…",
        parse_mode=ParseMode.MARKDOWN,
    )
    try:
        tracks, err = await spotify_scrape(url, kind)

        if err or not tracks:
            await status.edit_text(
                f"❌ *Spotify error:*\n`{err or 'No tracks found'}`",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        total = min(len(tracks), MAX_PLAYLIST_SONGS)
        await status.edit_text(
            f"📥 Downloading {total} track(s) at {quality} kbps…\n"
            "_Finding YouTube Music matches…_",
            parse_mode=ParseMode.MARKDOWN,
        )

        for track in tracks[:MAX_PLAYLIST_SONGS]:
            try:
                await _dl_send_spotify(context, chat_id, track, quality)
                if total > 1: await asyncio.sleep(1.5)
            except Exception as e:
                log.error(f"Spotify track error '{track.get('title')}': {e}")
                await context.bot.send_message(
                    chat_id,
                    f"⚠️ Skipped *{track.get('title','?')}*\n`{type(e).__name__}: {e}`",
                    parse_mode=ParseMode.MARKDOWN,
                )

        await status.delete()
    except Exception as e:
        log.error(f"Spotify pipeline: {e}")
        await status.edit_text(f"❌ `{type(e).__name__}: {e}`", parse_mode=ParseMode.MARKDOWN)


async def run_youtube(context, chat_id, url, quality):
    status = await context.bot.send_message(chat_id, "⏳ Downloading from YouTube…")
    try:
        path = await download_yt(url, quality=quality)
        if not path:
            await status.edit_text("❌ Download failed. Video may be unavailable.")
            return
        stem = Path(path).stem.replace(f"_{quality}kbps", "")
        meta = {"title": stem, "artist": "", "album": "", "duration": 0}
        await tag_mp3(path, meta)
        await send_audio(context, chat_id, path, meta, quality)
        cleanup(path)
        await status.delete()
    except Exception as e:
        log.error(f"YouTube pipeline: {e}")
        await status.edit_text(f"❌ `{type(e).__name__}: {e}`", parse_mode=ParseMode.MARKDOWN)


# ══════════════════════════════════════════════════════════════════════════════
#  TELEGRAM HANDLERS
# ══════════════════════════════════════════════════════════════════════════════
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎵 *Music Downloader Bot*\n\n"
        "Send me a link or song name:\n\n"
        "🟠 *JioSaavn* — song / album / playlist URL\n"
        "🟢 *Spotify* — track / album / playlist / artist URL\n"
        "🔴 *YouTube* — direct URL\n"
        "🔍 *Song name* — shows search results with source filter\n\n"
        "Each file includes cover art, lyrics & metadata 🎶",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, context)


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text    = (update.message.text or "").strip()
    if not text:
        return
    chat_id = update.effective_chat.id
    mid     = str(update.message.message_id)

    jio_kind = detect_jiosaavn(text)
    sp_kind  = detect_spotify(text)
    yt       = is_youtube(text)

    if jio_kind:
        context.bot_data[mid] = {"type": "jio", "url": text, "kind": jio_kind}
        kbd = InlineKeyboardMarkup([[
            InlineKeyboardButton("🎵 128 kbps", callback_data=f"q|128|{mid}"),
            InlineKeyboardButton("🎶 320 kbps", callback_data=f"q|320|{mid}"),
        ]])
        await update.message.reply_text("🟠 JioSaavn detected!\nChoose quality:", reply_markup=kbd)

    elif sp_kind:
        context.bot_data[mid] = {"type": "spotify", "url": text, "kind": sp_kind}
        kbd = InlineKeyboardMarkup([[
            InlineKeyboardButton("🎵 128 kbps", callback_data=f"q|128|{mid}"),
            InlineKeyboardButton("🎶 320 kbps", callback_data=f"q|320|{mid}"),
        ]])
        await update.message.reply_text("🟢 Spotify detected!\nChoose quality:", reply_markup=kbd)

    elif yt:
        context.bot_data[mid] = {"type": "youtube", "url": text, "kind": ""}
        kbd = InlineKeyboardMarkup([[
            InlineKeyboardButton("🎵 128 kbps", callback_data=f"q|128|{mid}"),
            InlineKeyboardButton("🎶 320 kbps", callback_data=f"q|320|{mid}"),
        ]])
        await update.message.reply_text("🔴 YouTube detected!\nChoose quality:", reply_markup=kbd)

    else:
        await run_search(context, chat_id, text, mid)


async def on_quality(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cb = update.callback_query
    await cb.answer()
    parts = (cb.data or "").split("|", 2)
    if len(parts) != 3:
        return
    _, quality, key = parts
    chat_id = update.effective_chat.id

    payload = context.bot_data.pop(key, None)
    if not payload:
        await cb.edit_message_text("❌ Session expired. Please send the link again.")
        return

    try:
        await cb.edit_message_text(
            f"✅ Starting at *{quality} kbps*…", parse_mode=ParseMode.MARKDOWN
        )
    except BadRequest:
        pass

    if isinstance(payload, dict) and "type" in payload:
        t = payload["type"]
        if t == "jio":       await run_jiosaavn(context, chat_id, payload["url"], payload["kind"], quality)
        elif t == "spotify": await run_spotify(context, chat_id, payload["url"], payload["kind"], quality)
        elif t == "youtube": await run_youtube(context, chat_id, payload["url"], quality)
    else:
        # Search-pick
        song   = payload
        src    = song.get("source", SRC_YT)
        status = await context.bot.send_message(
            chat_id,
            f"📥 Downloading *{song.get('title','?')}* at {quality} kbps…",
            parse_mode=ParseMode.MARKDOWN,
        )
        try:
            if src == SRC_JIO:
                await _dl_send_jio(context, chat_id, song, quality)
            else:
                await _dl_send_yt(context, chat_id, song, quality)
            await status.delete()
        except Exception as e:
            log.error(f"Search-pick download error: {e}")
            await status.edit_text(f"❌ `{type(e).__name__}: {e}`", parse_mode=ParseMode.MARKDOWN)


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
    app.add_handler(CallbackQueryHandler(on_source_filter, pattern=r"^srcf\|"))
    app.add_handler(CallbackQueryHandler(on_search_pick,   pattern=r"^pick\|"))
    app.add_handler(CallbackQueryHandler(on_quality,        pattern=r"^q\|"))

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
