"""
helpers/spotify_handler.py

Spotify/YouTube via yt-dlp ONLY — no spotipy, no API keys, no 403 errors.

yt-dlp has a built-in Spotify extractor that reads track metadata from
Spotify's public pages and finds YouTube matches automatically.

Supports:
  - Spotify track / album / playlist / artist URLs
  - YouTube direct URLs
  - Song name search (ytsearch)
  - Lyrics via Genius (optional)
"""

import os
import re
import asyncio
import logging
import yt_dlp

from config import GENIUS_ACCESS_TOKEN, DOWNLOAD_DIR, MAX_PLAYLIST_SONGS, MAX_SEARCH_RESULTS

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


# ── yt-dlp metadata extract (no download) ────────────────────────────────────
def _extract(url: str, flat: bool = False) -> dict | None:
    opts = {
        "quiet":         True,
        "no_warnings":   True,
        "extract_flat":  flat,
        "skip_download": True,
        "noplaylist":    False,
        "ignoreerrors":  True,
        "socket_timeout": 25,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)
    except Exception as e:
        log.error(f"yt-dlp extract error for {url}: {e}")
        return None


def _to_track(e: dict) -> dict | None:
    if not e or not isinstance(e, dict):
        return None
    title  = e.get("track") or e.get("title") or "Unknown"
    artist = e.get("artist") or e.get("uploader") or e.get("creator") or ""
    album  = e.get("album") or ""
    dur    = int(e.get("duration") or 0)
    thumb  = e.get("thumbnail") or ""
    if not thumb and e.get("thumbnails"):
        thumb = e["thumbnails"][-1].get("url", "")
    search = f"{title} {artist}".strip() if artist else title
    return {
        "title":    title,
        "artist":   artist,
        "album":    album,
        "image":    thumb,
        "duration": dur,
        "search":   search,
        "lyrics":   "",
        "source":   "spotify",
    }


# ── Spotify fetchers ──────────────────────────────────────────────────────────
def spotify_track(url: str) -> tuple[list[dict], str | None]:
    info = _extract(url, flat=False)
    if not info:
        return [], "yt-dlp could not read Spotify track"
    t = _to_track(info)
    return ([t], None) if t else ([], "Could not parse track info")


def spotify_album(url: str) -> tuple[list[dict], str | None]:
    info = _extract(url, flat=True)
    if not info:
        return [], "yt-dlp could not read Spotify album"
    entries = info.get("entries") or []
    tracks  = [t for e in entries[:MAX_PLAYLIST_SONGS]
               for t in [_to_track(e)] if t]
    return (tracks, None) if tracks else ([], "No tracks found in album")


def spotify_playlist(url: str) -> tuple[list[dict], str | None]:
    info = _extract(url, flat=True)
    if not info:
        return [], "yt-dlp could not read Spotify playlist"
    entries = info.get("entries") or []
    tracks  = [t for e in entries[:MAX_PLAYLIST_SONGS]
               for t in [_to_track(e)] if t]
    return (tracks, None) if tracks else ([], "No tracks found in playlist")


def spotify_artist(url: str) -> tuple[list[dict], str | None]:
    info = _extract(url, flat=True)
    if not info:
        return [], "yt-dlp could not read Spotify artist"
    entries = info.get("entries") or []
    tracks  = [t for e in entries[:10]
               for t in [_to_track(e)] if t]
    return (tracks, None) if tracks else ([], "No tracks found for artist")


# ── YouTube search — returns multiple results ─────────────────────────────────
async def search_youtube(query: str, limit: int = MAX_SEARCH_RESULTS) -> list[dict]:
    """
    Search YouTube and return up to `limit` results.
    Used for the search-results picker UI.
    """
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
        entries = info.get("entries") or []
        results = []
        for e in entries:
            if not e:
                continue
            title  = e.get("title") or "Unknown"
            artist = e.get("uploader") or e.get("channel") or ""
            dur    = int(e.get("duration") or 0)
            yt_url = e.get("url") or e.get("webpage_url") or f"https://youtu.be/{e.get('id','')}"
            results.append({
                "title":    title,
                "artist":   artist,
                "album":    "",
                "image":    e.get("thumbnail") or "",
                "duration": dur,
                "search":   yt_url,   # use direct URL for download
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

    loop = asyncio.get_event_loop()
    try:
        def _run():
            with yt_dlp.YoutubeDL(_ydl_opts(out_tmpl, quality)) as ydl:
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
