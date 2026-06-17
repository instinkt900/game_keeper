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

Most commands exist in two forms with identical behavior:

```
!games 5d            # compact A–Z list for the last 5 days: name, link, who added it
!games 12h           # last 12 hours
!games               # defaults to 7d
!games all           # every game ever added, with how long each has been on the list
!details 5d          # rich recall: one embed per game (price, reviews, image, app id)
!remove <link|id>    # delete a game and all its mentions (e.g. !remove 268130)
!refresh             # re-fetch details for every stored game
!suggest             # a few random game-night picks (same as the weekly post)
!help                # short description of the bot and a list of its commands
```

The `!`-prefixed commands post their reply in the channel. The same six are
also registered as **slash commands** — `/games`, `/details`, `/remove`,
`/refresh`, `/suggest`, `/help` — which reply **privately to you (ephemeral)** instead of posting to
the channel, so you can browse the list without cluttering it. They take the
same `window`/`target` arguments, surfaced as typed fields in Discord's command
UI.

There's also a slash-only **`/add <link|id>`** for adding a game without posting
its link in the channel (posting a Steam link in the watched channel already
captures it, so there's no `!add`). It's handy for adding a game you found
elsewhere, privately. Since there's no channel message behind a `/add` entry,
your name shows as plain text in the recall list rather than a clickable
jump-back link.

`games`/`details` take the same time window (`s`, `m`, `h`, `d`, `w`; a bare
number means days). `games` is a terse one-line-per-game list, while `details`
shows each game's price, current review standing (pulled live from Steam at
recall time), header image, and app id for use with `remove`. `games` also
accepts `all` (nothing is ever auto-pruned, so this is the full list), which
drops the time filter and annotates each game with how long it's been on the
list — handy for finding entries that have been sitting there for ages.

In both lists, each person's name links to their first message mentioning that
game, so you can jump back to the original context.

### Weekly game-night suggestions

The bot can post an automated message to the watched channel — by default every
**Friday at 4pm AEST** — that picks three random games from the list and shows
them in the same compact format (number, name, link, who suggested it). You can
trigger the same suggestion on demand at any time with `!suggest` / `/suggest`.
The scheduled post is off until you enable it:

```
/announce_enable                      # turn it on (keeps the current day/time)
/announce_enable at:18:30             # turn it on and set the time to 18:30 AEST
/announce_enable day:Wednesday        # turn it on and announce on Wednesdays
/announce_enable day:Sunday at:20:00  # set both at once
/announce_disable                     # turn it off
/announce_status                      # show whether it's on, and the day/time
```

`day` is a dropdown of Monday–Sunday; `at` is a 24-hour `HH:MM` time in AEST
(UTC+10, no daylight saving). Both are optional — omit one to leave it unchanged.
The on/off state, day, and time are stored in the database, so they survive
restarts. It fires once a week on the chosen day.

## Setup

1. Create a bot application at https://discord.com/developers/applications and
   enable the **Message Content Intent** under Bot → Privileged Gateway Intents.
2. Invite the bot to your server with permission to read the watched channel.
   Include both the **`bot`** and **`applications.commands`** OAuth scopes (OAuth2
   → URL Generator) — the latter is what lets the slash commands appear. If the
   bot was already invited with only `bot`, just re-authorize with the updated
   URL; no need to remove it first.
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
