"""
helpers/spotify.py

Spotify support WITHOUT spotipy — uses yt-dlp's built-in Spotify extractor.
This avoids Spotify API 403 errors caused by cloud server IP blocks.

How it works:
  - Spotify track/album/playlist URL → yt-dlp extracts metadata + searches YouTube
  - YouTube URL → yt-dlp downloads directly
  - Search query → yt-dlp ytsearch
  - Lyrics → Genius API (optional)
"""

import os
import re
import asyncio
import logging
import json

import yt_dlp

from config import (
    GENIUS_ACCESS_TOKEN, DOWNLOAD_DIR, MAX_PLAYLIST_SONGS
)

log = logging.getLogger(__name__)

# ── URL detection ─────────────────────────────────────────────────────────────
_SP_TRACK    = re.compile(r"open\.spotify\.com/(?:[a-z-]+/)?track/([A-Za-z0-9]+)")
_SP_ALBUM    = re.compile(r"open\.spotify\.com/(?:[a-z-]+/)?album/([A-Za-z0-9]+)")
_SP_PLAYLIST = re.compile(r"open\.spotify\.com/(?:[a-z-]+/)?playlist/([A-Za-z0-9]+)")
_SP_ARTIST   = re.compile(r"open\.spotify\.com/(?:[a-z-]+/)?artist/([A-Za-z0-9]+)")
_YT          = re.compile(r"(youtube\.com/watch|youtu\.be/|music\.youtube\.com)")


def detect_spotify(text: str) -> str | None:
    if "spotify.com" not in text:
        return None
    if _SP_TRACK.search(text):    return "track"
    if _SP_ALBUM.search(text):    return "album"
    if _SP_PLAYLIST.search(text): return "playlist"
    if _SP_ARTIST.search(text):   return "artist"
    return None


def is_youtube(text: str) -> bool:
    return bool(_YT.search(text))


# ── yt-dlp metadata extractor (no download) ───────────────────────────────────
def _extract_info(url: str, flat: bool = True) -> dict | None:
    """Extract metadata from Spotify/YouTube URL without downloading."""
    opts = {
        "quiet":            True,
        "no_warnings":      True,
        "extract_flat":     flat,
        "skip_download":    True,
        "noplaylist":       False,
        "ignoreerrors":     True,
        "socket_timeout":   20,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)
    except Exception as e:
        log.error(f"yt-dlp extract_info error for {url}: {e}")
        return None


def _entry_to_track(entry: dict) -> dict | None:
    """Convert a yt-dlp info dict entry to our track dict format."""
    if not entry or not isinstance(entry, dict):
        return None

    title  = entry.get("title") or entry.get("track") or "Unknown"
    artist = (
        entry.get("artist")
        or entry.get("uploader")
        or entry.get("creator")
        or ""
    )
    album    = entry.get("album") or ""
    duration = int(entry.get("duration") or 0)
    image    = (
        entry.get("thumbnail")
        or (entry.get("thumbnails") or [{}])[-1].get("url", "")
    )

    # Build search query for YouTube
    search = f"{title} {artist}".strip() if artist else title

    return {
        "title":    title,
        "artist":   artist,
        "album":    album,
        "image":    image,
        "duration": duration,
        "search":   search,
        "lyrics":   "",
    }


# ── Spotify fetchers (via yt-dlp) ─────────────────────────────────────────────
def spotify_track(url: str) -> tuple[list[dict], str | None]:
    """Returns (tracks, error)"""
    info = _extract_info(url, flat=False)
    if not info:
        return [], "yt-dlp could not extract Spotify track info"
    track = _entry_to_track(info)
    if not track:
        return [], "Could not parse track info"
    return [track], None


def spotify_album(url: str) -> tuple[list[dict], str | None]:
    info = _extract_info(url, flat=True)
    if not info:
        return [], "yt-dlp could not extract Spotify album info"
    entries = info.get("entries") or []
    tracks  = []
    for e in entries[:MAX_PLAYLIST_SONGS]:
        # Flat entries have limited info — build search from title + artist
        if isinstance(e, dict):
            t = _entry_to_track(e)
            if t:
                tracks.append(t)
    if not tracks:
        return [], "No tracks found in album"
    return tracks, None


def spotify_playlist(url: str) -> tuple[list[dict], str | None]:
    info = _extract_info(url, flat=True)
    if not info:
        return [], "yt-dlp could not extract Spotify playlist info"
    entries = info.get("entries") or []
    tracks  = []
    for e in entries[:MAX_PLAYLIST_SONGS]:
        if isinstance(e, dict):
            t = _entry_to_track(e)
            if t:
                tracks.append(t)
    if not tracks:
        return [], "No tracks found in playlist"
    return tracks, None


def spotify_artist(url: str) -> tuple[list[dict], str | None]:
    info = _extract_info(url, flat=True)
    if not info:
        return [], "yt-dlp could not extract Spotify artist info"
    entries = info.get("entries") or []
    tracks  = []
    for e in entries[:10]:
        if isinstance(e, dict):
            t = _entry_to_track(e)
            if t:
                tracks.append(t)
    if not tracks:
        return [], "No tracks found for artist"
    return tracks, None


# ── yt-dlp audio downloader ───────────────────────────────────────────────────
def _safe_filename(s: str) -> str:
    return re.sub(r'[<>:"/\\|?*\n\r\t]', "", s).strip()[:80]


def _ydl_opts(out_tmpl: str, quality: str) -> dict:
    return {
        "format":       "bestaudio/best",
        "outtmpl":      out_tmpl,
        "quiet":        True,
        "no_warnings":  True,
        "noplaylist":   True,
        "postprocessors": [{
            "key":              "FFmpegExtractAudio",
            "preferredcodec":   "mp3",
            "preferredquality": quality,
        }],
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/124 Safari/537.36"
            )
        },
        "retries":        3,
        "socket_timeout": 30,
        "ignoreerrors":   False,
    }


async def download_yt(
    url_or_query: str,
    meta: dict | None = None,
    quality: str = "320",
) -> str | None:
    """
    Download audio from YouTube URL or search query.
    quality: "128" or "320"
    Returns final .mp3 path or None.
    """
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    if meta and (meta.get("artist") or meta.get("title")):
        base = _safe_filename(f"{meta.get('artist', '')} - {meta.get('title', '')}")
    else:
        base = _safe_filename(url_or_query[:60])

    out_tmpl   = os.path.join(DOWNLOAD_DIR, f"{base}_{quality}kbps.%(ext)s")
    final_path = os.path.join(DOWNLOAD_DIR, f"{base}_{quality}kbps.mp3")

    if os.path.exists(final_path):
        return final_path

    target = url_or_query
    if not (target.startswith("http") or target.startswith("www.")):
        target = f"ytsearch1:{target}"

    opts = _ydl_opts(out_tmpl, quality)
    loop = asyncio.get_event_loop()

    try:
        def _run():
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([target])
        await loop.run_in_executor(None, _run)
    except Exception as e:
        log.error(f"yt-dlp download error for '{target}': {e}")
        return None

    return final_path if os.path.exists(final_path) else None


# ── Genius lyrics ─────────────────────────────────────────────────────────────
async def get_lyrics(title: str, artist: str) -> str:
    if not GENIUS_ACCESS_TOKEN or not title:
        return ""
    try:
        import lyricsgenius
        genius = lyricsgenius.Genius(
            GENIUS_ACCESS_TOKEN,
            verbose=False,
            timeout=10,
            remove_section_headers=True,
        )
        loop = asyncio.get_event_loop()
        song = await loop.run_in_executor(
            None, lambda: genius.search_song(title, artist or "")
        )
        if song and song.lyrics:
            return song.lyrics[:4000]
    except Exception as e:
        log.warning(f"Genius error: {e}")
    return ""
