# Smart Media Downloader

Telegram Bot + Telegram Mini App for downloading media from supported public links where the user has permission to download. Built with FastAPI, python-telegram-bot, yt-dlp and FFmpeg.

## Railway setup
- Deploy this repository as a service.
- Set `BOT_TOKEN` from BotFather.
- Set `WEBAPP_URL` to the public Railway domain.
- Optional: `MAX_FILE_MB` (default 45) and `ALLOWED_USER_IDS` (comma-separated Telegram IDs; empty means public access).
- Expose port 8000. App routes: `/` and `/health`.

Only download content you own or are authorized to download and comply with platform terms. Upload size, memory, disk and platform restrictions apply. Not every URL or format is guaranteed.