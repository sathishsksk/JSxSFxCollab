"""
helpers/spotify.py

Spotify: get metadata via spotipy, download audio via yt-dlp + FFmpeg
YouTube: download directly via yt-dlp + FFmpeg
Search:  yt-dlp ytsearch
Lyrics:  Genius API (optional)
"""

import os
import re
import asyncio
import logging

import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
import yt_dlp

from config import (
    SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET,
    GENIUS_ACCESS_TOKEN, DOWNLOAD_DIR, MAX_PLAYLIST_SONGS
)

log = logging.getLogger(__name__)

# ── Spotify client (lazy init) ────────────────────────────────────────────────
_sp: spotipy.Spotify | None = None


def _spotify() -> spotipy.Spotify | None:
    global _sp
    if _sp is None and SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET:
        try:
            _sp = spotipy.Spotify(
                auth_manager=SpotifyClientCredentials(
                    client_id=SPOTIFY_CLIENT_ID,
                    client_secret=SPOTIFY_CLIENT_SECRET,
                )
            )
        except Exception as e:
            log.error(f"Spotify init error: {e}")
    return _sp


# ── URL detection ─────────────────────────────────────────────────────────────
_SP_TRACK    = re.compile(r"open\.spotify\.com/(?:intl-\w+/)?track/([A-Za-z0-9]+)")
_SP_ALBUM    = re.compile(r"open\.spotify\.com/(?:intl-\w+/)?album/([A-Za-z0-9]+)")
_SP_PLAYLIST = re.compile(r"open\.spotify\.com/(?:intl-\w+/)?playlist/([A-Za-z0-9]+)")
_SP_ARTIST   = re.compile(r"open\.spotify\.com/(?:intl-\w+/)?artist/([A-Za-z0-9]+)")
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


# ── Spotify metadata fetchers ─────────────────────────────────────────────────
def _build_track(t: dict, album: dict | None = None) -> dict:
    artists = ", ".join(a["name"] for a in t.get("artists", []))
    alb     = album or t.get("album", {})
    images  = alb.get("images", [])
    image   = images[0]["url"] if images else ""
    return {
        "title":    t["name"],
        "artist":   artists,
        "album":    alb.get("name", ""),
        "image":    image,
        "duration": t.get("duration_ms", 0) // 1000,
        "search":   f"{t['name']} {artists}",
        "lyrics":   "",
    }


def spotify_track(url: str) -> list[dict]:
    sp = _spotify()
    if not sp: return []
    m = _SP_TRACK.search(url)
    if not m: return []
    try:
        return [_build_track(sp.track(m.group(1)))]
    except Exception as e:
        log.error(f"Spotify track error: {e}")
        return []


def spotify_album(url: str) -> list[dict]:
    sp = _spotify()
    if not sp: return []
    m = _SP_ALBUM.search(url)
    if not m: return []
    try:
        album  = sp.album(m.group(1))
        tracks = sp.album_tracks(m.group(1))["items"]
        return [_build_track(t, album) for t in tracks[:MAX_PLAYLIST_SONGS]]
    except Exception as e:
        log.error(f"Spotify album error: {e}")
        return []


def spotify_playlist(url: str) -> list[dict]:
    sp = _spotify()
    if not sp: return []
    m = _SP_PLAYLIST.search(url)
    if not m: return []
    try:
        items = sp.playlist_items(m.group(1), limit=MAX_PLAYLIST_SONGS)["items"]
        return [_build_track(i["track"]) for i in items if i.get("track")]
    except Exception as e:
        log.error(f"Spotify playlist error: {e}")
        return []


def spotify_artist(url: str) -> list[dict]:
    sp = _spotify()
    if not sp: return []
    m = _SP_ARTIST.search(url)
    if not m: return []
    try:
        tracks = sp.artist_top_tracks(m.group(1))["tracks"]
        return [_build_track(t) for t in tracks[:10]]
    except Exception as e:
        log.error(f"Spotify artist error: {e}")
        return []


# ── yt-dlp download ───────────────────────────────────────────────────────────
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
            "preferredquality": quality,   # exact 128 or 320
        }],
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/124 Safari/537.36"
            )
        },
        "retries": 3,
        "socket_timeout": 30,
    }


async def download_yt(
    url_or_query: str,
    meta: dict | None = None,
    quality: str = "320",
) -> str | None:
    """
    Download audio from YouTube URL or search query.
    quality: "128" or "320"
    Returns final .mp3 path or None on failure.
    """
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    if meta and (meta.get("artist") or meta.get("title")):
        base = _safe_filename(f"{meta.get('artist','')} - {meta.get('title','')}")
    else:
        base = _safe_filename(url_or_query[:60])

    out_tmpl   = os.path.join(DOWNLOAD_DIR, f"{base}_{quality}kbps.%(ext)s")
    final_path = os.path.join(DOWNLOAD_DIR, f"{base}_{quality}kbps.mp3")

    if os.path.exists(final_path):
        return final_path

    # Wrap plain text as YouTube search
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
        log.error(f"yt-dlp error for '{target}': {e}")
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
