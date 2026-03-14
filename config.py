import os

# ── Telegram ──────────────────────────────────────────────────────────────────
BOT_TOKEN           = os.environ.get("BOT_TOKEN", "")
TELEGRAM_API_ID     = int(os.environ.get("TELEGRAM_API_ID", 0))
TELEGRAM_API_HASH   = os.environ.get("TELEGRAM_API_HASH", "")
BOT_ID              = os.environ.get("BOT_ID", "MusicBot")

# ── JioSaavn API ──────────────────────────────────────────────────────────────
JIOSAAVN_API        = os.environ.get("JIOSAAVN_API", "https://saavnapi-nine.vercel.app")

# ── Spotify (FREE developer account is enough) ────────────────────────────────
# spotDL uses /v1/tracks, /v1/albums, /v1/playlists — NOT /v1/search
# These endpoints work with a FREE Spotify developer account.
# Get from: developer.spotify.com/dashboard → Create App
SPOTIFY_CLIENT_ID     = os.environ.get("SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET", "")

# ── Genius Lyrics (optional) ──────────────────────────────────────────────────
GENIUS_ACCESS_TOKEN = os.environ.get("GENIUS_ACCESS_TOKEN", "")

# ── Settings ──────────────────────────────────────────────────────────────────
MAX_PLAYLIST_SONGS  = int(os.environ.get("MAX_PLAYLIST_SONGS", 25))
MAX_SEARCH_RESULTS  = int(os.environ.get("MAX_SEARCH_RESULTS", 6))
DOWNLOAD_DIR        = "/tmp/musicbot"
PORT                = int(os.environ.get("PORT", 8080))
