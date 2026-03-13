"""
JioSaavn downloader using your own Vercel API (saavnapi-nine.vercel.app).
Supports: song URL, album URL, playlist URL.
Quality: Always downloads 320kbps source from API, then FFmpeg re-encodes
         to EXACT 128kbps or 320kbps as requested by user.
Embeds:  thumbnail, title, artist, album, lyrics into MP3.
"""

import os
import re
import asyncio
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


def _best_source_url(song: dict) -> str:
    """
    Always pick the BEST available quality from API as source.
    FFmpeg will re-encode to exact 128kbps or 320kbps after download.
    JioSaavn API fields: 320kbps > media_url > 160kbps > 96kbps
    """
    return (
        _fix_url(song.get("320kbps", ""))
        or _fix_url(song.get("media_url", ""))
        or _fix_url(song.get("160kbps", ""))
        or _fix_url(song.get("96kbps", ""))
    )


def _parse_songs(raw: list, quality: str = "320") -> list[dict]:
    result = []
    for s in raw:
        if not isinstance(s, dict):
            continue
        dl_url = _best_source_url(s)
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
            "quality":      quality,   # target quality for FFmpeg re-encode
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


# ── FFmpeg re-encode ──────────────────────────────────────────────────────────
async def _reencode_mp3(src: str, dest: str, bitrate: str):
    """
    Re-encode src MP3 to exact bitrate (128 or 320) using FFmpeg.
    src  — raw downloaded file (any format/bitrate)
    dest — output MP3 at exact bitrate
    """
    cmd = [
        "ffmpeg", "-y",
        "-i", src,
        "-vn",                    # no video
        "-ar", "44100",           # sample rate
        "-ac", "2",               # stereo
        "-b:a", f"{bitrate}k",    # exact bitrate: 128k or 320k
        "-f", "mp3",
        dest
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"FFmpeg re-encode failed (exit {proc.returncode})")


# ── Downloader + tagger ───────────────────────────────────────────────────────
def _safe_name(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*\n\r]', "", name).strip()[:80]


async def download_song(song: dict) -> str:
    """
    Downloads JioSaavn song at best quality,
    re-encodes to EXACT 128kbps or 320kbps via FFmpeg,
    embeds thumbnail + metadata + lyrics.
    Returns final MP3 path.
    """
    quality  = song.get("quality", "320")
    name     = _safe_name(f"{song['artist']} - {song['title']}")
    raw_path = os.path.join(DOWNLOAD_DIR, f"{name}_raw.mp3")
    dest     = os.path.join(DOWNLOAD_DIR, f"{name}_{quality}kbps.mp3")

    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    # Step 1 — Download raw best-quality file
    if not os.path.exists(raw_path):
        async with aiohttp.ClientSession() as session:
            async with session.get(
                song["download_url"],
                timeout=aiohttp.ClientTimeout(total=180)
            ) as r:
                r.raise_for_status()
                async with aiofiles.open(raw_path, "wb") as f:
                    async for chunk in r.content.iter_chunked(1024 * 64):
                        await f.write(chunk)

    # Step 2 — FFmpeg re-encode to EXACT requested bitrate
    if not os.path.exists(dest):
        await _reencode_mp3(raw_path, dest, quality)

    # Step 3 — Clean up raw file
    try:
        os.remove(raw_path)
    except Exception:
        pass

    # Step 4 — Embed thumbnail + metadata + lyrics
    await embed_metadata(dest, song, lyrics=song.get("lyrics") or None)

    return dest
