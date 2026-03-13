FROM python:3.11-slim

# FFmpeg for yt-dlp audio conversion (128 / 320 kbps)
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /tmp/music_downloads

EXPOSE 8080

CMD ["python", "-u", "bot.py"]
