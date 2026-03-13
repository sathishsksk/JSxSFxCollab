"""
helpers/jiosaavn.py

cyberboysumanjay/JioSaavnAPI (saavnapi-nine.vercel.app)

Endpoints:
  GET /result/?query=<name-or-url>&lyrics=true   ← universal (returns list for search)
  GET /song/?query=<url>&lyrics=true
  GET /album/?query=<url>&lyrics=true
  GET /playlist/?query=<url>&lyrics=true

Response fields per song:
  title, singers, album, image_url, url (mp3), duration, lyrics
"""

import os
import re
import asyncio
import logging
import aiohttp
import aiofiles

from config import JIOSAAVN_API, DOWNLOAD_DIR, MAX_SEARCH_RESULTS

log = logging.getLogger(__name__)

SONG_RE     = re.compile(r"jiosaavn\.com/.+/song/")
ALBUM_RE    = re.compile(r"jiosaavn\.com/.+/album/")
PLAYLIST_RE = re.compile(r"jiosaavn\.com/featured/|jiosaavn\.com/s/playlist/")


def detect_jiosaavn(text: str) -> str | None:
    if "jiosaavn.com" not in text:
        return None
    if SONG_RE.search(text):     return "song"
    if ALBUM_RE.search(text):    return "album"
    if PLAYLIST_RE.search(text): return "playlist"
    return "song"


# ── API call ──────────────────────────────────────────────────────────────────
async def _api_get(endpoint: str, query: str, lyrics: bool = True):
    url    = f"{JIOSAAVN_API.rstrip('/')}/{endpoint}/"
    params = {"query": query}
    if lyrics:
        params["lyrics"] = "true"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params,
                                   timeout=aiohttp.ClientTimeout(total=30)) as r:
                if r.status != 200:
                    log.error(f"JioSaavn API {r.status} for {endpoint}?query={query}")
                    return None
                return await r.json(content_type=None)
    except Exception as e:
        log.error(f"JioSaavn API error: {e}")
        return None


def _clean(s: str) -> str:
    return re.sub(r"<[^>]+>", "", str(s)).strip()


def _parse_one(raw: dict, quality: str = "320") -> dict | None:
    if not isinstance(raw, dict):
        return None
    # The API returns 'url' as the mp3 download link
    mp3_url = raw.get("url") or raw.get("media_url") or ""
    if not mp3_url or not isinstance(mp3_url, str):
        return None
    if mp3_url.startswith("//"):
        mp3_url = "https:" + mp3_url

    image = raw.get("image_url") or raw.get("image") or ""
    if image.startswith("//"):
        image = "https:" + image

    return {
        "title":    _clean(raw.get("title") or raw.get("song") or "Unknown"),
        "artist":   _clean(raw.get("singers") or raw.get("primary_artists") or raw.get("artist") or ""),
        "album":    _clean(raw.get("album") or ""),
        "image":    image,
        "mp3_url":  mp3_url,
        "duration": int(raw.get("duration") or 0),
        "lyrics":   raw.get("lyrics") or "",
        "quality":  quality,
        "source":   "jiosaavn",
    }


def _to_song_list(data) -> list[dict]:
    if data is None:              return []
    if isinstance(data, list):    return data
    if isinstance(data, dict):
        if "songs" in data:       return data["songs"] if isinstance(data["songs"], list) else []
        if data.get("title") or data.get("song"): return [data]
    return []


# ── Fetch by URL ──────────────────────────────────────────────────────────────
async def fetch_song(url: str, quality: str = "320") -> list[dict]:
    data = await _api_get("song", url)
    return [s for s in (_parse_one(r, quality) for r in _to_song_list(data)) if s]


async def fetch_album(url: str, quality: str = "320") -> list[dict]:
    data = await _api_get("album", url)
    return [s for s in (_parse_one(r, quality) for r in _to_song_list(data)) if s]


async def fetch_playlist(url: str, quality: str = "320") -> list[dict]:
    data = await _api_get("playlist", url)
    return [s for s in (_parse_one(r, quality) for r in _to_song_list(data)) if s]


# ── Search — returns multiple results ────────────────────────────────────────
async def search_songs(query: str, quality: str = "320", limit: int = MAX_SEARCH_RESULTS) -> list[dict]:
    """
    Search JioSaavn by name.
    Returns up to `limit` results so the user can pick one.
    """
    data = await _api_get("result", query)
    songs = [s for s in (_parse_one(r, quality) for r in _to_song_list(data)) if s]
    return songs[:limit]


# ── Download + FFmpeg re-encode ───────────────────────────────────────────────
def _safe_fn(s: str) -> str:
    return re.sub(r'[<>:"/\\|?*\n\r\t]', "", s).strip()[:80]


async def _ffmpeg(src: str, dest: str, bitrate: str):
    cmd = ["ffmpeg", "-y", "-i", src, "-vn", "-ar", "44100",
           "-ac", "2", "-b:a", f"{bitrate}k", "-f", "mp3", dest]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"FFmpeg: {stderr.decode()[-300:]}")


async def download_and_encode(song: dict) -> str:
    quality  = song.get("quality", "320")
    name     = _safe_fn(f"{song['artist']} - {song['title']}")
    raw_path = os.path.join(DOWNLOAD_DIR, f"{name}_raw.mp3")
    out_path = os.path.join(DOWNLOAD_DIR, f"{name}_{quality}kbps.mp3")
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    if not os.path.exists(raw_path):
        async with aiohttp.ClientSession() as s:
            async with s.get(song["mp3_url"],
                             timeout=aiohttp.ClientTimeout(total=300),
                             headers={"User-Agent": "Mozilla/5.0"}) as r:
                if r.status != 200:
                    raise RuntimeError(f"Download HTTP {r.status}")
                async with aiofiles.open(raw_path, "wb") as f:
                    async for chunk in r.content.iter_chunked(65536):
                        await f.write(chunk)

    if not os.path.exists(out_path):
        await _ffmpeg(raw_path, out_path, quality)

    try: os.remove(raw_path)
    except OSError: pass

    return out_path
