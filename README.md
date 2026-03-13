# 🎵 Music Downloader Bot — Combined (Koyeb Ready)

Merged from:
- [sathishsksk/Music-downloader-bot](https://github.com/sathishsksk/Music-downloader-bot) — JioSaavn
- [nimiology/spotify_downloader_telegram__bot](https://github.com/nimiology/spotify_downloader_telegram__bot) — Spotify + YouTube

## Features

| Source | Supported |
|---|---|
| 🟠 JioSaavn song / album / playlist | ✅ |
| 🟢 Spotify track / album / playlist / artist | ✅ |
| 🔴 YouTube direct URL | ✅ |
| 🔍 Search by song name | ✅ |
| 🎵 128 kbps quality | ✅ |
| 🎶 320 kbps quality | ✅ |

## How Quality Works

- **JioSaavn** → picks `160kbps` or `320kbps` field directly from API response (no re-encoding)
- **Spotify / YouTube / Search** → yt-dlp downloads best audio → FFmpeg converts to 128 or 320 kbps MP3

## File Structure

```
├── bot.py                     # Main bot + health server
├── config.py                  # All environment variables
├── helpers/
│   ├── jiosaavn.py            # JioSaavn downloader
│   └── spotify_handler.py     # Spotify + YouTube downloader
├── requirements.txt
├── Dockerfile
└── sample.env
```

## Deploy on Koyeb

1. Push all files to a GitHub repo
2. Go to [app.koyeb.com](https://app.koyeb.com) → New App
3. Source: **GitHub** → your repo
4. Builder: **Dockerfile**
5. Port: **8080**
6. Health check path: **/health**
7. Add environment variables:

| Variable | Required | Notes |
|---|---|---|
| `BOT_TOKEN` | ✅ | From @BotFather |
| `TELEGRAM_API_ID` | ✅ | my.telegram.org |
| `TELEGRAM_API_HASH` | ✅ | my.telegram.org |
| `BOT_ID` | ✅ | Bot username without @ |
| `SPOTIFY_CLIENT_ID` | ⚠️ | Spotify only |
| `SPOTIFY_CLIENT_SECRET` | ⚠️ | Spotify only |
| `GENIUS_ACCESS_TOKEN` | Optional | For lyrics |
| `JIOSAAVN_API` | Optional | Default is your Vercel API |
| `MAX_PLAYLIST_SONGS` | Optional | Default: 25 |

## Local Run

```bash
pip install -r requirements.txt
cp sample.env .env
# fill in .env values
python bot.py
```
