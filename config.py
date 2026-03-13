import os

# ── Telegram ──────────────────────────────────────────────────────────────────
BOT_TOKEN             = os.environ.get("BOT_TOKEN", "")
TELEGRAM_API_ID       = int(os.environ.get("TELEGRAM_API_ID", 0))
TELEGRAM_API_HASH     = os.environ.get("TELEGRAM_API_HASH", "")
BOT_ID                = os.environ.get("BOT_ID", "MusicBot")

# ── JioSaavn ──────────────────────────────────────────────────────────────────
# saavnapi-nine.vercel.app is cyberboysumanjay's JioSaavnAPI
# Endpoints: /result/ /song/ /album/ /playlist/
# Response fields: title, singers, album, image_url, url (mp3), duration, lyrics
JIOSAAVN_API          = os.environ.get("JIOSAAVN_API", "https://saavnapi-nine.vercel.app")

# ── Spotify ───────────────────────────────────────────────────────────────────
SPOTIFY_CLIENT_ID     = os.environ.get("SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET", "")

# ── Genius Lyrics ─────────────────────────────────────────────────────────────
GENIUS_ACCESS_TOKEN   = os.environ.get("GENIUS_ACCESS_TOKEN", "")

# ── Settings ──────────────────────────────────────────────────────────────────
MAX_PLAYLIST_SONGS    = int(os.environ.get("MAX_PLAYLIST_SONGS", 25))
DOWNLOAD_DIR          = "/tmp/musicbot"
PORT                  = int(os.environ.get("PORT", 8080))
