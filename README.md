# Game Keeper

A small Discord bot that watches one channel in one server for Steam store
links, resolves each link to game details, and stores them in SQLite so they can
be recalled later.

## What it does

- Listens to a single configured channel for `store.steampowered.com/app/...`
  and `steamcommunity.com/app/...` links.
- Looks up each game via Steam's public `appdetails` API and saves its name,
  price, description, and header image.
- Records every mention (who, when, which message), reacting with 🎮 when a new
  game is added, or 👀 when the linked game is already on the list.
- Only tracks games that support **co-op** — single-player-only games are
  ignored (reacting 🧍) so they never end up on the list or in suggestions.

## Commands

Most commands exist in two forms with identical behavior:

```
!games 5d            # compact A–Z list for the last 5 days: name, link, who added it
!games 12h           # last 12 hours
!games               # defaults to 7d
!games all           # every game ever added, with how long each has been on the list
!details 5d          # rich recall: one embed per game (price, reviews, image, app id)
!remove <link|id>    # delete a game and all its mentions (e.g. !remove 268130)
!suggest             # a few random game-night picks (same as the weekly post)
!pick                # pick a single random game to play ("you will be playing…")
!web                 # link to the voting web app (requires WEB_APP_URL)
!help                # short description of the bot and a list of its commands
```

The `!`-prefixed commands post their reply in the channel. The same seven are
also registered as **slash commands** — `/games`, `/details`, `/remove`,
`/suggest`, `/pick`, `/web`, `/help` — which reply **privately to you (ephemeral)** instead of posting to
the channel, so you can browse the list without cluttering it. They take the
same `window`/`target` arguments, surfaced as typed fields in Discord's command
UI.

There's also a slash-only **`/add <link|id>`** for adding a game without posting
its link in the channel (posting a Steam link in the watched channel already
captures it, so there's no `!add`). It's handy for adding a game you found
elsewhere, privately. Unlike passive channel capture, `/add` **skips the co-op
check** — it's an explicit request, so a single-player game you deliberately add
is honored. Since there's no channel message behind a `/add` entry,
your name shows as plain text in the recall list rather than a clickable
jump-back link.

`games`/`details` take the same time window (`s`, `m`, `h`, `d`, `w`; a bare
number means days). `games` is a terse one-line-per-game list, while `details`
shows each game's price, current review standing (pulled live from Steam at
recall time), header image, and app id for use with `remove`. `games` also
accepts `all` (nothing is ever auto-pruned, so this is the full list), which
drops the time filter and annotates each game with how long it's been on the
list — handy for finding entries that have been sitting there for ages.

Both lists credit the person who **added** each game (its earliest mention) and
link that name back to the message that added it, so you can jump to the original
context. Anyone who links a game that's already on the list is still recorded
(and reacted to with 👀), but the credit stays with the original adder.

### Weekly game-night suggestions

The bot can post an automated message to the watched channel — by default every
**Friday at 4pm AEST** — that picks three random games from the list and shows
each with its title/banner image, current price (re-fetched live so sales show),
a store link, and who suggested it, to jog your memory of what the game is. You
can
trigger the same suggestion on demand at any time with `!suggest` / `/suggest`.
If you set `WEB_APP_URL` (the public address of the voting web app), the message
also links to it so people can go vote games off the list.
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

### Voting web app & auto-cull

Because nothing is ever auto-pruned, the list grows without bound. To keep it in
check there's a companion **web app** (`web.py`) where server members vote games
off the list:

- Members **sign in with Discord** (OAuth2). Only users who are members of the
  watched server get in — anyone else is shown an "access denied" page and can
  neither see the list nor vote.
- Each member casts one **👍 +1 or 👎 −1** vote per game (click your own vote
  again to clear it). Games are shown worst-score-first, so the ones nearest the
  chopping block surface at the top.
- The bot runs a daily **auto-cull** that removes any game whose net vote score
  falls below a threshold you set, and posts what it dropped to the channel. Only
  games that have received at least one vote are eligible, so freshly added games
  are never swept away before anyone has seen them. It's off until enabled:

  ```
  /cull_enable                 # on, default threshold -3, daily at 03:00 AEST
  /cull_enable threshold:-5    # on, remove games that hit a net score below -5
  /cull_enable at:04:00        # on, run the daily cull at 04:00 AEST
  /cull_disable                # off
  /cull_status                 # show whether it's on, the threshold, and the time
  ```

The web app shares the bot's SQLite database, so run it alongside the bot (the
`docker compose` setup below starts both). It needs a few extra environment
variables — see `.env.example` (`DISCORD_CLIENT_ID`, `DISCORD_CLIENT_SECRET`,
`DISCORD_REDIRECT_URI`, `WEB_SECRET_KEY`). Get the client id/secret from your
Discord application's **OAuth2** page, and add the redirect URI (e.g.
`http://localhost:8000/callback`) to that page's **Redirects** list — it must
match exactly.

Run it locally with:

```bash
.venv/bin/gunicorn -b 0.0.0.0:8000 web:app
# or, for development: .venv/bin/python web.py
```

### Staging vs. production

The bot and web app are configured entirely through `.env`, so running a staged
copy against a test server and a production copy against the real one is just a
matter of separate env files:

```bash
cp .env.example .env.staging   # test server's guild/channel + its own Discord app
cp .env.example .env.prod      # real server's guild/channel + its own Discord app

# Keep the two deployments' containers and database volumes fully separate with
# distinct project names:
docker compose -p gk-staging --env-file .env.staging up -d --build
docker compose -p gk-prod    --env-file .env.prod    up -d --build
```

Give each environment its **own Discord application** (or at least its own OAuth
redirect URI and `WEB_PORT`) so their logins and web ports don't collide. `-p`
namespaces the containers *and* the `game-keeper-data` volume, so the staged and
production databases never touch.

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
