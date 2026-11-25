# Insta Save Bot

Telegram bot for downloading Instagram videos and authorized Zikra videos.

## Features

- Instagram video/Reels download.
- Zikra shared-video resolution through Supabase.
- Inline **Musiqani aniqlash** action using AudD.
- Inline **Dumaloq video** action that creates a Telegram-compatible square video note and preserves audio.
- Admin diagnostics in Telegram: `/status`, `/logs`, `/clearlogs`.

## Configuration

Set these values in Render Environment:

- `BOT_TOKEN` — Telegram bot token.
- `ADMIN_IDS` — comma-separated Telegram admin IDs.
- `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` — required for Zikra share links that resolve through the reels table.
- `AUDD_API_TOKEN` — optional but required for music recognition.
- `ZIKRA_VIDEO_TABLE=reels`
- `ZIKRA_VIDEO_ID_COLUMN=id`
- `ZIKRA_VIDEO_PATH_COLUMN=video_url`

Keep service-role keys and API tokens private. Never commit them to Git.

## Run locally

1. Copy `.env.example` to `.env` and fill in the values.
2. Install dependencies: `pip install -r requirements.txt`.
3. Install FFmpeg and ensure it is available on `PATH`.
4. Run: `python -m app`.

The bot only downloads content the user is authorized to access. Video-note conversion is limited to the first 60 seconds, as required by Telegram's video-note format. Music recognition sends an 18-second audio sample to AudD.
