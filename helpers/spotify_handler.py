"""
helpers/spotify_handler.py

Spotify downloads via spotDL (spotDL/spotify-downloader).

Why spotDL works (unlike spotipy):
  - spotipy's search() calls /v1/search  → requires Premium since Nov 2024 → 403
  - spotDL calls /v1/tracks /v1/albums /v1/playlists → still FREE with any dev account
  - spotDL finds matching YouTube audio automatically
  - spotDL embeds title, artist, album, cover art, lyrics in one go

Flow:
  Spotify URL
    → spotDL.search([url])       # gets metadata from Spotify free endpoints
    → spotDL.download(song)      # finds YouTube match, downloads, embeds tags
    → returns MP3 path

SPOTIFY_CLIENT_ID + SPOTIFY_CLIENT_SECRET still required (free developer account is fine).
Get them at: developer.spotify.com/dashboard → Create App

YouTube/Search downloads:
  Direct YouTube URLs and search queries still use yt-dlp.
"""

import os
import re
import asyncio
import logging
from pathlib import Path

import yt_dlp

from config import (
    SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET,
    GENIUS_ACCESS_TOKEN, DOWNLOAD_DIR, MAX_PLAYLIST_SONGS, MAX_SEARCH_RESULTS,
)

log = logging.getLogger(__name__)

# ── URL detection ─────────────────────────────────────────────────────────────
_SP_TRACK    = re.compile(r"open\.spotify\.com/(?:[a-z-]+/)?track/([A-Za-z0-9]+)")
_SP_ALBUM    = re.compile(r"open\.spotify\.com/(?:[a-z-]+/)?album/([A-Za-z0-9]+)")
_SP_PLAYLIST = re.compile(r"open\.spotify\.com/(?:[a-z-]+/)?playlist/([A-Za-z0-9]+)")
_SP_ARTIST   = re.compile(r"open\.spotify\.com/(?:[a-z-]+/)?artist/([A-Za-z0-9]+)")
_YT          = re.compile(r"(youtube\.com/watch|youtu\.be/|music\.youtube\.com)")


def detect_spotify(text: str) -> str | None:
    if "spotify.com" not in text: return None
    if _SP_TRACK.search(text):    return "track"
    if _SP_ALBUM.search(text):    return "album"
    if _SP_PLAYLIST.search(text): return "playlist"
    if _SP_ARTIST.search(text):   return "artist"
    return None


def is_youtube(text: str) -> bool:
    return bool(_YT.search(text))


# ── spotDL singleton ──────────────────────────────────────────────────────────
_spotdl = None
_spotdl_err: str | None = None


def _get_spotdl(bitrate: str = "320k"):
    """Lazy-init spotDL instance."""
    global _spotdl, _spotdl_err

    if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
        _spotdl_err = (
            "SPOTIFY_CLIENT_ID or SPOTIFY_CLIENT_SECRET not set.\n"
            "Add them in Koyeb env vars.\n"
            "Get from: developer.spotify.com/dashboard (free account works)"
        )
        return None, _spotdl_err

    try:
        from spotdl import Spotdl
        os.makedirs(DOWNLOAD_DIR, exist_ok=True)

        instance = Spotdl(
            client_id=SPOTIFY_CLIENT_ID,
            client_secret=SPOTIFY_CLIENT_SECRET,
            downloader_settings={
                "output":          f"{DOWNLOAD_DIR}/{{artist}} - {{title}}.{{output-ext}}",
                "format":          "mp3",
                "bitrate":         bitrate,
                "threads":         1,
                "overwrite":       "skip",
                "log_level":       "ERROR",
                "print_errors":    False,
                "generate_lrc":    False,
                "save_file":       None,
                "lyrics_providers": ["musixmatch", "genius"],
            },
        )
        _spotdl_err = None
        return instance, None
    except Exception as e:
        _spotdl_err = str(e)
        log.error(f"spotDL init error: {e}")
        return None, str(e)


def _song_to_meta(song) -> dict:
    """Convert spotDL Song object to our standard meta dict."""
    artists = ", ".join(song.artists) if hasattr(song, "artists") else getattr(song, "artist", "")
    return {
        "title":    song.name,
        "artist":   artists,
        "album":    getattr(song, "album_name", "") or "",
        "image":    getattr(song, "cover_url", "") or "",
        "duration": int(getattr(song, "duration", 0) or 0),
        "lyrics":   getattr(song, "lyrics", "") or "",
        "source":   "spotify",
    }


# ── Spotify URL downloads ─────────────────────────────────────────────────────
async def spotdl_download(url: str, bitrate: str = "320k") -> tuple[list[tuple[dict, str]], str | None]:
    """
    Download one or more tracks from a Spotify URL using spotDL.
    Returns: ([(meta, filepath), ...], error_message)
    """
    spotdl, err = _get_spotdl(bitrate)
    if err:
        return [], err

    loop = asyncio.get_event_loop()
    try:
        # 1. Get songs metadata from Spotify (uses /v1/tracks, /v1/albums etc — no Premium needed)
        songs = await loop.run_in_executor(
            None, lambda: spotdl.search([url])
        )
        if not songs:
            return [], "Spotify URL returned no tracks. Check the link."

        songs = songs[:MAX_PLAYLIST_SONGS]
        log.info(f"spotDL found {len(songs)} track(s) for {url}")

        results = []
        for song in songs:
            try:
                # 2. Download: spotDL finds YouTube match, downloads, embeds tags
                _, path = await loop.run_in_executor(
                    None, lambda s=song: spotdl.download(s)
                )
                if path and Path(path).exists():
                    meta = _song_to_meta(song)
                    results.append((meta, str(path)))
                else:
                    log.warning(f"spotDL: no file for {song.name}")
            except Exception as e:
                log.error(f"spotDL download error for '{song.name}': {e}")

        if not results:
            return [], "spotDL could not download any tracks (YouTube match may not exist)"

        return results, None

    except Exception as e:
        log.error(f"spotDL error: {e}")
        err_str = str(e)
        if "403" in err_str or "premium" in err_str.lower():
            return [], (
                "Spotify API 403.\n"
                "Your app may be in dev mode. Fix:\n"
                "developer.spotify.com → your app → Users and Access → add your email"
            )
        return [], f"spotDL error: {err_str}"


# ── YouTube search — multiple results ─────────────────────────────────────────
async def search_youtube(query: str, limit: int = MAX_SEARCH_RESULTS) -> list[dict]:
    opts = {
        "quiet":         True,
        "no_warnings":   True,
        "extract_flat":  True,
        "skip_download": True,
        "noplaylist":    False,
        "ignoreerrors":  True,
    }
    loop = asyncio.get_event_loop()
    try:
        def _run():
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
        info = await loop.run_in_executor(None, _run)
        if not info:
            return []
        results = []
        for e in (info.get("entries") or []):
            if not e:
                continue
            vid_id = e.get("id") or ""
            results.append({
                "title":    e.get("title") or "Unknown",
                "artist":   e.get("uploader") or e.get("channel") or "",
                "album":    "",
                "image":    e.get("thumbnail") or "",
                "duration": int(e.get("duration") or 0),
                "search":   f"https://youtu.be/{vid_id}" if vid_id else e.get("url", ""),
                "lyrics":   "",
                "source":   "youtube",
            })
        return results[:limit]
    except Exception as e:
        log.error(f"YouTube search error: {e}")
        return []


# ── yt-dlp audio download ─────────────────────────────────────────────────────
def _safe_fn(s: str) -> str:
    return re.sub(r'[<>:"/\\|?*\n\r\t]', "", s).strip()[:80]


async def download_yt(
    url_or_query: str,
    meta: dict | None = None,
    quality: str = "320",
) -> str | None:
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    if meta and (meta.get("artist") or meta.get("title")):
        base = _safe_fn(f"{meta.get('artist','')} - {meta.get('title','')}")
    else:
        base = _safe_fn(url_or_query[:60])

    out_tmpl   = os.path.join(DOWNLOAD_DIR, f"{base}_{quality}kbps.%(ext)s")
    final_path = os.path.join(DOWNLOAD_DIR, f"{base}_{quality}kbps.mp3")

    if os.path.exists(final_path):
        return final_path

    target = url_or_query
    if not (target.startswith("http") or target.startswith("www.")):
        target = f"ytsearch1:{target}"

    opts = {
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
    }
    loop = asyncio.get_event_loop()
    try:
        def _run():
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([target])
        await loop.run_in_executor(None, _run)
    except Exception as e:
        log.error(f"yt-dlp error '{target}': {e}")
        return None

    return final_path if os.path.exists(final_path) else None


# ── Genius lyrics ─────────────────────────────────────────────────────────────
async def get_lyrics(title: str, artist: str) -> str:
    if not GENIUS_ACCESS_TOKEN or not title:
        return ""
    try:
        import lyricsgenius
        genius = lyricsgenius.Genius(
            GENIUS_ACCESS_TOKEN, verbose=False,
            timeout=10, remove_section_headers=True,
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
