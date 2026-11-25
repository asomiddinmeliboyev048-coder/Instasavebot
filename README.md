# Universal Video Downloader Bot

Telegram bot for downloading supported Instagram and YouTube videos and retrieving authorized Zikra/Supabase Storage videos.

See `.env.example` for configuration and the deployment notes below.

## Quick start

1. Copy `.env.example` to `.env` and fill in the required values.
2. Install dependencies: `pip install -r requirements.txt`.
3. Install FFmpeg and ensure it is available on `PATH`.
4. Run: `python -m app`.

The bot only downloads content the user is authorized to access. Private Zikra assets require valid Supabase credentials and a configured Storage bucket. Telegram's hosted Bot API has a per-file upload limit; oversized files are rejected with a helpful message.
