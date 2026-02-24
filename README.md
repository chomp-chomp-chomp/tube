# Tube Downloader

A password-protected, self-hosted web app for saving YouTube videos as **MP4** or **MP3**. Access it from any browser — phone, tablet, or desktop — on your local network.

## Features

- Password-protected login (session cookie, 1-second brute-force delay)
- Download MP4 at Best / 1080p / 720p / 480p
- Download MP3 (192 kbps) extracted from the best available audio stream
- Video preview (thumbnail, title, uploader, duration) before downloading
- Real-time progress bar via background job polling
- Auto-cleanup of old files after 1 hour

## Requirements

- Python 3.10+
- [ffmpeg](https://ffmpeg.org/download.html) (required for MP3 extraction and MP4 merging)

## Setup

```bash
# 1. Clone / enter the project
cd tube

# 2. Create a virtual environment
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure
cp .env.example .env
# Edit .env — at minimum set APP_PASSWORD and SECRET_KEY

# 5. Run
python app.py
```

The app listens on `http://0.0.0.0:5000` by default, so it's reachable from any device on your local network.

## Accessing from your phone

1. Find your computer's local IP (e.g. `192.168.1.42`) with `ip addr` or `ifconfig`.
2. Open `http://192.168.1.42:5000` in your phone's browser.
3. Enter the password you set in `.env`.
4. Paste a YouTube URL, pick format/quality, hit **Download**, then tap **Save file**.

## Environment variables (`.env`)

| Variable | Description | Default |
|---|---|---|
| `APP_PASSWORD` | Login password | `changeme` |
| `SECRET_KEY` | Flask session signing key | random (changes on restart) |
| `DOWNLOAD_DIR` | Where files are saved | `./downloads` |
| `HOST` | Bind address | `0.0.0.0` |
| `PORT` | Port | `5000` |
| `FLASK_DEBUG` | Enable debug mode | `false` |

## Running as a persistent service (optional)

### systemd (Linux)

```ini
[Unit]
Description=Tube Downloader
After=network.target

[Service]
WorkingDirectory=/path/to/tube
EnvironmentFile=/path/to/tube/.env
ExecStart=/path/to/tube/.venv/bin/python app.py
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now tube-downloader
```

### macOS launchd

Use a `launchd` plist pointing to the same `python app.py` command with `RunAtLoad = true`.

## Security notes

- Run behind a reverse proxy (nginx/Caddy) with HTTPS if exposing outside your LAN.
- Never set `FLASK_DEBUG=true` in production.
- The `SECRET_KEY` should be a long random string — generate one with:
  ```bash
  python -c "import secrets; print(secrets.token_hex(32))"
  ```

## Troubleshooting: `Requested format is not available`

If yt-dlp returns:

```text
ERROR: [youtube] <id>: Requested format is not available.
```

try these options in order:

1. Inspect available formats:

   ```bash
   yt-dlp --list-formats "https://www.youtube.com/watch?v=<id>"
   ```

2. Use a permissive fallback selector (no hard MP4 requirement):

   ```bash
   yt-dlp -f "bestvideo*+bestaudio*/best" --merge-output-format mp4 "<url>"
   ```

3. Switch YouTube extractor client (often bypasses empty/limited format lists):

   ```bash
   yt-dlp --extractor-args "youtube:player_client=ios" -f "bestvideo*+bestaudio*/best" --merge-output-format mp4 "<url>"
   ```

4. Last resort for reliability over quality:

   ```bash
   yt-dlp --extractor-args "youtube:player_client=ios" -f "best" "<url>"
   ```

Notes:
- `--merge-output-format mp4` requires `ffmpeg`.
- If you force `ext=mp4` on videos that only provide WebM/Opus streams, yt-dlp can raise this error.
