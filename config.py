import os

# ── Telegram ──────────────────────────────────────────────────────────────────
BOT_TOKEN            = os.environ.get("BOT_TOKEN", "")
TELEGRAM_API_ID      = int(os.environ.get("TELEGRAM_API_ID", 0))
TELEGRAM_API_HASH    = os.environ.get("TELEGRAM_API_HASH", "")
BOT_ID               = os.environ.get("BOT_ID", "MusicBot")

# ── Channels ──────────────────────────────────────────────────────────────────
DB_CHANNEL_ID        = int(os.environ.get("DB_CHANNEL_ID", 0))

# ── JioSaavn API (your own Vercel API) ───────────────────────────────────────
JIOSAAVN_API         = os.environ.get(
    "JIOSAAVN_API",
    "https://saavnapi-nine.vercel.app"   # your own API — default
)

# ── Spotify ───────────────────────────────────────────────────────────────────
SPOTIFY_CLIENT_ID     = os.environ.get("SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET", "")

# ── Genius Lyrics ─────────────────────────────────────────────────────────────
GENIUS_ACCESS_TOKEN   = os.environ.get("GENIUS_ACCESS_TOKEN", "")

# ── Limits / Settings ─────────────────────────────────────────────────────────
MAX_PLAYLIST_SONGS   = int(os.environ.get("MAX_PLAYLIST_SONGS", 25))
DOWNLOAD_DIR         = "/tmp/music_downloads"
PORT                 = int(os.environ.get("PORT", 8080))
