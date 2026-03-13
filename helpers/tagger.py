"""helpers/tagger.py — embed ID3 tags into MP3"""

import logging
import aiohttp
from mutagen.id3 import ID3, ID3NoHeaderError, TIT2, TPE1, TALB, APIC, USLT, COMM

log = logging.getLogger(__name__)


async def _fetch_image(url: str) -> bytes | None:
    if not url or not url.startswith("http"):
        return None
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status == 200:
                    return await r.read()
    except Exception as e:
        log.warning(f"Cover art fetch failed: {e}")
    return None


async def tag_mp3(filepath: str, meta: dict, lyrics: str = ""):
    try:
        try:    tags = ID3(filepath)
        except ID3NoHeaderError: tags = ID3()

        if meta.get("title"):  tags["TIT2"] = TIT2(encoding=3, text=meta["title"])
        if meta.get("artist"): tags["TPE1"] = TPE1(encoding=3, text=meta["artist"])
        if meta.get("album"):  tags["TALB"] = TALB(encoding=3, text=meta["album"])

        tags["COMM::eng"] = COMM(encoding=3, lang="eng", desc="", text="@MusicDownloaderBot")

        if lyrics:
            tags["USLT::eng"] = USLT(encoding=3, lang="eng", desc="", text=lyrics)

        img = await _fetch_image(meta.get("image", ""))
        if img:
            mime = "image/png" if img[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
            tags["APIC:Cover"] = APIC(encoding=3, mime=mime, type=3, desc="Cover", data=img)

        tags.save(filepath, v2_version=3)
    except Exception as e:
        log.error(f"Tagging failed: {e}")
