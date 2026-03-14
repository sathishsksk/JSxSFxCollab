"""
helpers/tagger.py

Embeds full ID3v2.3 tags into MP3:
  TIT2 — Title
  TPE1 — Artist
  TPE2 — Album Artist
  TALB — Album
  TYER — Year
  TCON — Genre
  TRCK — Track number
  TPOS — Disc number
  APIC — Cover art (always max quality)
  USLT — Lyrics
  COMM — Comment

Spotify image URL upgrade:
  Spotify CDN uses hash prefix to encode resolution:
    ab67616d000048a1  →  64×64
    ab67616d00001e02  →  300×300
    ab67616d0000b273  →  640×640   ← we always force this
"""

import re
import logging
import aiohttp
from mutagen.id3 import (
    ID3, ID3NoHeaderError,
    TIT2, TPE1, TPE2, TALB, TYER, TCON, TRCK, TPOS,
    APIC, USLT, COMM,
)

log = logging.getLogger(__name__)

# Spotify CDN resolution hashes — replace any smaller one with max
_SP_SMALL_HASHES = [
    "ab67616d000048a1",   # 64×64
    "ab67616d00001e02",   # 300×300
]
_SP_MAX_HASH = "ab67616d0000b273"   # 640×640


def _upgrade_image_url(url: str) -> str:
    """
    Force Spotify CDN image URL to 640×640 max resolution.
    Also upgrades JioSaavn URLs from 150/500 to highest available.
    """
    if not url:
        return url

    # Spotify CDN: replace smaller hash with max-res hash
    for small in _SP_SMALL_HASHES:
        if small in url:
            url = url.replace(small, _SP_MAX_HASH)
            return url

    # JioSaavn CDN: upgrade to 500×500 if smaller size in URL
    # e.g. -150x150.jpg → -500x500.jpg
    url = re.sub(r"-\d+x\d+\.(jpg|jpeg|png|webp)", r"-500x500.\1", url, flags=re.I)

    return url


async def _fetch_image(url: str) -> bytes | None:
    url = _upgrade_image_url(url)
    if not url or not url.startswith("http"):
        return None
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                url,
                timeout=aiohttp.ClientTimeout(total=20),
                headers={"User-Agent": "Mozilla/5.0"},
            ) as r:
                if r.status == 200:
                    data = await r.read()
                    log.info(f"Cover art: {len(data)//1024}KB from {url[:60]}")
                    return data
                log.warning(f"Cover art HTTP {r.status}: {url[:60]}")
    except Exception as e:
        log.warning(f"Cover art fetch failed: {e}")
    return None


async def tag_mp3(filepath: str, meta: dict, lyrics: str = ""):
    """
    Write all available ID3 tags to the MP3 file.
    meta keys used: title, artist, album_artist, album, year, genre,
                    track_number, disc_number, image (URL), source
    """
    try:
        try:
            tags = ID3(filepath)
        except ID3NoHeaderError:
            tags = ID3()

        # ── Text tags ─────────────────────────────────────────────────────────
        if meta.get("title"):
            tags["TIT2"] = TIT2(encoding=3, text=meta["title"])

        if meta.get("artist"):
            tags["TPE1"] = TPE1(encoding=3, text=meta["artist"])

        album_artist = meta.get("album_artist") or meta.get("artist") or ""
        if album_artist:
            tags["TPE2"] = TPE2(encoding=3, text=album_artist)

        if meta.get("album"):
            tags["TALB"] = TALB(encoding=3, text=meta["album"])

        if meta.get("year"):
            tags["TYER"] = TYER(encoding=3, text=str(meta["year"]))

        if meta.get("genre"):
            tags["TCON"] = TCON(encoding=3, text=meta["genre"])

        if meta.get("track_number"):
            total = meta.get("track_total", "")
            trck  = f"{meta['track_number']}/{total}" if total else str(meta["track_number"])
            tags["TRCK"] = TRCK(encoding=3, text=trck)

        if meta.get("disc_number"):
            tags["TPOS"] = TPOS(encoding=3, text=str(meta["disc_number"]))

        tags["COMM::eng"] = COMM(
            encoding=3, lang="eng", desc="", text="t.me/MusicDownloaderBot"
        )

        # ── Lyrics ────────────────────────────────────────────────────────────
        if lyrics:
            tags["USLT::eng"] = USLT(encoding=3, lang="eng", desc="", text=lyrics)

        # ── Cover art — always max quality ────────────────────────────────────
        img = await _fetch_image(meta.get("image") or "")
        if img:
            mime = "image/png" if img[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
            tags["APIC:Cover"] = APIC(
                encoding=3, mime=mime, type=3,
                desc="Cover", data=img,
            )

        tags.save(filepath, v2_version=3)
        log.info(f"Tagged: {meta.get('title','?')} — {len(tags)} tags")

    except Exception as e:
        log.error(f"Tagging failed for {filepath}: {e}")
