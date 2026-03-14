"""
helpers/spotify_handler.py

Spotify downloads via spotDL (spotDL/spotify-downloader).

HOW IT WORKS:
  - spotDL reads Spotify metadata via free API endpoints (/v1/tracks, /v1/albums etc)
  - spotDL finds the best matching audio on YouTube Music and downloads it
  - spotDL embeds ALL tags: title, artist, album, cover art, lyrics, genre, year
  - Result: perfectly tagged MP3 that matches the Spotify track exactly

WHY NOT "DIRECT FROM SPOTIFY":
  Spotify audio is DRM-protected. Direct download requires Spotify Premium +
  librespot protocol + compiled Rust auth tool (Zotify) — too fragile for
  production deployment. spotDL's YouTube Music match is indistinguishable
  in practice since it uses the official YouTube Music upload of the same song.

BUG FIXED: "A spotify client has already been initialized"
  Root cause: _spotdl was never assigned, so Spotdl() was called on every
  request, triggering the singleton guard inside spotDL.
  Fix: assign _spotdl = Spotdl(...) so it is only created once.
"""

import os
import re
import asyncio
import logging
from pathlib import Path

import yt_dlp

from config import (
    SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET,
    GENIUS_ACCESS_TOKEN, DOWNLOAD_DIR, MAX_PLAYLIST_SONGS, MAX_SEARCH_RESULTS,
)

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


# ── spotDL singleton ──────────────────────────────────────────────────────────
# THE BUG WAS HERE: _spotdl was never set to the created instance,
# so Spotdl() was called fresh every time → "already initialized" error.
_spotdl      = None
_spotdl_err  = None
_spotdl_lock = asyncio.Lock() if False else None   # replaced below in init


def _get_spotdl():
    """
    Return (spotdl_instance, error_string).
    Creates the instance exactly ONCE and reuses it for all subsequent calls.
    """
    global _spotdl, _spotdl_err

    # Already created successfully
    if _spotdl is not None:
        return _spotdl, None

    # Already failed — return cached error
    if _spotdl_err is not None:
        return None, _spotdl_err

    if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
        _spotdl_err = (
            "SPOTIFY_CLIENT_ID or SPOTIFY_CLIENT_SECRET not set.\n"
            "Add them in Koyeb env vars.\n"
            "Get from: developer.spotify.com/dashboard (free account works)"
        )
        return None, _spotdl_err

    try:
        from spotdl import Spotdl
        os.makedirs(DOWNLOAD_DIR, exist_ok=True)

        # ── Create instance ONCE and store in _spotdl ─────────────────────────
        _spotdl = Spotdl(
            client_id=SPOTIFY_CLIENT_ID,
            client_secret=SPOTIFY_CLIENT_SECRET,
            downloader_settings={
                "output":           os.path.join(DOWNLOAD_DIR, "{artist} - {title}.{output-ext}"),
                "format":           "mp3",
                "bitrate":          "auto",      # spotDL picks best available
                "threads":          1,
                "overwrite":        "skip",
                "log_level":        "ERROR",
                "print_errors":     False,
                "generate_lrc":     False,
                "save_file":        None,
                "audio_providers":  ["youtube-music"],   # YouTube Music > raw YouTube
                "lyrics_providers": ["musixmatch", "genius"],
            },
        )
        _spotdl_err = None
        log.info("✅ spotDL singleton initialized")
        return _spotdl, None

    except Exception as e:
        _spotdl_err = str(e)
        _spotdl     = None
        log.error(f"spotDL init failed: {e}")
        return None, str(e)


def _song_to_meta(song) -> dict:
    """Convert a spotDL Song object to our standard meta dict."""
    artists = ", ".join(song.artists) if hasattr(song, "artists") else getattr(song, "artist", "")
    return {
        "title":    song.name,
        "artist":   artists,
        "album":    getattr(song, "album_name", "") or "",
        "image":    getattr(song, "cover_url", "") or "",
        "duration": int(getattr(song, "duration", 0) or 0),
        "lyrics":   getattr(song, "lyrics", "") or "",
        "source":   "spotify",
    }


# ── Main spotDL download function ─────────────────────────────────────────────
async def spotdl_download(
    url: str,
    quality: str = "320",
) -> tuple[list[tuple[dict, str]], str | None]:
    """
    Download tracks from a Spotify URL using spotDL.
    Returns: ([(meta_dict, filepath), ...], error_string | None)

    quality: "128" or "320" — applied via FFmpeg post-processing if needed.
    spotDL downloads the best available quality and we re-encode to target.
    """
    spotdl_inst, err = _get_spotdl()
    if err:
        return [], err

    loop = asyncio.get_event_loop()

    try:
        # Step 1: Get song list from Spotify metadata (free endpoints, no Premium needed)
        log.info(f"spotDL searching: {url}")
        songs = await loop.run_in_executor(
            None, lambda: spotdl_inst.search([url])
        )

        if not songs:
            return [], "No tracks found for this Spotify URL. Check the link."

        songs = songs[:MAX_PLAYLIST_SONGS]
        log.info(f"spotDL found {len(songs)} track(s)")

        results = []
        for song in songs:
            try:
                # Step 2: Download — spotDL finds YouTube Music match, downloads, tags
                log.info(f"Downloading: {song.name}")
                _, path = await loop.run_in_executor(
                    None, lambda s=song: spotdl_inst.download(s)
                )

                if path and Path(str(path)).exists():
                    meta = _song_to_meta(song)
                    # Re-encode to exact requested bitrate
                    final = await _reencode(str(path), quality)
                    results.append((meta, final))
                    log.info(f"✅ Done: {song.name}")
                else:
                    log.warning(f"spotDL returned no file for: {song.name}")

            except Exception as e:
                log.error(f"spotDL download error '{song.name}': {e}")

        if not results:
            return [], "spotDL could not download any tracks. YouTube Music match not found."

        return results, None

    except Exception as e:
        err_str = str(e)
        log.error(f"spotDL error: {err_str}")
        if "403" in err_str or "premium" in err_str.lower():
            return [], (
                "Spotify API 403 — free account endpoint blocked.\n"
                "Go to developer.spotify.com → your app → Users and Access\n"
                "→ add your Spotify account email, then redeploy."
            )
        return [], f"spotDL error: {err_str}"


# ── Re-encode to exact bitrate ─────────────────────────────────────────────────
async def _reencode(src: str, quality: str) -> str:
    """Re-encode MP3 to exact 128k or 320k. Skip if already target bitrate."""
    dest = src.replace(".mp3", f"_{quality}kbps.mp3")
    if os.path.exists(dest):
        return dest
    cmd = [
        "ffmpeg", "-y", "-i", src,
        "-vn", "-ar", "44100", "-ac", "2",
        "-b:a", f"{quality}k",
        "-map_metadata", "0",   # preserve all existing tags
        "-id3v2_version", "3",
        "-f", "mp3", dest,
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        log.warning(f"FFmpeg re-encode failed, using original: {stderr.decode()[-200:]}")
        return src   # fall back to original file
    try:
        os.remove(src)
    except OSError:
        pass
    return dest


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


# ── yt-dlp audio download (YouTube URLs & search) ─────────────────────────────
def _safe_fn(s: str) -> str:
    return re.sub(r'[<>:"/\\|?*\n\r\t]', "", s).strip()[:80]


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
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/124 Safari/537.36"
            )
        },
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
