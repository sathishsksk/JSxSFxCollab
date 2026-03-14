"""
🎵 JSxSFxCollab Music Bot
"""

import os
import asyncio
import logging
import unicodedata
from pathlib import Path
from math import ceil

import aiohttp
from aiohttp import web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters,
)
from telegram.constants import ParseMode, ChatAction
from telegram.error import BadRequest

from config import BOT_TOKEN, BOT_ID, PORT, DOWNLOAD_DIR, MAX_PLAYLIST_SONGS, MAX_SEARCH_RESULTS
from helpers.jiosaavn import (
    detect_jiosaavn, fetch_song, fetch_album, fetch_playlist,
    search_songs, search_albums, search_artists, download_and_encode,
)
from helpers.spotify_handler import (
    detect_spotify, is_youtube,
    spotify_scrape, download_spotify_track,
    search_youtube, download_yt, get_lyrics,
)
from helpers.tagger import tag_mp3

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")
log = logging.getLogger(__name__)
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

PAGE_SIZE   = 6
SRC_JIO     = "jiosaavn"
SRC_YT      = "youtube"
SRC_LABELS  = {SRC_JIO: "JioSaavn", SRC_YT: "YouTube"}
SRC_ICONS   = {SRC_JIO: "🎵", SRC_YT: "▶️"}
JIO_TYPES   = {"songs": "Songs", "albums": "Albums", "artists": "Artists"}
JIO_ICONS   = {"songs": "🎶", "albums": "💿", "artists": "🎤"}

# Suggested related search terms appended to query for more variety
def _suggest_terms(query: str) -> list[str]:
    """Return related search suggestion chips based on query."""
    q = query.strip()
    suggestions = []
    # Add common related suffixes people search for
    for suffix in ["songs", "hits", "album", "latest", "best of", "playlist"]:
        if suffix.lower() not in q.lower():
            suggestions.append(f"{q} {suffix}")
    return suggestions[:4]


# ══════════════════════════════════════════════════════════════════════════════
#  UTILITIES
# ══════════════════════════════════════════════════════════════════════════════
def human_dur(secs: int) -> str:
    m, s = divmod(max(int(secs), 0), 60)
    return f"{m}:{s:02d}"


def cleanup(*paths):
    for p in paths:
        try:
            if p and os.path.exists(p): os.remove(p)
        except OSError: pass


async def send_audio(context, chat_id, filepath, meta, quality):
    title    = meta.get("title") or "Unknown"
    artist   = meta.get("artist") or ""
    album    = meta.get("album") or ""
    duration = int(meta.get("duration") or 0)
    year     = meta.get("year") or ""
    source   = meta.get("source") or ""

    src_tag = {"jiosaavn": "🎵 JioSaavn", "spotify": "💚 Spotify", "youtube": "▶️ YouTube"}.get(source, "")

    caption = f"🎧  *{title}*\n"
    if artist:  caption += f"👤  {artist}\n"
    if album:   caption += f"💿  {album}\n"
    if year:    caption += f"📅  {year}\n"
    if duration:
        caption += f"⏱  {human_dur(duration)}   ·   🎚 {quality} kbps\n"
    else:
        caption += f"🎚  {quality} kbps\n"
    caption += f"\n🔗  via @{BOT_ID}"
    if src_tag: caption += f"  ·  {src_tag}"

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


# ══════════════════════════════════════════════════════════════════════════════
#  DOWNLOAD HELPERS
# ══════════════════════════════════════════════════════════════════════════════
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
        # Final fallback — try JioSaavn for this song
        title  = song.get("title","")
        artist = song.get("artist","")
        if title:
            from helpers.jiosaavn import search_songs as _jio_s, download_and_encode as _jio_dl
            try:
                results = await _jio_s(f"{title} {artist}".strip(), quality=quality, limit=1)
                if results:
                    jio = results[0]
                    jio["quality"] = quality
                    path = await _jio_dl(jio)
                    lyrics = jio.get("lyrics") or ""
                    await tag_mp3(path, jio, lyrics=lyrics)
                    await send_audio(context, chat_id, path, jio, quality)
                    cleanup(path)
                    return
            except Exception as e:
                log.warning(f"JioSaavn fallback also failed: {e}")
        raise RuntimeError(
            "Could not download this track.\n"
            "YouTube is blocking cloud server IPs for this video.\n"
            "Try sending the JioSaavn link directly for Indian songs."
        )
    lyrics = await get_lyrics(song.get("title",""), song.get("artist",""))
    await tag_mp3(path, song, lyrics=lyrics)
    await send_audio(context, chat_id, path, song, quality)
    cleanup(path)


async def _dl_send_spotify(context, chat_id, song: dict, quality: str):
    path, enriched = await download_spotify_track(song, quality=quality)
    lyrics = enriched.get("lyrics") or await get_lyrics(
        enriched.get("title",""), enriched.get("artist","")
    )
    await tag_mp3(path, enriched, lyrics=lyrics)
    await send_audio(context, chat_id, path, enriched, quality)
    cleanup(path)


# ══════════════════════════════════════════════════════════════════════════════
#  SEARCH SESSION
# ══════════════════════════════════════════════════════════════════════════════
def _sess(context, sid) -> dict:
    return context.bot_data.get(f"s:{sid}", {})

def _save(context, sid, data):
    context.bot_data[f"s:{sid}"] = data

def _drop(context, sid):
    context.bot_data.pop(f"s:{sid}", None)


async def _fetch_results(query: str, source: str, jio_type: str = "songs", limit: int = 50) -> list[dict]:
    """Fetch up to `limit` results for one source."""
    try:
        if source == SRC_JIO:
            if jio_type == "albums":
                return await search_albums(query, limit=limit)
            elif jio_type == "artists":
                return await search_artists(query, limit=limit)
            else:
                return await search_songs(query, limit=limit)
        elif source == SRC_YT:
            ascii_q = unicodedata.normalize("NFKD", query).encode("ascii","ignore").decode().strip()
            q = ascii_q if ascii_q and ascii_q != query else query
            return await search_youtube(q, limit=limit)
    except Exception as e:
        log.warning(f"Fetch {source}/{jio_type} error: {e}")
    return []


def _build_search_kbd(results: list[dict], sid: str, source: str, jio_type: str,
                      page: int, total_pages: int, query: str = "") -> InlineKeyboardMarkup:
    """
    Keyboard layout:
      Numbered result buttons (6 per page)
      Source row:   🎵 JioSaavn  /  ▶️ YouTube   (radio — one active)
      Type row:     🎶 Songs  💿 Albums  🎤 Artists  (JioSaavn only)
      Suggestion:   🔍 related query chips
      Pagination:   ◀  Page N/M  ▶
      Close:        ✕ Close
    """
    buttons = []

    # ── Result rows ───────────────────────────────────────────────────────────
    start        = page * PAGE_SIZE
    page_results = results[start : start + PAGE_SIZE]

    for i, s in enumerate(page_results):
        global_i = start + i
        dur      = f"  {human_dur(s['duration'])}" if s.get("duration") else ""
        rtype    = s.get("result_type", "")
        artist   = s.get("artist") or ""
        title    = s.get("title") or "Unknown"

        if rtype == "album":
            icon  = "💿"
            label = f"{icon}  {title}  —  {artist}"
        elif rtype == "artist":
            icon  = "🎤"
            label = f"{icon}  {artist or title}"
        else:
            icon  = "🎵"
            label = f"{icon}  {artist}  —  {title}{dur}" if artist else f"{icon}  {title}{dur}"

        label = f"{global_i+1}.  {label[3:]}"   # keep number, strip duplicate icon — add back below
        label = f"{global_i+1}. {icon} {artist} — {title}{dur}" if (artist and not rtype) \
                else f"{global_i+1}. {icon} {title} — {artist}{dur}" if rtype == "album" \
                else f"{global_i+1}. {icon} {artist or title}"  if rtype == "artist" \
                else f"{global_i+1}. {icon} {title}{dur}"
        if len(label) > 62: label = label[:59] + "…"
        buttons.append([InlineKeyboardButton(label, callback_data=f"pick|{sid}|{global_i}")])

    # ── Source row (radio) ────────────────────────────────────────────────────
    src_row = []
    for src in [SRC_JIO, SRC_YT]:
        icon   = SRC_ICONS[src]
        lbl    = SRC_LABELS[src]
        active = "  ✦" if src == source else ""
        src_row.append(InlineKeyboardButton(
            f"{icon} {lbl}{active}",
            callback_data=f"src|{sid}|{src}",
        ))
    buttons.append(src_row)

    # ── JioSaavn type row ─────────────────────────────────────────────────────
    if source == SRC_JIO:
        type_row = []
        for key, lbl in JIO_TYPES.items():
            icon   = JIO_ICONS[key]
            active = "  ✦" if key == jio_type else ""
            type_row.append(InlineKeyboardButton(
                f"{icon} {lbl}{active}",
                callback_data=f"jtype|{sid}|{key}",
            ))
        buttons.append(type_row)

    # ── Related suggestions ───────────────────────────────────────────────────
    if query and page == 0:
        suggestions = _suggest_terms(query)
        if suggestions:
            # Split into rows of 2
            for i in range(0, min(len(suggestions), 4), 2):
                row = []
                for sug in suggestions[i:i+2]:
                    short = sug if len(sug) <= 22 else sug[:19] + "…"
                    row.append(InlineKeyboardButton(
                        f"🔍 {short}",
                        callback_data=f"suggest|{sid}|{sug[:40]}",
                    ))
                buttons.append(row)

    # ── Pagination ────────────────────────────────────────────────────────────
    if total_pages > 1:
        pg_row = []
        if page > 0:
            pg_row.append(InlineKeyboardButton("◀  Prev", callback_data=f"pg|{sid}|{page-1}"))
        pg_row.append(InlineKeyboardButton(f"  {page+1} / {total_pages}  ", callback_data="noop"))
        if page < total_pages - 1:
            pg_row.append(InlineKeyboardButton("Next  ▶", callback_data=f"pg|{sid}|{page+1}"))
        buttons.append(pg_row)

    buttons.append([InlineKeyboardButton("✕  Close", callback_data=f"pick|{sid}|close")])
    return InlineKeyboardMarkup(buttons)


async def _render_search(context, chat_id, sid: str, msg_id: int | None = None) -> int:
    sess     = _sess(context, sid)
    query    = sess.get("query", "")
    source   = sess.get("source", SRC_JIO)
    jio_type = sess.get("jio_type", "songs")
    page     = sess.get("page", 0)
    cache    = sess.get("cache", {})

    cache_key = f"{source}:{jio_type}"
    if cache_key not in cache:
        results = await _fetch_results(query, source, jio_type, limit=50)
        cache[cache_key] = results
        sess["cache"] = cache
        _save(context, sid, sess)

    results     = cache.get(cache_key, [])
    total_pages = max(1, ceil(len(results) / PAGE_SIZE))
    page        = min(page, total_pages - 1)

    sess["page"] = page
    _save(context, sid, sess)

    src_label = SRC_LABELS.get(source, source)
    type_label = JIO_TYPES.get(jio_type, "") if source == SRC_JIO else ""
    subtitle  = f"{src_label}  •  {type_label}" if type_label else src_label

    if results:
        text = (
            f"🔍  *{query}*\n"
            f"_{subtitle}_  ·  {len(results)} results\n\n"
            f"Tap any result to download ↓"
        )
    else:
        text = (
            f"🔍  *{query}*\n"
            f"_{subtitle}_\n\n"
            f"No results found. Try switching source or type."
        )

    kbd = _build_search_kbd(results, sid, source, jio_type, page, total_pages, query=query)

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


async def run_search(context, chat_id: int, query: str, sid: str):
    status = await context.bot.send_message(chat_id, f"🔍  Searching _{query}_…", parse_mode=ParseMode.MARKDOWN)
    _save(context, sid, {
        "query":    query,
        "source":   SRC_JIO,
        "jio_type": "songs",
        "page":     0,
        "cache":    {},
    })
    await status.delete()
    await _render_search(context, chat_id, sid)


# ══════════════════════════════════════════════════════════════════════════════
#  CALLBACK HANDLERS
# ══════════════════════════════════════════════════════════════════════════════
async def on_suggestion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User taps a related suggestion chip — re-search with that query."""
    cb = update.callback_query
    await cb.answer()
    _, sid, new_query = cb.data.split("|", 2)

    sess = _sess(context, sid)
    if not sess:
        await cb.edit_message_text("Session expired. Search again.")
        return

    sess["query"]    = new_query
    sess["page"]     = 0
    sess["cache"]    = {}   # clear cache for new query
    _save(context, sid, sess)
    await _render_search(context, update.effective_chat.id, sid, msg_id=cb.message.message_id)


async def on_noop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()


async def on_source_switch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User taps a source button — switch source, reset page, re-render."""
    cb = update.callback_query
    await cb.answer()
    _, sid, new_src = cb.data.split("|", 2)

    sess = _sess(context, sid)
    if not sess:
        await cb.edit_message_text("Session expired. Search again.")
        return

    sess["source"] = new_src
    sess["page"]   = 0
    _save(context, sid, sess)
    await _render_search(context, update.effective_chat.id, sid, msg_id=cb.message.message_id)


async def on_jio_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User taps Songs/Albums/Artists sub-button."""
    cb = update.callback_query
    await cb.answer()
    _, sid, jtype = cb.data.split("|", 2)

    sess = _sess(context, sid)
    if not sess:
        await cb.edit_message_text("Session expired. Search again.")
        return

    sess["jio_type"] = jtype
    sess["page"]     = 0
    _save(context, sid, sess)
    await _render_search(context, update.effective_chat.id, sid, msg_id=cb.message.message_id)


async def on_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User taps Prev/Next page."""
    cb = update.callback_query
    await cb.answer()
    _, sid, page_str = cb.data.split("|", 2)

    sess = _sess(context, sid)
    if not sess:
        await cb.edit_message_text("Session expired. Search again.")
        return

    sess["page"] = int(page_str)
    _save(context, sid, sess)
    await _render_search(context, update.effective_chat.id, sid, msg_id=cb.message.message_id)


async def on_search_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User taps a result — show quality picker."""
    cb = update.callback_query
    await cb.answer()
    parts = (cb.data or "").split("|", 2)
    if len(parts) != 3: return
    _, sid, idx = parts

    if idx == "close":
        _drop(context, sid)
        try: await cb.message.delete()
        except Exception: pass
        return

    sess = _sess(context, sid)
    if not sess:
        await cb.edit_message_text("Session expired. Search again.")
        return

    source   = sess.get("source", SRC_JIO)
    jio_type = sess.get("jio_type", "songs")
    cache    = sess.get("cache", {})
    results  = cache.get(f"{source}:{jio_type}", [])

    try:
        item = results[int(idx)]
    except (IndexError, ValueError):
        return

    # If album/artist was picked → fetch songs, go back to search view
    rtype = item.get("result_type", "")
    if rtype == "album":
        await cb.answer("Loading album…", show_alert=False)
        album_q = item.get("album_query") or item.get("title", "")
        status  = await context.bot.send_message(
            update.effective_chat.id,
            f"Loading album _{item.get('title')}_…",
            parse_mode=ParseMode.MARKDOWN,
        )
        songs = await search_songs(album_q, limit=25)
        await status.delete()
        if not songs:
            await context.bot.send_message(update.effective_chat.id, "No songs found for this album.")
            return
        # Show quality picker for whole album
        pick_key = f"p:{sid}:album"
        context.bot_data[pick_key] = songs   # list of songs
        _drop(context, sid)
        kbd = InlineKeyboardMarkup([[
            InlineKeyboardButton("128 kbps", callback_data=f"q|128|{pick_key}"),
            InlineKeyboardButton("320 kbps", callback_data=f"q|320|{pick_key}"),
        ]])
        await cb.edit_message_text(
            f"*{item.get('title')}*\n_{item.get('artist','')}_\n\n{len(songs)} songs — choose quality:",
            parse_mode=ParseMode.MARKDOWN, reply_markup=kbd,
        )
        return

    if rtype == "artist":
        artist_name = item.get("artist") or item.get("title", "")
        # Search songs by this artist
        new_sid = f"{sid}_ar"
        _drop(context, sid)
        _save(context, new_sid, {
            "query":    artist_name,
            "source":   SRC_JIO,
            "jio_type": "songs",
            "page":     0,
            "cache":    {},
        })
        await _render_search(context, update.effective_chat.id, new_sid,
                             msg_id=cb.message.message_id)
        return

    # Normal song — show quality picker
    pick_key = f"p:{sid}:{idx}"
    context.bot_data[pick_key] = item
    _drop(context, sid)

    title  = item.get("title","?")
    artist = item.get("artist","")
    label  = f"{artist} — {title}" if artist else title
    if len(label) > 60: label = label[:57] + "…"

    kbd = InlineKeyboardMarkup([[
        InlineKeyboardButton("128 kbps", callback_data=f"q|128|{pick_key}"),
        InlineKeyboardButton("320 kbps", callback_data=f"q|320|{pick_key}"),
    ]])
    await cb.edit_message_text(
        f"*{label}*\n\nChoose quality:",
        parse_mode=ParseMode.MARKDOWN, reply_markup=kbd,
    )


# ══════════════════════════════════════════════════════════════════════════════
#  DOWNLOAD PIPELINES
# ══════════════════════════════════════════════════════════════════════════════
async def run_jiosaavn(context, chat_id, url, kind, quality):
    status = await context.bot.send_message(chat_id, "⏳  Fetching from JioSaavn…")
    try:
        if kind == "song":       songs = await fetch_song(url, quality)
        elif kind == "album":    songs = await fetch_album(url, quality)
        elif kind == "playlist": songs = await fetch_playlist(url, quality)
        else:                    songs = await fetch_song(url, quality)

        if not songs:
            await status.edit_text("😕  No results from JioSaavn. The link may be invalid.")
            return

        total = min(len(songs), MAX_PLAYLIST_SONGS)
        await status.edit_text(f"⬇️  Downloading {total} track{'s' if total>1 else ''} at {quality} kbps…")
        for song in songs[:MAX_PLAYLIST_SONGS]:
            try:
                await _dl_send_jio(context, chat_id, song, quality)
                if total > 1: await asyncio.sleep(1)
            except Exception as e:
                log.error(f"JioSaavn song error: {e}")
                await context.bot.send_message(
                    chat_id,
                    f"⚠️  Skipped *{song.get('title','?')}*\n`{type(e).__name__}: {e}`",
                    parse_mode=ParseMode.MARKDOWN,
                )
        await status.delete()
    except Exception as e:
        log.error(f"JioSaavn pipeline: {e}")
        await status.edit_text(f"❌  Something went wrong.\n`{e}`", parse_mode=ParseMode.MARKDOWN)


async def run_spotify(context, chat_id, url, kind, quality):
    status = await context.bot.send_message(chat_id, "⏳  Reading Spotify info…")
    try:
        tracks, err = await spotify_scrape(url, kind)
        if err or not tracks:
            await status.edit_text(
                f"❌  Couldn't read Spotify info.\n`{err or 'No tracks found'}`",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        total = min(len(tracks), MAX_PLAYLIST_SONGS)
        await status.edit_text(f"⬇️  Downloading {total} track{'s' if total>1 else ''} at {quality} kbps…")

        for track in tracks[:MAX_PLAYLIST_SONGS]:
            try:
                await _dl_send_spotify(context, chat_id, track, quality)
                if total > 1: await asyncio.sleep(1.5)
            except Exception as e:
                log.error(f"Spotify track error: {e}")
                await context.bot.send_message(
                    chat_id,
                    f"⚠️  Skipped *{track.get('title','?')}*\n`{type(e).__name__}: {e}`",
                    parse_mode=ParseMode.MARKDOWN,
                )
        await status.delete()
    except Exception as e:
        log.error(f"Spotify pipeline: {e}")
        await status.edit_text(f"❌  Something went wrong.\n`{e}`", parse_mode=ParseMode.MARKDOWN)


async def run_youtube(context, chat_id, url, quality):
    status = await context.bot.send_message(chat_id, "⏳  Downloading from YouTube…")
    try:
        path = await download_yt(url, quality=quality)
        if not path:
            await status.edit_text("❌  Download failed. The video may be unavailable.")
            return
        stem = Path(path).stem.replace(f"_{quality}kbps", "")
        meta = {"title": stem, "artist": "", "album": "", "duration": 0}
        await tag_mp3(path, meta)
        await send_audio(context, chat_id, path, meta, quality)
        cleanup(path)
        await status.delete()
    except Exception as e:
        log.error(f"YouTube pipeline: {e}")
        await status.edit_text(f"❌  Something went wrong.\n`{e}`", parse_mode=ParseMode.MARKDOWN)


# ══════════════════════════════════════════════════════════════════════════════
#  QUALITY CALLBACK
# ══════════════════════════════════════════════════════════════════════════════
async def on_quality(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cb = update.callback_query
    await cb.answer()
    parts = (cb.data or "").split("|", 2)
    if len(parts) != 3: return
    _, quality, key = parts
    chat_id = update.effective_chat.id

    payload = context.bot_data.pop(key, None)
    if not payload:
        await cb.edit_message_text("⏰  Session expired. Please send the link again.")
        return

    try:
        await cb.edit_message_text(
            f"⬇️  Grabbing it at *{quality} kbps*…", parse_mode=ParseMode.MARKDOWN
        )
    except BadRequest:
        pass

    # URL payload
    if isinstance(payload, dict) and "type" in payload:
        t = payload["type"]
        if   t == "jio":     await run_jiosaavn(context, chat_id, payload["url"], payload["kind"], quality)
        elif t == "spotify": await run_spotify(context, chat_id, payload["url"], payload["kind"], quality)
        elif t == "youtube": await run_youtube(context, chat_id, payload["url"], quality)

    # Album payload (list of songs)
    elif isinstance(payload, list):
        status = await context.bot.send_message(
            chat_id,
            f"⬇️  Downloading {len(payload)} tracks at {quality} kbps…"
        )
        for song in payload:
            try:
                await _dl_send_jio(context, chat_id, song, quality)
                await asyncio.sleep(1)
            except Exception as e:
                await context.bot.send_message(
                    chat_id,
                    f"⚠️  Skipped *{song.get('title','?')}*\n`{e}`",
                    parse_mode=ParseMode.MARKDOWN,
                )
        await status.delete()

    # Single song from search
    else:
        song   = payload
        src    = song.get("source", SRC_YT)
        title  = song.get("title","?")
        status = await context.bot.send_message(
            chat_id,
            f"⬇️  Downloading *{title}* at {quality} kbps…",
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
            await status.edit_text(f"❌  Download failed.\n`{e}`", parse_mode=ParseMode.MARKDOWN)


# ══════════════════════════════════════════════════════════════════════════════
#  COMMANDS
# ══════════════════════════════════════════════════════════════════════════════
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎶  *Welcome to Music Downloader Bot!*\n\n"
        "Drop a link or type a song name — I'll handle the rest.\n\n"
        "🎵  JioSaavn  ·  song, album, playlist\n"
        "💚  Spotify  ·  track, album, playlist, artist\n"
        "▶️  YouTube  ·  any video link\n"
        "🔍  Search  ·  just type a song or artist name\n\n"
        "Every file comes packed with cover art, lyrics, "
        "artist, album and year tags. 🎧\n\n"
        "Type /help to see all options.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🛠  *How it works*\n\n"
        "━━━━━━━━━━━━━━━\n"
        "🔗  *Send a link*\n"
        "  🎵 JioSaavn — song, album or playlist URL\n"
        "  💚 Spotify — track, album, playlist or artist URL\n"
        "  ▶️ YouTube — any video URL\n\n"
        "🔍  *Search by name*\n"
        "  Type any song or artist name.\n"
        "  Use the filter row to switch between\n"
        "  JioSaavn and YouTube results.\n"
        "  On JioSaavn, tap 🎶 Songs / 💿 Albums / 🎤 Artists\n"
        "  to change the result type.\n"
        "  Browse pages with ◀ and ▶.\n\n"
        "🎚  *Quality*\n"
        "  Choose 128 kbps or 320 kbps before every download.\n\n"
        "📦  *Every file includes*\n"
        "  Cover art · Title · Artist · Album · Year · Lyrics\n"
        "━━━━━━━━━━━━━━━",
        parse_mode=ParseMode.MARKDOWN,
    )


# ══════════════════════════════════════════════════════════════════════════════
#  MESSAGE ROUTER
# ══════════════════════════════════════════════════════════════════════════════
async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text    = (update.message.text or "").strip()
    if not text: return

    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    msg_id  = update.message.message_id

    # Unique key: chat + user + message — prevents collision in group chats
    mid = f"{chat_id}_{user_id}_{msg_id}"

    # In groups: strip bot mention if present (@BotName text → text)
    bot_username = (await context.bot.get_me()).username
    if f"@{bot_username}" in text:
        text = text.replace(f"@{bot_username}", "").strip()
    if not text: return

    jio_kind = detect_jiosaavn(text)
    sp_kind  = detect_spotify(text)
    yt       = is_youtube(text)

    def _quality_kbd(key):
        return InlineKeyboardMarkup([[
            InlineKeyboardButton("🎵  128 kbps", callback_data=f"q|128|{key}"),
            InlineKeyboardButton("🎧  320 kbps", callback_data=f"q|320|{key}"),
        ]])

    if jio_kind:
        context.bot_data[mid] = {"type": "jio", "url": text, "kind": jio_kind}
        kind_label = {"song": "Song", "album": "Album", "playlist": "Playlist"}.get(jio_kind, "Link")
        await update.message.reply_text(
            f"🎵  *JioSaavn {kind_label}* detected\n\nPick your quality 👇",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_quality_kbd(mid),
        )
    elif sp_kind:
        context.bot_data[mid] = {"type": "spotify", "url": text, "kind": sp_kind}
        kind_label = {"track": "Track", "album": "Album", "playlist": "Playlist", "artist": "Artist"}.get(sp_kind, "Link")
        await update.message.reply_text(
            f"💚  *Spotify {kind_label}* detected\n\nPick your quality 👇",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_quality_kbd(mid),
        )
    elif yt:
        context.bot_data[mid] = {"type": "youtube", "url": text, "kind": ""}
        await update.message.reply_text(
            f"▶️  *YouTube* link detected\n\nPick your quality 👇",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_quality_kbd(mid),
        )
    else:
        await run_search(context, chat_id, text, mid)


# ══════════════════════════════════════════════════════════════════════════════
#  HEALTH SERVER
# ══════════════════════════════════════════════════════════════════════════════
async def _health(_): return web.Response(text="OK")

async def _keep_alive(app_url: str):
    """Ping own health endpoint every 5 min to prevent Koyeb sleep."""
    await asyncio.sleep(60)   # wait for server to start first
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                await session.get(f"{app_url}/health", timeout=aiohttp.ClientTimeout(total=10))
                log.info("Keep-alive ping sent")
            except Exception as e:
                log.warning(f"Keep-alive ping failed: {e}")
            await asyncio.sleep(300)   # every 5 minutes

async def start_health():
    app_url = os.environ.get("APP_URL", "").rstrip("/")
    webapp = web.Application()
    webapp.router.add_get("/health", _health)
    webapp.router.add_get("/",       _health)
    runner = web.AppRunner(webapp)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    log.info(f"Health server on :{PORT}")
    if app_url:
        asyncio.create_task(_keep_alive(app_url))
        log.info(f"Keep-alive enabled → {app_url}")


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════════════
async def main():
    await start_health()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help",  cmd_help))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    app.add_handler(CallbackQueryHandler(on_noop,         pattern=r"^noop$"))
    app.add_handler(CallbackQueryHandler(on_suggestion,   pattern=r"^suggest\|"))
    app.add_handler(CallbackQueryHandler(on_source_switch, pattern=r"^src\|"))
    app.add_handler(CallbackQueryHandler(on_jio_type,      pattern=r"^jtype\|"))
    app.add_handler(CallbackQueryHandler(on_page,          pattern=r"^pg\|"))
    app.add_handler(CallbackQueryHandler(on_search_pick,   pattern=r"^pick\|"))
    app.add_handler(CallbackQueryHandler(on_quality,       pattern=r"^q\|"))

    log.info("Bot starting…")
    async with app:
        await app.start()
        await app.updater.start_polling(
            drop_pending_updates=True,
            allowed_updates=["message", "callback_query", "edited_message"],
        )
        log.info("Bot running.")
        await asyncio.Event().wait()
        await app.updater.stop()
        await app.stop()


if __name__ == "__main__":
    asyncio.run(main())
