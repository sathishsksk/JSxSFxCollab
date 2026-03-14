import os

# ── Telegram ──────────────────────────────────────────────────────────────────
BOT_TOKEN           = os.environ.get("BOT_TOKEN", "")
TELEGRAM_API_ID     = int(os.environ.get("TELEGRAM_API_ID", 0))
TELEGRAM_API_HASH   = os.environ.get("TELEGRAM_API_HASH", "")
BOT_ID              = os.environ.get("BOT_ID", "MusicBot")

# ── JioSaavn API ──────────────────────────────────────────────────────────────
JIOSAAVN_API        = os.environ.get("JIOSAAVN_API", "https://saavnapi-nine.vercel.app")

# ── Genius Lyrics (optional) ──────────────────────────────────────────────────
GENIUS_ACCESS_TOKEN = os.environ.get("GENIUS_ACCESS_TOKEN", "")

# ── Settings ──────────────────────────────────────────────────────────────────
MAX_PLAYLIST_SONGS  = int(os.environ.get("MAX_PLAYLIST_SONGS", 25))
MAX_SEARCH_RESULTS  = int(os.environ.get("MAX_SEARCH_RESULTS", 6))
DOWNLOAD_DIR        = "/tmp/musicbot"
PORT                = int(os.environ.get("PORT", 8080))
APP_URL             = os.environ.get("APP_URL", "")   # e.g. https://your-app.koyeb.app

# NOTE: No Spotify API keys needed at all.
# Spotify works by scraping open.spotify.com public web pages.
