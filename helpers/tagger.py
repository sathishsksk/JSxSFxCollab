"""
helpers/tagger.py
Embeds into the MP3 file:
  • Album art (thumbnail)  — APIC frame
  • Title, Artist, Album   — TIT2, TPE1, TALB frames
  • Track duration         — (handled by player from audio itself)
  • Lyrics                 — USLT frame
  • Genre                  — TCON frame
  • Comment / source       — COMM frame
Uses mutagen for ID3 tagging and aiohttp to download the cover image.
"""

import asyncio
import logging
import aiohttp

from mutagen.mp3 import MP3
from mutagen.id3 import (
    ID3, ID3NoHeaderError,
    TIT2, TPE1, TALB, TCON,
    APIC, USLT, COMM,
    error as ID3Error,
)

log = logging.getLogger(__name__)


async def _fetch_image(url: str) -> bytes | None:
    """Download cover image bytes from URL."""
    if not url:
        return None
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status == 200:
                    return await r.read()
    except Exception as e:
        log.warning(f"Cover fetch failed: {e}")
    return None


async def embed_metadata(filepath: str, meta: dict, lyrics: str | None = None):
    """
    Embed full ID3 metadata into an MP3 file.

    meta keys used:
        title, artist, album, image (URL), genre (optional)
    lyrics:
        plain text string or None
    """
    try:
        # Load or create ID3 tag
        try:
            tags = ID3(filepath)
        except ID3NoHeaderError:
            tags = ID3()

        # ── Text tags ──────────────────────────────────────────────────────
        if meta.get("title"):
            tags["TIT2"] = TIT2(encoding=3, text=meta["title"])

        if meta.get("artist"):
            tags["TPE1"] = TPE1(encoding=3, text=meta["artist"])

        if meta.get("album"):
            tags["TALB"] = TALB(encoding=3, text=meta["album"])

        if meta.get("genre"):
            tags["TCON"] = TCON(encoding=3, text=meta["genre"])

        # Source comment
        tags["COMM::eng"] = COMM(
            encoding=3,
            lang="eng",
            desc="",
            text="Downloaded via @MusicBot"
        )

        # ── Lyrics ─────────────────────────────────────────────────────────
        if lyrics:
            tags["USLT::eng"] = USLT(
                encoding=3,
                lang="eng",
                desc="",
                text=lyrics
            )

        # ── Album art (thumbnail) ──────────────────────────────────────────
        image_url = meta.get("image", "")
        if image_url:
            cover_data = await _fetch_image(image_url)
            if cover_data:
                # Detect mime type from first bytes
                mime = "image/jpeg"
                if cover_data[:8] == b"\x89PNG\r\n\x1a\n":
                    mime = "image/png"

                tags["APIC:Cover"] = APIC(
                    encoding=3,
                    mime=mime,
                    type=3,          # 3 = Front cover
                    desc="Cover",
                    data=cover_data,
                )
                log.info(f"✅ Cover art embedded ({len(cover_data)//1024} KB)")
            else:
                log.warning("⚠️ Cover image could not be downloaded")

        # ── Save ───────────────────────────────────────────────────────────
        tags.save(filepath, v2_version=3)
        log.info(f"✅ Metadata embedded: {meta.get('title','?')}")

    except Exception as e:
        log.error(f"Tag embedding failed for {filepath}: {e}")
