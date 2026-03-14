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
from helpers.jiosaavn import search_songs as _jio_search, download_and_encode as _jio_dl

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
    """
    Get highest resolution image URL from Spotify images/sources array.
    Spotify sources have: {"url": "...", "width": 640, "height": 640}
    Width may be null — fallback: first item is usually largest (Spotify returns largest first).
    Then apply URL hash upgrade for guaranteed max quality.
    """
    if not images:
        return ""
    # Filter valid entries
    valid = [i for i in images if isinstance(i, dict) and i.get("url")]
    if not valid:
        return ""
    # Sort by width descending; treat None width as 0
    valid.sort(key=lambda x: x.get("width") or x.get("height") or 0, reverse=True)
    # Pick the largest — or first if all widths are null (Spotify returns largest first)
    url = valid[0].get("url", "")
    # Force max-res hash if it's a Spotify CDN URL
    from helpers.tagger import _upgrade_image_url
    return _upgrade_image_url(url)


def _track_entry(
    name: str, artists: list, album: str, image: str, duration_ms: int,
    year: str = "", genre: str = "", track_number: int = 0, disc_number: int = 0,
) -> dict:
    artist_str = ", ".join(
        a.get("name", "") if isinstance(a, dict) else str(a)
        for a in (artists or [])
    )
    return {
        "title":        name,
        "artist":       artist_str,
        "album":        album,
        "image":        image,
        "duration":     int(duration_ms / 1000),
        "year":         year,
        "genre":        genre,
        "track_number": track_number,
        "disc_number":  disc_number,
        "source":       "spotify",
        "lyrics":       "",
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

        # Extra metadata
        year         = ""
        track_number = 0
        disc_number  = 0
        if isinstance(album, dict):
            date = album.get("date", {})
            if isinstance(date, dict):
                year = str(date.get("year", "") or "")
            elif isinstance(date, str):
                year = date[:4]
        track_number = int(entity.get("trackNumber") or 0)
        disc_number  = int(entity.get("discNumber") or 0)

        return [_track_entry(name, artists, album_name, _img_url(images), duration,
                             year=year, track_number=track_number, disc_number=disc_number)]
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


def _clean_query(s: str) -> str:
    """
    Clean a Spotify title for search.
    Removes: (feat. X), - From "Album", [Remix], quotes, extra hyphens.
    Example: 'Ayalathe Veettile - From "Matinee"' → 'Ayalathe Veettile'
    """
    s = re.sub(r'\s*-\s*[Ff]rom\s+"[^"]+"', "", s)   # remove: - From "Album"
    s = re.sub(r'\s*-\s*[Ff]rom\s+\S+',     "", s)   # remove: - From Album
    s = re.sub(r'\s*[\(\[](feat|ft|with|remix|remaster)[^\)\]]*[\)\]]', "", s, flags=re.I)
    s = re.sub(r'["\']', "", s)                        # remove quotes
    s = re.sub(r'\s{2,}', " ", s)
    return s.strip()


async def download_spotify_track(song: dict, quality: str = "320") -> tuple[str, dict]:
    """
    Download audio for a Spotify track.
    Returns (filepath, enriched_meta) where enriched_meta has:
      - JioSaavn metadata (correct artist, album, year, lyrics) if found there
      - Spotify image URL (higher quality) always kept from original song dict
    Strategy:
      1. JioSaavn search — no YouTube bot issues, best for Indian music
      2. yt-dlp ios/tv_embed — YouTube fallback for non-Indian tracks
    """
    title          = song.get("title", "") or ""
    artist         = song.get("artist", "") or ""
    spotify_image  = song.get("image", "")   # always keep Spotify's high-res image

    clean_title = _clean_query(title)
    jio_queries = []
    if clean_title != title:
        jio_queries.append(f"{clean_title} {artist}".strip())
        jio_queries.append(clean_title)
    jio_queries.append(f"{title} {artist}".strip())
    jio_queries.append(title)

    seen = set()
    jio_queries = [q for q in jio_queries if q and not (q in seen or seen.add(q))]

    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    base       = _safe_fn(f"{artist} - {clean_title}" if artist else clean_title)
    final_path = os.path.join(DOWNLOAD_DIR, f"{base}_{quality}kbps.mp3")

    if os.path.exists(final_path):
        return final_path, song

    # ── Strategy 1: JioSaavn ─────────────────────────────────────────────────
    for q in jio_queries:
        log.info(f"JioSaavn search: '{q}'")
        try:
            results = await _jio_search(q, quality=quality, limit=1)
            if results:
                jio_song = results[0]
                jio_song["quality"] = quality
                path = await _jio_dl(jio_song)
                log.info(f"✅ JioSaavn: {jio_song.get('title')} — {jio_song.get('artist')}")

                # Merge: JioSaavn metadata + Spotify image (better quality)
                enriched = {
                    "title":        jio_song.get("title")  or title,
                    "artist":       jio_song.get("artist") or artist,
                    "album":        jio_song.get("album")  or song.get("album", ""),
                    "album_artist": jio_song.get("artist") or artist,
                    "year":         song.get("year", ""),      # Spotify has year, JioSaavn usually doesn't
                    "genre":        song.get("genre", ""),
                    "track_number": song.get("track_number", 0),
                    "disc_number":  song.get("disc_number", 0),
                    "image":        spotify_image or jio_song.get("image", ""),  # prefer Spotify 640px
                    "duration":     jio_song.get("duration") or song.get("duration", 0),
                    "lyrics":       jio_song.get("lyrics") or "",
                    "source":       "spotify",
                }

                import shutil
                shutil.move(path, final_path)
                return final_path, enriched
        except Exception as e:
            log.warning(f"JioSaavn '{q}' failed: {e}")

    log.warning(f"Not on JioSaavn, trying YouTube: {title}")

    # ── Strategy 2: YouTube ───────────────────────────────────────────────────
    query   = f"{clean_title} {artist}".strip()
    ascii_q = unicodedata.normalize("NFKD", query).encode("ascii", "ignore").decode().strip()

    yt_targets = [f"ytsearch1:{query}"]
    if ascii_q and ascii_q != query:
        yt_targets.append(f"ytsearch1:{ascii_q}")
    yt_targets.append(f"ytsearch1:{clean_title}")

    out_tmpl = os.path.join(DOWNLOAD_DIR, f"{base}_{quality}kbps.%(ext)s")

    for player in [["ios"], ["tv_embed"], ["mweb"]]:
        yt_opts = {
            "format":        "bestaudio/best",
            "outtmpl":       out_tmpl,
            "quiet":         True,
            "no_warnings":   True,
            "noplaylist":    True,
            "postprocessors": [{
                "key":              "FFmpegExtractAudio",
                "preferredcodec":   "mp3",
                "preferredquality": quality,
            }],
            "http_headers":  HEADERS,
            "retries":       2,
            "socket_timeout": 30,
            "extractor_args": {"youtube": {"player_client": player}},
        }
        loop = asyncio.get_event_loop()
        for target in yt_targets:
            for f in Path(DOWNLOAD_DIR).glob(f"{base}_{quality}kbps.*"):
                try: f.unlink()
                except OSError: pass
            try:
                def _run(t=target, o=yt_opts):
                    with yt_dlp.YoutubeDL(o) as ydl:
                        ydl.download([t])
                await loop.run_in_executor(None, _run)
                if os.path.exists(final_path):
                    log.info(f"✅ YouTube ({player}): {title}")
                    # Use original Spotify metadata, keep Spotify image
                    enriched = dict(song)
                    enriched["image"] = spotify_image or song.get("image", "")
                    return final_path, enriched
            except Exception as e:
                err = str(e)
                log.warning(f"YouTube {player} ({target}): {err[:100]}")
                if "Sign in" not in err and "bot" not in err.lower():
                    break

    raise RuntimeError(
        f"'{clean_title}' not found on JioSaavn or YouTube.\n"
        "For Indian songs, send the JioSaavn link directly."
    )


# ── YouTube search (for search results UI) ────────────────────────────────────
async def search_youtube(query: str, limit: int = MAX_SEARCH_RESULTS) -> list[dict]:
    opts = {
        "quiet":         True,
        "no_warnings":   True,
        "extract_flat":  True,
        "skip_download": True,
        "noplaylist":    False,
        "ignoreerrors":  True,
        "extractor_args": {
            "youtube": {"player_client": ["ios"]},
        },
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

    opts_base = {
        "format":       "bestaudio[ext=m4a]/bestaudio/best",
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

    loop      = asyncio.get_event_loop()
    last_err  = ""

    # Try multiple player clients — tv_embed and mweb are most permissive on cloud IPs
    for player in [["tv_embed"], ["ios"], ["mweb"], ["web"]]:
        opts = {**opts_base, "extractor_args": {"youtube": {"player_client": player}}}

        # Clean up any partial files from previous attempt
        for f in Path(DOWNLOAD_DIR).glob(f"{base}_{quality}kbps.*"):
            try: f.unlink()
            except OSError: pass

        try:
            def _run(t=target, o=opts):
                with yt_dlp.YoutubeDL(o) as ydl:
                    ydl.download([t])
            await loop.run_in_executor(None, _run)
            if os.path.exists(final_path):
                return final_path
        except Exception as e:
            last_err = str(e)
            log.warning(f"download_yt {player} failed: {e!s:.120}")
            # Only retry bot-detection errors — give up on real unavailability
            if not any(k in last_err for k in ["Sign in", "bot", "429"]):
                break

    log.error(f"download_yt all attempts failed for '{target}': {last_err:.200}")
    return None


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
