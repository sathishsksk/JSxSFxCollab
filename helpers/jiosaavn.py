"""
helpers/jiosaavn.py

Based on cyberboysumanjay/JioSaavnAPI (saavnapi-nine.vercel.app)

Exact API response fields:
    title       — song name
    singers     — artist names (comma separated)
    album       — album name
    image_url   — 500x500 thumbnail URL
    url         — direct MP3 download URL
    duration    — seconds as string
    lyrics      — lyrics text (only when &lyrics=true)

Endpoints:
    /result/?query=<name-or-jiosaavn-url>&lyrics=true   ← universal
    /song/?query=<jiosaavn-song-url>&lyrics=true
    /album/?query=<jiosaavn-album-url>&lyrics=true
    /playlist/?query=<jiosaavn-playlist-url>&lyrics=true
"""

import os
import re
import asyncio
import logging
import aiohttp
import aiofiles
import subprocess

from config import JIOSAAVN_API, DOWNLOAD_DIR

log = logging.getLogger(__name__)

# ── URL detection ─────────────────────────────────────────────────────────────
SONG_RE     = re.compile(r"jiosaavn\.com/.+/song/")
ALBUM_RE    = re.compile(r"jiosaavn\.com/.+/album/")
PLAYLIST_RE = re.compile(r"jiosaavn\.com/featured/|jiosaavn\.com/s/playlist/")


def detect_jiosaavn(text: str) -> str | None:
    """Returns 'song', 'album', 'playlist' if JioSaavn URL, else None."""
    if "jiosaavn.com" not in text:
        return None
    if SONG_RE.search(text):     return "song"
    if ALBUM_RE.search(text):    return "album"
    if PLAYLIST_RE.search(text): return "playlist"
    # fallback — try as song
    return "song"


# ── API call ──────────────────────────────────────────────────────────────────
async def _api_get(endpoint: str, query: str, lyrics: bool = True) -> dict | list | None:
    """Call the JioSaavn API and return parsed JSON."""
    url    = f"{JIOSAAVN_API.rstrip('/')}/{endpoint}/"
    params = {"query": query}
    if lyrics:
        params["lyrics"] = "true"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url,
                params=params,
                timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                if resp.status != 200:
                    log.error(f"JioSaavn API {resp.status} for {url}?query={query}")
                    return None
                return await resp.json(content_type=None)
    except Exception as e:
        log.error(f"JioSaavn API error: {e}")
        return None


# ── Parse a single song dict from API response ────────────────────────────────
def _parse_one(raw: dict, quality: str) -> dict | None:
    """
    Parse one song dict from the API response.
    The API returns: title, singers, album, image_url, url, duration, lyrics
    """
    if not isinstance(raw, dict):
        return None

    # Get download URL — field name is 'url' in this API
    mp3_url = raw.get("url") or raw.get("media_url") or raw.get("320kbps") or ""
    if not mp3_url or not isinstance(mp3_url, str):
        log.warning(f"No download URL in song: {raw.get('title','?')} keys={list(raw.keys())}")
        return None

    # Fix protocol-relative URLs
    if mp3_url.startswith("//"):
        mp3_url = "https:" + mp3_url

    # Thumbnail — field name is 'image_url' in this API
    image = raw.get("image_url") or raw.get("image") or ""
    if image and not image.startswith("http"):
        image = "https:" + image if image.startswith("//") else ""

    return {
        "title":    _clean(raw.get("title") or raw.get("song") or "Unknown"),
        "artist":   _clean(raw.get("singers") or raw.get("primary_artists") or raw.get("artist") or "Unknown"),
        "album":    _clean(raw.get("album") or ""),
        "image":    image,
        "mp3_url":  mp3_url,
        "duration": int(raw.get("duration") or 0),
        "lyrics":   raw.get("lyrics") or "",
        "quality":  quality,
    }


def _clean(s: str) -> str:
    """Strip HTML tags and extra whitespace."""
    return re.sub(r"<[^>]+>", "", str(s)).strip()


# ── Normalize API response to list of raw song dicts ─────────────────────────
def _to_song_list(data) -> list[dict]:
    """
    The API can return:
      - a single song dict
      - {"songs": [...]}
      - a list of dicts
    """
    if data is None:
        return []
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        if "songs" in data and isinstance(data["songs"], list):
            return data["songs"]
        # single song
        if data.get("title") or data.get("song"):
            return [data]
    return []


# ── Public fetchers ───────────────────────────────────────────────────────────
async def fetch_song(url: str, quality: str = "320") -> list[dict]:
    data = await _api_get("song", url, lyrics=True)
    songs = []
    for raw in _to_song_list(data):
        parsed = _parse_one(raw, quality)
        if parsed:
            songs.append(parsed)
    return songs


async def fetch_album(url: str, quality: str = "320") -> list[dict]:
    data = await _api_get("album", url, lyrics=True)
    songs = []
    for raw in _to_song_list(data):
        parsed = _parse_one(raw, quality)
        if parsed:
            songs.append(parsed)
    return songs


async def fetch_playlist(url: str, quality: str = "320") -> list[dict]:
    data = await _api_get("playlist", url, lyrics=True)
    songs = []
    for raw in _to_song_list(data):
        parsed = _parse_one(raw, quality)
        if parsed:
            songs.append(parsed)
    return songs


async def search_song(query: str, quality: str = "320") -> list[dict]:
    """Search by song name using the universal /result/ endpoint."""
    data = await _api_get("result", query, lyrics=True)
    songs = []
    for raw in _to_song_list(data):
        parsed = _parse_one(raw, quality)
        if parsed:
            songs.append(parsed)
    return songs


# ── Download + FFmpeg re-encode ───────────────────────────────────────────────
def _safe_filename(s: str) -> str:
    return re.sub(r'[<>:"/\\|?*\n\r\t]', "", s).strip()[:80]


async def _ffmpeg_reencode(src: str, dest: str, bitrate: str):
    """Re-encode MP3 to exact bitrate using FFmpeg."""
    cmd = [
        "ffmpeg", "-y", "-i", src,
        "-vn", "-ar", "44100", "-ac", "2",
        "-b:a", f"{bitrate}k",
        "-f", "mp3", dest
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"FFmpeg failed: {stderr.decode()[-200:]}")


async def download_and_encode(song: dict) -> str:
    """
    1. Download raw MP3 from JioSaavn API url
    2. Re-encode to exact 128kbps or 320kbps via FFmpeg
    3. Return final MP3 path
    """
    quality  = song.get("quality", "320")
    name     = _safe_filename(f"{song['artist']} - {song['title']}")
    raw_path = os.path.join(DOWNLOAD_DIR, f"{name}_raw.mp3")
    out_path = os.path.join(DOWNLOAD_DIR, f"{name}_{quality}kbps.mp3")

    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    # Step 1 — Download
    if not os.path.exists(raw_path):
        log.info(f"Downloading: {song['mp3_url']}")
        async with aiohttp.ClientSession() as session:
            async with session.get(
                song["mp3_url"],
                timeout=aiohttp.ClientTimeout(total=300),
                headers={"User-Agent": "Mozilla/5.0"}
            ) as r:
                if r.status != 200:
                    raise RuntimeError(f"Download HTTP {r.status}")
                async with aiofiles.open(raw_path, "wb") as f:
                    async for chunk in r.content.iter_chunked(65536):
                        await f.write(chunk)

    # Step 2 — Re-encode
    if not os.path.exists(out_path):
        await _ffmpeg_reencode(raw_path, out_path, quality)

    # Step 3 — Clean raw
    try:
        os.remove(raw_path)
    except OSError:
        pass

    return out_path
