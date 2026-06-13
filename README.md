# Game Keeper

A small Discord bot that watches one channel in one server for Steam store
links, resolves each link to game details, and stores them in SQLite so they can
be recalled later.

## What it does

- Listens to a single configured channel for `store.steampowered.com/app/...`
  and `steamcommunity.com/app/...` links.
- Looks up each game via Steam's public `appdetails` API and saves its name,
  price, description, and header image.
- Records every mention (who, when, which message), reacting with 🎮 on success.

## Commands

```
!games 5d            # compact A–Z list for the last 5 days: name, link, who added it
!games 12h           # last 12 hours
!games               # defaults to 7d
!details 5d          # rich recall: one embed per game (price, reviews, image, app id)
!remove <link|id>    # delete a game and all its mentions (e.g. !remove 268130)
!refresh             # re-fetch details for every stored game
```

`!games` and `!details` take the same time window (`s`, `m`, `h`, `d`, `w`; a
bare number means days). `!games` is a terse one-line-per-game list, while
`!details` shows each game's price, current review standing (pulled live from
Steam at recall time), header image, and app id for use with `!remove`.

In both lists, each person's name links to their first message mentioning that
game, so you can jump back to the original context.

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

## Running with Docker

With your `.env` filled in, build and start the bot in the background:

```bash
docker compose up -d --build
docker compose logs -f        # follow output
docker compose down           # stop
```

The database is stored on a named volume (`game-keeper-data`, mounted at
`/data`), so it survives container restarts and rebuilds. The container reads
your credentials from `.env`; `DB_PATH` is set to `/data/games.db` automatically
by the compose file.
