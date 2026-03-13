"""
JioSaavn downloader using your own Vercel API (saavnapi-nine.vercel.app).
Supports: song URL, album URL, playlist URL.
Embeds: thumbnail, title, artist, album, lyrics into MP3.
"""

import os
import re
import logging
import aiohttp
import aiofiles

from config import JIOSAAVN_API, DOWNLOAD_DIR
from helpers.tagger import embed_metadata

log = logging.getLogger(__name__)

# ── URL patterns ──────────────────────────────────────────────────────────────
SONG_RE     = re.compile(r"jiosaavn\.com/.+/song/")
ALBUM_RE    = re.compile(r"jiosaavn\.com/.+/album/")
PLAYLIST_RE = re.compile(r"jiosaavn\.com/featured/")


def detect_jiosaavn(text: str) -> str | None:
    if "jiosaavn.com" not in text:
        return None
    if SONG_RE.search(text):     return "song"
    if ALBUM_RE.search(text):    return "album"
    if PLAYLIST_RE.search(text): return "playlist"
    return "song"


# ── API helpers ───────────────────────────────────────────────────────────────
async def _get(session: aiohttp.ClientSession, endpoint: str, params: dict) -> dict:
    url = f"{JIOSAAVN_API.rstrip('/')}/{endpoint}"
    async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=30)) as r:
        r.raise_for_status()
        return await r.json()


def _fix_url(u: str) -> str:
    if not u:
        return ""
    return "https:" + u if u.startswith("//") else u


def _pick_quality_url(song: dict, quality: str) -> str:
    if quality == "128":
        return (
            _fix_url(song.get("160kbps", ""))
            or _fix_url(song.get("96kbps", ""))
            or _fix_url(song.get("media_url", ""))
        )
    return (
        _fix_url(song.get("320kbps", ""))
        or _fix_url(song.get("media_url", ""))
        or _fix_url(song.get("160kbps", ""))
    )


def _parse_songs(raw: list, quality: str = "320") -> list[dict]:
    result = []
    for s in raw:
        if not isinstance(s, dict):
            continue
        dl_url = _pick_quality_url(s, quality)
        if not dl_url:
            continue
        result.append({
            "title":        s.get("song") or s.get("title") or "Unknown",
            "artist":       s.get("primary_artists") or s.get("artist") or "Unknown",
            "album":        s.get("album") or "",
            "image":        (s.get("image") or "").replace("150x150", "500x500"),
            "download_url": dl_url,
            "duration":     int(s.get("duration", 0)),
            "lyrics":       s.get("lyrics") or "",
            "quality":      quality,
            "source":       "jiosaavn",
        })
    return result


# ── Public fetchers ───────────────────────────────────────────────────────────
async def fetch_song(url: str, quality: str = "320") -> list[dict]:
    async with aiohttp.ClientSession() as session:
        data = await _get(session, "song", {"query": url})
        songs = data.get("songs") or [data]
        return _parse_songs(songs, quality)


async def fetch_album(url: str, quality: str = "320") -> list[dict]:
    async with aiohttp.ClientSession() as session:
        data = await _get(session, "album", {"query": url})
        return _parse_songs(data.get("songs", []), quality)


async def fetch_playlist(url: str, quality: str = "320") -> list[dict]:
    async with aiohttp.ClientSession() as session:
        data = await _get(session, "playlist", {"query": url})
        return _parse_songs(data.get("songs", []), quality)


# ── Downloader + tagger ───────────────────────────────────────────────────────
def _safe_name(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*\n\r]', "", name).strip()[:80]


async def download_song(song: dict) -> str:
    """Downloads JioSaavn song, embeds full metadata + lyrics. Returns local mp3 path."""
    quality = song.get("quality", "320")
    name    = _safe_name(f"{song['artist']} - {song['title']}_{quality}kbps")
    dest    = os.path.join(DOWNLOAD_DIR, f"{name}.mp3")

    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    if not os.path.exists(dest):
        async with aiohttp.ClientSession() as session:
            async with session.get(
                song["download_url"],
                timeout=aiohttp.ClientTimeout(total=180)
            ) as r:
                r.raise_for_status()
                async with aiofiles.open(dest, "wb") as f:
                    async for chunk in r.content.iter_chunked(1024 * 64):
                        await f.write(chunk)

    # Embed thumbnail + metadata + lyrics
    await embed_metadata(dest, song, lyrics=song.get("lyrics") or None)

    return dest
