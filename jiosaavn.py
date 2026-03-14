"""
helpers/jiosaavn.py — JioSaavn downloader
API: cyberboysumanjay/JioSaavnAPI (saavnapi-nine.vercel.app)
Response fields: title, singers, album, image_url, url, duration, lyrics

NOTE on search results:
  The /result/ endpoint caps at ~5 results per query.
  To get more results we run multiple parallel queries (title variations,
  artist-focused, etc.) and deduplicate by title+artist.
"""

import os
import re
import asyncio
import logging
import aiohttp
import aiofiles

from config import JIOSAAVN_API, DOWNLOAD_DIR

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
            async with s.get(url, params=params, timeout=aiohttp.ClientTimeout(total=20)) as r:
                if r.status != 200:
                    log.error(f"JioSaavn {r.status} {endpoint}?query={query}")
                    return None
                return await r.json(content_type=None)
    except Exception as e:
        log.error(f"JioSaavn API error: {e}")
        return None


def _clean(s): return re.sub(r"<[^>]+>", "", str(s)).strip()


def _clean_lyrics(raw: str) -> str:
    """
    Convert JioSaavn HTML lyrics to plain text with proper line breaks.
    JioSaavn returns lyrics like:
      "Line one<br>Line two<br><br>Verse 2<br>Line one"
    Also handles: &amp; &quot; &#39; &nbsp; <p> tags etc.
    """
    if not raw:
        return ""
    s = raw
    # Convert <br>, <br/>, <p>, </p> to newlines
    s = re.sub(r"<br\s*/?>",      "\n",  s, flags=re.I)
    s = re.sub(r"</p\s*>",        "\n",  s, flags=re.I)
    s = re.sub(r"<p[^>]*>",       "\n",  s, flags=re.I)
    s = re.sub(r"</?(?:div|li)[^>]*>", "\n", s, flags=re.I)
    # Strip all remaining HTML tags
    s = re.sub(r"<[^>]+>", "", s)
    # Decode HTML entities
    s = s.replace("&amp;",  "&")
    s = s.replace("&quot;", '"')
    s = s.replace("&#39;",  "'")
    s = s.replace("&nbsp;", " ")
    s = s.replace("&lt;",   "<")
    s = s.replace("&gt;",   ">")
    s = s.replace("&apos;", "'")
    # Collapse 3+ consecutive newlines to 2 (paragraph break)
    s = re.sub(r"\n{3,}", "\n\n", s)
    # Strip leading/trailing whitespace on each line
    lines = [line.strip() for line in s.splitlines()]
    return "\n".join(lines).strip()


def _max_image(url: str) -> str:
    """
    Force JioSaavn CDN image to highest quality (500×500).
    JioSaavn CDN pattern: https://c.saavncdn.com/.../Name-50x50.jpg
                                                         ^^^^^^ replace this part only
    Only replaces the trailing NxN before the file extension — safe, no false matches.
    """
    if not url:
        return url
    # Replace NxN immediately before .jpg/.jpeg/.png/.webp at end of path
    return re.sub(r"\d+x\d+(?=\.(jpg|jpeg|png|webp)(\?|$))", "500x500", url, flags=re.I)


def _parse(raw: dict, quality: str = "320") -> dict | None:
    if not isinstance(raw, dict): return None
    url = raw.get("url") or raw.get("media_url") or ""
    if not url or not isinstance(url, str): return None
    if url.startswith("//"): url = "https:" + url
    img = raw.get("image_url") or raw.get("image") or ""
    if img.startswith("//"): img = "https:" + img
    img = _max_image(img)   # always upgrade to 500x500
    return {
        "title":    _clean(raw.get("title") or raw.get("song") or "Unknown"),
        "artist":   _clean(raw.get("singers") or raw.get("primary_artists") or ""),
        "album":    _clean(raw.get("album") or ""),
        "image":    img,
        "mp3_url":  url,
        "duration": int(raw.get("duration") or 0),
        "lyrics":   _clean_lyrics(raw.get("lyrics") or ""),
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


# ── Multi-query search ────────────────────────────────────────────────────────
def _make_queries(query: str) -> list[str]:
    """
    Generate search query variations to maximise results from the API.
    The /result/ endpoint caps at ~5 per call, so we run multiple queries
    with different phrasings and merge+deduplicate.
    """
    q  = query.strip()
    qs = [q]

    parts = q.split()

    # First word only (often the song name)
    if len(parts) >= 2:
        qs.append(parts[0])

    # First two words
    if len(parts) >= 3:
        qs.append(" ".join(parts[:2]))

    # Last word (often artist name in "Song Artist" queries)
    if len(parts) >= 2:
        qs.append(parts[-1])

    # Without common suffixes
    clean = re.sub(r'\s*-\s*(from|feat|ft|remix|remaster|official)[^\-]*$', '', q, flags=re.I).strip()
    if clean and clean != q:
        qs.append(clean)

    # Deduplicate preserving order
    seen, result = set(), []
    for x in qs:
        if x and x.lower() not in seen:
            seen.add(x.lower())
            result.append(x)
    return result


async def _search_one(query: str, quality: str) -> list[dict]:
    data = await _api("result", query)
    return [s for s in (_parse(r, quality) for r in _to_list(data)) if s]


async def search_songs(query: str, quality: str = "320", limit: int = 30) -> list[dict]:
    """
    Search JioSaavn with multiple query variations in parallel.
    Deduplicates by (title, artist) and returns up to `limit` results.
    """
    queries = _make_queries(query)

    # Run all queries in parallel
    tasks   = [_search_one(q, quality) for q in queries]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    merged, seen = [], set()
    for batch in results:
        if isinstance(batch, Exception): continue
        for s in batch:
            key = (s["title"].lower().strip(), s["artist"].lower().strip())
            if key not in seen:
                seen.add(key)
                merged.append(s)

    return merged[:limit]


async def search_albums(query: str, limit: int = 20) -> list[dict]:
    queries = _make_queries(query)
    tasks   = [_search_one(q) for q in queries]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    seen, albums = set(), []
    for batch in results:
        if isinstance(batch, Exception): continue
        for s in batch:
            alb = s.get("album", "").strip()
            if alb and alb.lower() not in seen:
                seen.add(alb.lower())
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


async def search_artists(query: str, limit: int = 20) -> list[dict]:
    queries = _make_queries(query)
    tasks   = [_search_one(q) for q in queries]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    seen, artists = set(), []
    for batch in results:
        if isinstance(batch, Exception): continue
        for s in batch:
            art = s.get("artist", "").split(",")[0].strip()
            if art and art.lower() not in seen:
                seen.add(art.lower())
                artists.append({
                    "title":        art,
                    "artist":       art,
                    "image":        s.get("image", ""),
                    "duration":     0,
                    "source":       "jiosaavn",
                    "result_type":  "artist",
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
