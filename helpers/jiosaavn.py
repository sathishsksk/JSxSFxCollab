"""
helpers/jiosaavn.py — JioSaavn downloader
API: cyberboysumanjay/JioSaavnAPI (saavnapi-nine.vercel.app)
Response fields: title, singers, album, image_url, url, duration, lyrics
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
    if "jiosaavn.com" not in text: return None
    if SONG_RE.search(text):       return "song"
    if ALBUM_RE.search(text):      return "album"
    if PLAYLIST_RE.search(text):   return "playlist"
    return "song"


async def _api(endpoint: str, query: str, lyrics: bool = True):
    url    = f"{JIOSAAVN_API.rstrip('/')}/{endpoint}/"
    params = {"query": query}
    if lyrics: params["lyrics"] = "true"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, params=params,
                             timeout=aiohttp.ClientTimeout(total=30)) as r:
                if r.status != 200:
                    log.error(f"JioSaavn {r.status} {endpoint}?query={query}")
                    return None
                return await r.json(content_type=None)
    except Exception as e:
        log.error(f"JioSaavn API error: {e}")
        return None


def _clean(s): return re.sub(r"<[^>]+>", "", str(s)).strip()


def _parse(raw: dict, quality: str = "320") -> dict | None:
    if not isinstance(raw, dict): return None
    url = raw.get("url") or raw.get("media_url") or ""
    if not url or not isinstance(url, str): return None
    if url.startswith("//"): url = "https:" + url
    img = raw.get("image_url") or raw.get("image") or ""
    if img.startswith("//"): img = "https:" + img
    return {
        "title":    _clean(raw.get("title") or raw.get("song") or "Unknown"),
        "artist":   _clean(raw.get("singers") or raw.get("primary_artists") or ""),
        "album":    _clean(raw.get("album") or ""),
        "image":    img,
        "mp3_url":  url,
        "duration": int(raw.get("duration") or 0),
        "lyrics":   raw.get("lyrics") or "",
        "quality":  quality,
        "source":   "jiosaavn",
    }


def _to_list(data) -> list:
    if data is None:           return []
    if isinstance(data, list): return data
    if isinstance(data, dict):
        if "songs" in data:    return data["songs"] if isinstance(data["songs"], list) else []
        if data.get("title") or data.get("song"): return [data]
    return []


# ── URL fetchers ──────────────────────────────────────────────────────────────
async def fetch_song(url, quality="320"):
    return [s for s in (_parse(r, quality) for r in _to_list(await _api("song", url))) if s]

async def fetch_album(url, quality="320"):
    return [s for s in (_parse(r, quality) for r in _to_list(await _api("album", url))) if s]

async def fetch_playlist(url, quality="320"):
    return [s for s in (_parse(r, quality) for r in _to_list(await _api("playlist", url))) if s]


# ── Search ────────────────────────────────────────────────────────────────────
async def search_songs(query: str, quality: str = "320", limit: int = MAX_SEARCH_RESULTS) -> list[dict]:
    data = await _api("result", query)
    songs = [s for s in (_parse(r, quality) for r in _to_list(data)) if s]
    return songs[:limit]


async def search_albums(query: str, limit: int = 10) -> list[dict]:
    """
    Search JioSaavn for albums matching query.
    Returns list of album-dicts: {title, artist, image, album_query}
    where album_query is used to fetch all songs in that album.
    """
    data = await _api("result", query)
    songs = [s for s in (_parse(r) for r in _to_list(data)) if s]
    # Group by album, deduplicate
    seen, albums = set(), []
    for s in songs:
        alb = s.get("album", "").strip()
        if alb and alb not in seen:
            seen.add(alb)
            albums.append({
                "title":       alb,
                "artist":      s.get("artist", ""),
                "image":       s.get("image", ""),
                "duration":    0,
                "source":      "jiosaavn",
                "result_type": "album",
                "album_query": f"{alb} {s.get('artist','')}".strip(),
            })
    return albums[:limit]


async def search_artists(query: str, limit: int = 10) -> list[dict]:
    """
    Search JioSaavn for artists matching query.
    Returns list of artist-dicts grouped by primary artist.
    """
    data = await _api("result", query)
    songs = [s for s in (_parse(r) for r in _to_list(data)) if s]
    seen, artists = set(), []
    for s in songs:
        art = s.get("artist", "").split(",")[0].strip()
        if art and art not in seen:
            seen.add(art)
            artists.append({
                "title":       art,
                "artist":      art,
                "image":       s.get("image", ""),
                "duration":    0,
                "source":      "jiosaavn",
                "result_type": "artist",
                "artist_query": art,
            })
    return artists[:limit]


# ── Download + re-encode ──────────────────────────────────────────────────────
def _safe_fn(s): return re.sub(r'[<>:"/\\|?*\n\r\t]', "", s).strip()[:80]


async def _ffmpeg(src, dest, bitrate):
    cmd = ["ffmpeg", "-y", "-i", src, "-vn", "-ar", "44100",
           "-ac", "2", "-b:a", f"{bitrate}k", "-f", "mp3", dest]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
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
