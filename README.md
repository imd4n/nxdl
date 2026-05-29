# nxDL — Telegram Media Downloader Bot

A serverless Telegram inline bot that downloads media mainly from Instagram, here cobalt doesn't really work on anything else, wish I knew why.
Powered by [cobalt.directory](https://cobalt.directory) API for automatic instance discovery.

## Changes from `v1`

- **Vercel-ready**: Webhook-based instead of polling (required for serverless).
- **Dynamic instances**: Fetches working cobalt instances from `https://cobalt.directory/api/tests` instead of hardcoded `COBALT_INSTANCES`.
- **No `.env` file needed**: Uses Vercel Environment Variables.

## Deploy to Vercel

1. **Fork / upload** this code to a GitHub/GitLab repo.
2. **Import** the repo in [Vercel Dashboard](https://vercel.com/dashboard).
3. **Add Environment Variables** in Project Settings:
   - `BOT_TOKEN` — Get from [@BotFather](https://t.me/BotFather)
   - `WEBHOOK_URL` — Your Vercel deployment URL + `/api` (e.g. `https://my-bot.vercel.app/api`)
4. **Deploy**.
5. (Optional) Open `https://your-project.vercel.app/api` in a browser — you should see "Bot is running!".
6. Send `/start` to your bot, then use inline mode: `@YourBotName https://youtube.com/watch?v=...`

## Local testing (optional)

```bash
pip install -r requirements.txt
export BOT_TOKEN=your_token
export WEBHOOK_URL=https://your-ngrok-url.ngrok.io/api
python api/index.py   # Note: this only starts a local HTTP server, not polling
```

For local webhook testing, use [ngrok](https://ngrok.com) or [localtunnel](https://localtunnel.github.io/www/).

## File structure

```
.
├── api/
│   └── index.py          # Vercel serverless function (bot logic)
├── requirements.txt      # Python dependencies
```

## Notes

- The bot fetches the cobalt instance list from `cobalt.directory` every 5 minutes (in-memory cache).
- Official `*.imput.net` instances are skipped because they often require API-key / IP authentication.
- If no instances are available, the bot returns a friendly error inline result.
- Had some headaches with deployment on vercel - turns out the `vercel.json` was blocking me, so just deleting it helped.
