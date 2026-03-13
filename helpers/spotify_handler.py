"""
Spotify + YouTube downloader.
- Spotify URLs  → metadata via spotipy → download via yt-dlp → embed metadata
- YouTube URLs  → download via yt-dlp → embed metadata
- Song name     → YouTube search via yt-dlp → embed metadata
Quality: "128" or "320" passed to FFmpeg.
"""

import os
import re
import logging
import asyncio

import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
import yt_dlp

from config import (
    SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET,
    GENIUS_ACCESS_TOKEN, DOWNLOAD_DIR, MAX_PLAYLIST_SONGS
)
from helpers.tagger import embed_metadata

log = logging.getLogger(__name__)

# ── Spotify client ────────────────────────────────────────────────────────────
_sp: spotipy.Spotify | None = None


def get_spotify() -> spotipy.Spotify | None:
    global _sp
    if _sp is None and SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET:
        _sp = spotipy.Spotify(
            auth_manager=SpotifyClientCredentials(
                client_id=SPOTIFY_CLIENT_ID,
                client_secret=SPOTIFY_CLIENT_SECRET,
            )
        )
    return _sp


# ── URL detectors ─────────────────────────────────────────────────────────────
SPOTIFY_TRACK_RE    = re.compile(r"open\.spotify\.com/track/([A-Za-z0-9]+)")
SPOTIFY_ALBUM_RE    = re.compile(r"open\.spotify\.com/album/([A-Za-z0-9]+)")
SPOTIFY_PLAYLIST_RE = re.compile(r"open\.spotify\.com/playlist/([A-Za-z0-9]+)")
SPOTIFY_ARTIST_RE   = re.compile(r"open\.spotify\.com/artist/([A-Za-z0-9]+)")
YOUTUBE_RE          = re.compile(r"(youtube\.com/watch|youtu\.be/)")


def detect_spotify(url: str) -> str | None:
    if "spotify.com" not in url:
        return None
    if SPOTIFY_TRACK_RE.search(url):    return "track"
    if SPOTIFY_ALBUM_RE.search(url):    return "album"
    if SPOTIFY_PLAYLIST_RE.search(url): return "playlist"
    if SPOTIFY_ARTIST_RE.search(url):   return "artist"
    return None


def is_youtube(url: str) -> bool:
    return bool(YOUTUBE_RE.search(url))


# ── Spotify metadata ──────────────────────────────────────────────────────────
def _track_to_dict(t: dict, album_override: dict | None = None) -> dict:
    artists = ", ".join(a["name"] for a in t.get("artists", []))
    album   = album_override or t.get("album", {})
    images  = album.get("images", [])
    image   = images[0].get("url", "") if images else ""
    return {
        "title":    t["name"],
        "artist":   artists,
        "album":    album.get("name", ""),
        "image":    image,
        "duration": t.get("duration_ms", 0) // 1000,
        "search":   f"{t['name']} {artists}",
        "source":   "spotify",
    }


def get_track_info(url: str) -> list[dict]:
    sp = get_spotify()
    if not sp: return []
    m = SPOTIFY_TRACK_RE.search(url)
    return [_track_to_dict(sp.track(m.group(1)))] if m else []


def get_album_info(url: str) -> list[dict]:
    sp = get_spotify()
    if not sp: return []
    m = SPOTIFY_ALBUM_RE.search(url)
    if not m: return []
    album  = sp.album(m.group(1))
    tracks = sp.album_tracks(m.group(1))["items"]
    return [_track_to_dict(t, album_override=album) for t in tracks[:MAX_PLAYLIST_SONGS]]


def get_playlist_info(url: str) -> list[dict]:
    sp = get_spotify()
    if not sp: return []
    m = SPOTIFY_PLAYLIST_RE.search(url)
    if not m: return []
    items = sp.playlist_items(m.group(1), limit=MAX_PLAYLIST_SONGS)["items"]
    return [_track_to_dict(item["track"]) for item in items if item.get("track")]


def get_artist_top_tracks(url: str) -> list[dict]:
    sp = get_spotify()
    if not sp: return []
    m = SPOTIFY_ARTIST_RE.search(url)
    if not m: return []
    return [_track_to_dict(t) for t in sp.artist_top_tracks(m.group(1))["tracks"][:10]]


# ── Genius lyrics ─────────────────────────────────────────────────────────────
async def get_lyrics(title: str, artist: str) -> str | None:
    if not GENIUS_ACCESS_TOKEN:
        return None
    try:
        import lyricsgenius
        genius = lyricsgenius.Genius(GENIUS_ACCESS_TOKEN, verbose=False, timeout=10)
        loop   = asyncio.get_event_loop()
        song   = await loop.run_in_executor(
            None, lambda: genius.search_song(title, artist)
        )
        if song:
            return song.lyrics[:4000]
    except Exception as e:
        log.warning(f"Genius error: {e}")
    return None


# ── yt-dlp downloader ─────────────────────────────────────────────────────────
def _safe_name(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*\n\r]', "", name).strip()[:80]


def _ydl_opts(out_template: str, quality: str = "320") -> dict:
    return {
        "format":       "bestaudio/best",
        "outtmpl":      out_template,
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
                "AppleWebKit/537.36 Chrome/120 Safari/537.36"
            )
        },
    }


async def download_from_youtube(
    url_or_query: str,
    meta: dict | None = None,
    quality: str = "320",
) -> str | None:
    """
    Downloads audio from YouTube URL or search query.
    Embeds full metadata + thumbnail + lyrics (via Genius if token set).
    Returns local .mp3 path or None on failure.
    """
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    base         = _safe_name(
        f"{meta.get('artist','')} - {meta.get('title','')}" if meta
        else url_or_query[:60]
    )
    name         = f"{base}_{quality}kbps"
    out_template = os.path.join(DOWNLOAD_DIR, f"{name}.%(ext)s")
    final_path   = os.path.join(DOWNLOAD_DIR, f"{name}.mp3")

    if not os.path.exists(final_path):
        query = url_or_query
        if not (query.startswith("http") or query.startswith("www")):
            query = f"ytsearch1:{query}"

        opts = _ydl_opts(out_template, quality=quality)
        loop = asyncio.get_event_loop()

        try:
            def _run():
                with yt_dlp.YoutubeDL(opts) as ydl:
                    ydl.download([query])
            await loop.run_in_executor(None, _run)
        except Exception as e:
            log.error(f"yt-dlp error '{query}': {e}")
            return None

        if not os.path.exists(final_path):
            return None

    # ── Embed metadata ────────────────────────────────────────────────────
    if meta:
        # Fetch lyrics from Genius if token is configured
        lyrics = await get_lyrics(
            meta.get("title", ""),
            meta.get("artist", "")
        )
        await embed_metadata(final_path, meta, lyrics=lyrics)

    return final_path
