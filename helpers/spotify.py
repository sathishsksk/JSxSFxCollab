"""
helpers/spotify.py
Spotify: get metadata via spotipy → download via yt-dlp + FFmpeg
YouTube: download directly via yt-dlp + FFmpeg
Lyrics:  Genius API (optional)
"""

import os
import re
import asyncio
import logging

from config import (
    SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET,
    GENIUS_ACCESS_TOKEN, DOWNLOAD_DIR, MAX_PLAYLIST_SONGS
)

log = logging.getLogger(__name__)

# ── Spotify client ────────────────────────────────────────────────────────────
_sp = None
_sp_error = None   # store init error so we can show it to user


def _spotify():
    global _sp, _sp_error
    if _sp is not None:
        return _sp
    if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
        _sp_error = (
            "SPOTIFY_CLIENT_ID or SPOTIFY_CLIENT_SECRET is not set in env vars.\n"
            "Get them from: developer.spotify.com/dashboard"
        )
        return None
    try:
        import spotipy
        from spotipy.oauth2 import SpotifyClientCredentials
        _sp = spotipy.Spotify(
            auth_manager=SpotifyClientCredentials(
                client_id=SPOTIFY_CLIENT_ID,
                client_secret=SPOTIFY_CLIENT_SECRET,
            ),
            requests_timeout=15,
        )
        # Test the connection immediately
        _sp.search("test", limit=1, type="track")
        _sp_error = None
        log.info("✅ Spotify client initialized and tested OK")
        return _sp
    except Exception as e:
        _sp_error = str(e)
        _sp = None
        log.error(f"Spotify init/test failed: {e}")
        return None


def spotify_error() -> str | None:
    """Return the last Spotify init error, or None if OK."""
    _spotify()   # attempt init if not done yet
    return _sp_error


# ── URL detection ─────────────────────────────────────────────────────────────
_SP_TRACK    = re.compile(r"open\.spotify\.com/(?:[a-z]{2}/)?(?:intl-[a-z]+/)?track/([A-Za-z0-9]+)")
_SP_ALBUM    = re.compile(r"open\.spotify\.com/(?:[a-z]{2}/)?(?:intl-[a-z]+/)?album/([A-Za-z0-9]+)")
_SP_PLAYLIST = re.compile(r"open\.spotify\.com/(?:[a-z]{2}/)?(?:intl-[a-z]+/)?playlist/([A-Za-z0-9]+)")
_SP_ARTIST   = re.compile(r"open\.spotify\.com/(?:[a-z]{2}/)?(?:intl-[a-z]+/)?artist/([A-Za-z0-9]+)")
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


# ── Spotify metadata ──────────────────────────────────────────────────────────
def _build_track(t: dict, album: dict | None = None) -> dict:
    artists = ", ".join(a["name"] for a in t.get("artists", []))
    alb     = album or t.get("album") or {}
    images  = alb.get("images") or []
    image   = images[0]["url"] if images else ""
    return {
        "title":    t.get("name", "Unknown"),
        "artist":   artists or "Unknown",
        "album":    alb.get("name", ""),
        "image":    image,
        "duration": (t.get("duration_ms") or 0) // 1000,
        "search":   f"{t.get('name', '')} {artists}",
        "lyrics":   "",
    }


def spotify_track(url: str) -> tuple[list[dict], str | None]:
    """Returns (tracks, error_message)"""
    sp = _spotify()
    if not sp:
        return [], _sp_error or "Spotify client failed to initialize"
    m = _SP_TRACK.search(url)
    if not m:
        return [], "Could not parse Spotify track URL"
    try:
        return [_build_track(sp.track(m.group(1)))], None
    except Exception as e:
        return [], f"Spotify API error: {e}"


def spotify_album(url: str) -> tuple[list[dict], str | None]:
    sp = _spotify()
    if not sp:
        return [], _sp_error or "Spotify client failed to initialize"
    m = _SP_ALBUM.search(url)
    if not m:
        return [], "Could not parse Spotify album URL"
    try:
        album  = sp.album(m.group(1))
        tracks = sp.album_tracks(m.group(1))["items"]
        return [_build_track(t, album) for t in tracks[:MAX_PLAYLIST_SONGS]], None
    except Exception as e:
        return [], f"Spotify API error: {e}"


def spotify_playlist(url: str) -> tuple[list[dict], str | None]:
    sp = _spotify()
    if not sp:
        return [], _sp_error or "Spotify client failed to initialize"
    m = _SP_PLAYLIST.search(url)
    if not m:
        return [], "Could not parse Spotify playlist URL"
    try:
        items = sp.playlist_items(m.group(1), limit=MAX_PLAYLIST_SONGS)["items"]
        tracks = [_build_track(i["track"]) for i in items if i.get("track")]
        return tracks, None
    except Exception as e:
        return [], f"Spotify API error: {e}"


def spotify_artist(url: str) -> tuple[list[dict], str | None]:
    sp = _spotify()
    if not sp:
        return [], _sp_error or "Spotify client failed to initialize"
    m = _SP_ARTIST.search(url)
    if not m:
        return [], "Could not parse Spotify artist URL"
    try:
        tracks = sp.artist_top_tracks(m.group(1))["tracks"]
        return [_build_track(t) for t in tracks[:10]], None
    except Exception as e:
        return [], f"Spotify API error: {e}"


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


async def download_yt(
    url_or_query: str,
    meta: dict | None = None,
    quality: str = "320",
) -> str | None:
    import yt_dlp
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
