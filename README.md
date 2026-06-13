# discord_steam_watch

A small Discord bot that watches one channel in one server for Steam store
links, resolves each link to game details, and stores them in SQLite so they can
be recalled later.

## What it does

- Listens to a single configured channel for `store.steampowered.com/app/...`
  and `steamcommunity.com/app/...` links.
- Looks up each game via Steam's public `appdetails` API and saves its name,
  price, description, and image.
- Records every mention (who, when, which message), reacting with 🎮 on success.
- Recalls games on demand:

  ```
  !games 5d     # games mentioned in the last 5 days
  !games 12h    # last 12 hours
  !games        # defaults to 7d
  ```

  Windows accept `s`, `m`, `h`, `d`, `w`; a bare number means days.

## Setup

1. Create a bot application at https://discord.com/developers/applications and
   enable the **Message Content Intent** under Bot → Privileged Gateway Intents.
2. Invite the bot to your server with permission to read the watched channel.
3. Configure the environment:

   ```bash
   cp .env.example .env
   # fill in DISCORD_TOKEN, WATCH_GUILD_ID, WATCH_CHANNEL_ID
   ```

   Enable Discord Developer Mode, then right-click the server and channel and
   "Copy ID" to get the guild/channel IDs.

4. Install and run:

   ```bash
   python3 -m venv .venv
   .venv/bin/pip install -r requirements.txt
   .venv/bin/python bot.py
   ```

The SQLite database is created automatically at `DB_PATH` (default `games.db`).
