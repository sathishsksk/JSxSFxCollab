"""
helpers/tagger.py
Embeds ID3 tags into MP3:
  - Title, Artist, Album (TIT2, TPE1, TALB)
  - Front cover art (APIC)
  - Lyrics (USLT)
  - Comment (COMM)
"""

import logging
import aiohttp
from mutagen.id3 import (
    ID3, ID3NoHeaderError,
    TIT2, TPE1, TALB,
    APIC, USLT, COMM,
)

log = logging.getLogger(__name__)


async def _download_image(url: str) -> bytes | None:
    if not url or not url.startswith("http"):
        return None
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status == 200:
                    return await r.read()
    except Exception as e:
        log.warning(f"Image download failed ({url}): {e}")
    return None


async def tag_mp3(filepath: str, meta: dict, lyrics: str = ""):
    """
    Embed metadata into an MP3 file in-place.
    meta keys: title, artist, album, image (URL)
    """
    try:
        try:
            tags = ID3(filepath)
        except ID3NoHeaderError:
            tags = ID3()

        if meta.get("title"):
            tags["TIT2"] = TIT2(encoding=3, text=meta["title"])
        if meta.get("artist"):
            tags["TPE1"] = TPE1(encoding=3, text=meta["artist"])
        if meta.get("album"):
            tags["TALB"] = TALB(encoding=3, text=meta["album"])

        tags["COMM::eng"] = COMM(
            encoding=3, lang="eng", desc="", text="@MusicDownloaderBot"
        )

        if lyrics:
            tags["USLT::eng"] = USLT(
                encoding=3, lang="eng", desc="", text=lyrics
            )

        # Cover art
        image_data = await _download_image(meta.get("image", ""))
        if image_data:
            mime = "image/png" if image_data[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
            tags["APIC:Cover"] = APIC(
                encoding=3, mime=mime, type=3, desc="Cover", data=image_data
            )
            log.info(f"Cover art embedded ({len(image_data)//1024}KB)")

        tags.save(filepath, v2_version=3)
        log.info(f"Tagged: {meta.get('title','?')}")

    except Exception as e:
        log.error(f"Tagging failed for {filepath}: {e}")
