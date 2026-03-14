"""
helpers/spotify_handler.py

Spotify — NO API KEYS, NO 403, ZERO DEPENDENCIES ON SPOTIFY API.

How it works:
  Spotify's public web pages contain a <script id="__NEXT_DATA__"> tag
  with full JSON track listings (title, artist, album, cover, duration).
  We scrape that JSON directly — no auth, no rate limits, no 403 ever.

  Audio is then downloaded via yt-dlp searching YouTube Music:
    "ytsearch1:{title} {artist} audio"

  This is exactly how public Spotify embed players work.
"""

import os
import re
import json
import asyncio
import logging
import unicodedata
from pathlib import Path

import aiohttp
import yt_dlp

from config import (
    GENIUS_ACCESS_TOKEN, DOWNLOAD_DIR,
    MAX_PLAYLIST_SONGS, MAX_SEARCH_RESULTS,
)

log = logging.getLogger(__name__)

# ── URL detection ─────────────────────────────────────────────────────────────
_SP_TRACK    = re.compile(r"open\.spotify\.com/(?:[a-z-]+/)?track/([A-Za-z0-9]+)")
_SP_ALBUM    = re.compile(r"open\.spotify\.com/(?:[a-z-]+/)?album/([A-Za-z0-9]+)")
_SP_PLAYLIST = re.compile(r"open\.spotify\.com/(?:[a-z-]+/)?playlist/([A-Za-z0-9]+)")
_SP_ARTIST   = re.compile(r"open\.spotify\.com/(?:[a-z-]+/)?artist/([A-Za-z0-9]+)")
_YT          = re.compile(r"(youtube\.com/watch|youtu\.be/|music\.youtube\.com)")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


def detect_spotify(text: str) -> str | None:
    if "spotify.com" not in text: return None
    if _SP_TRACK.search(text):    return "track"
    if _SP_ALBUM.search(text):    return "album"
    if _SP_PLAYLIST.search(text): return "playlist"
    if _SP_ARTIST.search(text):   return "artist"
    return None


def is_youtube(text: str) -> bool:
    return bool(_YT.search(text))


# ── Spotify web page scraper ──────────────────────────────────────────────────
async def _fetch_page(url: str) -> str | None:
    """Fetch raw HTML of a Spotify page."""
    try:
        async with aiohttp.ClientSession(headers=HEADERS) as s:
            async with s.get(
                url, timeout=aiohttp.ClientTimeout(total=20), allow_redirects=True
            ) as r:
                if r.status == 200:
                    return await r.text()
                log.error(f"Spotify page HTTP {r.status} for {url}")
    except Exception as e:
        log.error(f"Spotify page fetch error: {e}")
    return None


def _extract_next_data(html: str) -> dict | None:
    """Extract JSON from <script id="__NEXT_DATA__"> in Spotify pages."""
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except Exception:
        return None


def _img_url(images: list) -> str:
    """Get largest image URL from Spotify images array."""
    if not images:
        return ""
    # Sort by width descending, pick largest
    try:
        images = sorted(images, key=lambda x: x.get("width") or 0, reverse=True)
    except Exception:
        pass
    return images[0].get("url", "") if images else ""


def _track_entry(name: str, artists: list, album: str, image: str, duration_ms: int) -> dict:
    artist_str = ", ".join(a.get("name","") for a in artists)
    return {
        "title":    name,
        "artist":   artist_str,
        "album":    album,
        "image":    image,
        "duration": int(duration_ms / 1000),
        "source":   "spotify",
        "lyrics":   "",
    }


# ── Parse specific Spotify page types from __NEXT_DATA__ ─────────────────────
def _parse_track(data: dict) -> list[dict]:
    try:
        # Path varies — try multiple known paths
        paths = [
            ["props", "pageProps", "state", "data", "entity"],
            ["props", "pageProps", "track"],
        ]
        entity = None
        for path in paths:
            node = data
            for key in path:
                node = node.get(key, {}) if isinstance(node, dict) else {}
            if node and node.get("name"):
                entity = node
                break

        if not entity:
            # Fallback: search recursively for track entity
            entity = _find_entity(data, "track")

        if not entity:
            return []

        name     = entity.get("name", "Unknown")
        artists  = entity.get("artists", {})
        if isinstance(artists, dict):
            artists = artists.get("items", [])
        album    = entity.get("albumOfTrack", {}) or entity.get("album", {})
        album_name = album.get("name", "") if isinstance(album, dict) else ""
        images   = []
        if isinstance(album, dict):
            cover = album.get("coverArt", {}) or album.get("images", {})
            if isinstance(cover, dict):
                images = cover.get("sources", []) or cover.get("items", [])
        duration = entity.get("duration", {})
        if isinstance(duration, dict):
            duration = duration.get("totalMilliseconds", 0)
        elif not isinstance(duration, int):
            duration = 0

        return [_track_entry(name, artists, album_name, _img_url(images), duration)]
    except Exception as e:
        log.error(f"_parse_track error: {e}")
        return []


def _parse_album(data: dict) -> list[dict]:
    try:
        entity = _find_entity(data, "album")
        if not entity:
            return []

        album_name = entity.get("name", "")
        cover      = entity.get("coverArt", {}) or {}
        images     = cover.get("sources", []) if isinstance(cover, dict) else []
        image      = _img_url(images)

        tracks_node = (
            entity.get("tracks", {})
            or entity.get("tracklist", {})
        )
        items = []
        if isinstance(tracks_node, dict):
            items = tracks_node.get("items", [])

        results = []
        for item in items[:MAX_PLAYLIST_SONGS]:
            track = item.get("track", item) if isinstance(item, dict) else {}
            if not track:
                continue
            name    = track.get("name", "Unknown")
            artists = track.get("artists", {})
            if isinstance(artists, dict):
                artists = artists.get("items", [])
            dur = track.get("duration", {})
            if isinstance(dur, dict):
                dur = dur.get("totalMilliseconds", 0)
            results.append(_track_entry(name, artists, album_name, image, dur or 0))

        return results
    except Exception as e:
        log.error(f"_parse_album error: {e}")
        return []


def _parse_playlist(data: dict) -> list[dict]:
    try:
        entity = _find_entity(data, "playlist")
        if not entity:
            return []

        items = []
        content = entity.get("content", {}) or entity.get("tracks", {})
        if isinstance(content, dict):
            items = content.get("items", [])

        results = []
        for item in items[:MAX_PLAYLIST_SONGS]:
            if not isinstance(item, dict):
                continue
            track = item.get("itemV2", {}) or item.get("track", {})
            if isinstance(track, dict) and "data" in track:
                track = track["data"]
            if not track or not track.get("name"):
                continue
            name    = track.get("name", "Unknown")
            artists = track.get("artists", {})
            if isinstance(artists, dict):
                artists = artists.get("items", [])
            album   = track.get("albumOfTrack", {}) or {}
            album_name = album.get("name", "") if isinstance(album, dict) else ""
            cover   = album.get("coverArt", {}) or {}
            images  = cover.get("sources", []) if isinstance(cover, dict) else []
            dur     = track.get("duration", {})
            if isinstance(dur, dict):
                dur = dur.get("totalMilliseconds", 0)
            results.append(_track_entry(name, artists, album_name, _img_url(images), dur or 0))

        return results
    except Exception as e:
        log.error(f"_parse_playlist error: {e}")
        return []


def _find_entity(data: dict, kind: str) -> dict | None:
    """
    Recursively search __NEXT_DATA__ for an entity that looks like
    a Spotify track/album/playlist.
    """
    if not isinstance(data, dict):
        return None
    # Look for known keys that signal the entity root
    for key in ["entity", kind, "trackUnion", "albumUnion", "playlistV2"]:
        node = data.get(key)
        if isinstance(node, dict) and node.get("name"):
            return node
    for v in data.values():
        result = _find_entity(v, kind)
        if result:
            return result
    return None


# ── Public fetchers ───────────────────────────────────────────────────────────
async def spotify_scrape(url: str, kind: str) -> tuple[list[dict], str | None]:
    """
    Scrape Spotify web page for track metadata.
    Returns (tracks, error).
    """
    html = await _fetch_page(url)
    if not html:
        return [], "Could not fetch Spotify page. Check the URL or try again later."

    data = _extract_next_data(html)
    if not data:
        # Some pages don't have __NEXT_DATA__ — try oEmbed for single tracks
        if kind == "track":
            return await _oembed_fallback(url)
        return [], "Could not parse Spotify page. URL may be private or invalid."

    if kind == "track":
        tracks = _parse_track(data)
    elif kind == "album":
        tracks = _parse_album(data)
    elif kind == "playlist":
        tracks = _parse_playlist(data)
    elif kind == "artist":
        # For artist: scrape their discography page
        tracks = _parse_album(data) or _parse_playlist(data)
    else:
        tracks = []

    if not tracks:
        # Final fallback for tracks
        if kind == "track":
            return await _oembed_fallback(url)
        return [], f"No tracks found on Spotify page for this {kind} URL."

    return tracks, None


async def _oembed_fallback(url: str) -> tuple[list[dict], str | None]:
    """
    Use Spotify's free oEmbed endpoint as fallback for single tracks.
    Returns title + artist_name only (no album/cover).
    """
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                "https://open.spotify.com/oembed",
                params={"url": url},
                timeout=aiohttp.ClientTimeout(total=10),
                headers=HEADERS,
            ) as r:
                if r.status == 200:
                    d = await r.json(content_type=None)
                    title  = d.get("title", "Unknown")
                    artist = d.get("author_name", "")
                    image  = d.get("thumbnail_url", "")
                    return [{
                        "title":    title,
                        "artist":   artist,
                        "album":    "",
                        "image":    image,
                        "duration": 0,
                        "source":   "spotify",
                        "lyrics":   "",
                    }], None
    except Exception as e:
        log.warning(f"oEmbed fallback failed: {e}")
    return [], "Could not fetch Spotify track info. The link may be private."


# ── yt-dlp download (audio for Spotify tracks) ───────────────────────────────
def _safe_fn(s: str) -> str:
    return re.sub(r'[<>:"/\\|?*\n\r\t]', "", s).strip()[:80]


async def download_spotify_track(song: dict, quality: str = "320") -> str:
    """
    Find best YouTube Music match for a Spotify track and download it.
    Uses: "ytsearch1:{title} {artist} audio" on YouTube Music.
    """
    title  = song.get("title", "")
    artist = song.get("artist", "")

    # Try YouTube Music first (better quality matches), fallback to regular YouTube
    queries = [
        f"https://music.youtube.com/search?q={title} {artist}",   # YT Music search
        f"ytsearch1:{title} {artist}",                              # regular YT
        f"ytsearch1:{title} {artist} audio",
    ]

    # ASCII fallback for Tamil/regional
    ascii_q = unicodedata.normalize("NFKD", f"{title} {artist}").encode("ascii","ignore").decode().strip()
    if ascii_q and ascii_q != f"{title} {artist}":
        queries.append(f"ytsearch1:{ascii_q}")

    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    base       = _safe_fn(f"{artist} - {title}" if artist else title)
    out_tmpl   = os.path.join(DOWNLOAD_DIR, f"{base}_{quality}kbps.%(ext)s")
    final_path = os.path.join(DOWNLOAD_DIR, f"{base}_{quality}kbps.mp3")

    if os.path.exists(final_path):
        return final_path

    opts = {
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
        "http_headers": HEADERS,
        "retries":        3,
        "socket_timeout": 30,
    }

    loop = asyncio.get_event_loop()
    # Try ytsearch (most reliable from cloud servers)
    target = f"ytsearch1:{title} {artist}"
    try:
        def _run():
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([target])
        await loop.run_in_executor(None, _run)
    except Exception as e:
        log.error(f"yt-dlp Spotify match error for '{title}': {e}")
        raise RuntimeError(f"Could not find audio for: {title} — {e}")

    if not os.path.exists(final_path):
        raise RuntimeError(f"Download completed but file not found: {final_path}")

    return final_path


# ── YouTube search (for search results UI) ────────────────────────────────────
async def search_youtube(query: str, limit: int = MAX_SEARCH_RESULTS) -> list[dict]:
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
        results = []
        for e in (info.get("entries") or []):
            if not e:
                continue
            vid_id = e.get("id") or ""
            results.append({
                "title":    e.get("title") or "Unknown",
                "artist":   e.get("uploader") or e.get("channel") or "",
                "album":    "",
                "image":    e.get("thumbnail") or "",
                "duration": int(e.get("duration") or 0),
                "search":   f"https://youtu.be/{vid_id}" if vid_id else e.get("url", ""),
                "lyrics":   "",
                "source":   "youtube",
            })
        return results[:limit]
    except Exception as e:
        log.error(f"YouTube search error: {e}")
        return []


# ── yt-dlp download (direct YouTube URL or search) ────────────────────────────
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

    opts = {
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
        "http_headers": HEADERS,
        "retries":        3,
        "socket_timeout": 30,
    }
    loop = asyncio.get_event_loop()
    try:
        def _run():
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([target])
        await loop.run_in_executor(None, _run)
    except Exception as e:
        log.error(f"yt-dlp error '{target}': {e}")
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
