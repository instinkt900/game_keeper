# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# One-time setup
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env          # then fill in DISCORD_TOKEN / WATCH_GUILD_ID / WATCH_CHANNEL_ID

# Run the bot
.venv/bin/python bot.py

# Or with Docker (bot + voting web app; database persists on the game-keeper-data volume)
docker compose up -d --build

# Run the voting web app on its own (shares the bot's DB)
.venv/bin/gunicorn -b 0.0.0.0:8000 web:app   # or .venv/bin/python web.py for dev
```

A companion **web app** (`web.py`, Flask + Authlib) lets guild members log in
with Discord and up/down-vote games; a daily `cull_loop` in `bot.py` removes
games whose net score drops below a threshold. See the web-app section in
Architecture. Staged vs. production is an env-file split (separate Discord app +
`WEB_PORT` + `docker compose -p` project name per environment, which also
namespaces the DB volume) — see README.

Discord commands (windows: `5d`, `12h`, …; default `7d`, or `all`): `!games
<window>` (compact A–Z list — name, link, who added it; `!games all` drops the
time filter, lists every stored game, and annotates each with how long it's been
on the list) and `!details <window>` (rich
per-game embeds, with live review refresh); `!remove <link|id>` (delete a game
and its mentions); `!refresh` (re-fetch details for every stored game);
`!suggest` (a few random game-night picks, the same message the weekly
announcement posts); `!pick` (like `!suggest` but commits to a single game, with
a "you will be playing…" reply); `!web` (advertise the voting web app's URL, from
the optional `WEB_APP_URL`); `!help` (a short blurb plus the command list —
our own, since the `Bot` is built with `help_command=None` to drop discord.py's
default). All eight
also exist as guild-scoped **slash commands** (`/games`, `/details`, `/remove`,
`/refresh`, `/suggest`, `/pick`, `/web`, `/help`) that reply ephemerally to the invoking user instead of posting to
the channel; both front-ends share the same core logic (see Architecture). The
bot must be invited with the `applications.commands` OAuth scope for the slash
commands to appear. One command is **slash-only**: `/add <link|id>` adds a game
by link or app id without posting it in the channel (posting a Steam link
already captures it, so there's deliberately no `!add`).

An optional weekly **game-night announcement** posts to the watched channel
(default Friday 4pm AEST) with three random games rendered in the compact-list
format. It's off by default and toggled with the `/announce_enable [day] [at]`,
`/announce_disable`, and `/announce_status` slash commands; the on/off flag,
weekday, and time persist in the DB `settings` table.

There is no test suite or linter configured yet. Pure logic (link parsing,
`parse_duration`, the DB layer with an in-memory `Database(':memory:')`) is
importable without Discord credentials and can be exercised directly with
`.venv/bin/python -c "..."`; only `bot.py`'s module-level code requires the
env vars to be set. The dev pattern in this repo has been to verify changes
this way and against a throwaway DB before touching the real `games.db`.

## Architecture

A single-server, single-channel Discord bot. Three modules with a deliberately
flat structure:

- **`bot.py`** — entrypoint and the only Discord-aware module. The `on_message`
  handler is gated to `WATCH_GUILD_ID` + `WATCH_CHANNEL_ID` for link capture,
  but still calls `process_commands` everywhere so `!games` works in any
  channel. Link capture is *also* skipped when the message is a valid command
  (`ctx.valid`): otherwise `!remove <steam_url>` would capture the game from its
  own argument's auto-embed and then remove it. Capture reacts 🎮
  (`REACTION_ADDED`) when a genuinely new game is stored and 👀
  (`REACTION_ALREADY`) when a linked game was already on the list — both can
  appear on a message mixing new and already-tracked links. A mention is recorded
  either way (the poster is credited even for an already-listed game). Owns the shared `aiohttp.ClientSession` (created in `setup_hook`,
  attached as `bot.http_session`) and a single module-level `Database` instance.
  `setup_hook` also `bot.tree.sync(guild=WATCH_GUILD)`s the slash commands —
  guild-scoped so syncs are instant (global syncs take up to an hour).
- **`steam.py`** — no Discord/DB knowledge. `extract_app_ids` pulls distinct app
  IDs from message text (handles both `store.steampowered.com` and
  `steamcommunity.com` `/app/<id>` URLs, deduped in order); `fetch_game_details`
  calls Steam's public `appdetails` endpoint and returns a `GameDetails`
  dataclass, or `None` for delisted/region-locked apps. Review standing comes
  from a *separate* endpoint (`appreviews`) via `fetch_review_summary`, which is
  tri-state: `None` = request failed (caller keeps stored value), `(None, total,
  None)` = genuinely no reviews, `(summary, total, pct)` = usable standing.
- **`db.py`** — synchronous `sqlite3`, schema created on construction. Tables:
  `games` (one row per app, upserted to the latest details), `mentions` (one row
  per link occurrence), `settings` (k/v), and `votes` (one row per
  `(app_id, user_id)`, +1/-1). `games`/`mentions` are separate because a game is
  mentioned many times; recall queries `JOIN` and `GROUP BY app_id`. The
  `UNIQUE(app_id, message_id)` constraint makes `record_mention` idempotent —
  it returns `False` for a duplicate so the bot only reacts to genuinely new
  mentions. Voting (`cast_vote`/`clear_vote`/`vote_summary`/`user_votes`) and
  `cull_below(threshold)` back the web app + cull loop; `cull_below` uses an
  **inner** join to `votes` (only voted-on games are cull-eligible, so unvoted
  new games are never swept) and an inclusive `<= threshold`. The connection is opened
  in **WAL mode** with a `busy_timeout` and `check_same_thread=False` because two
  processes (bot + web app) and the web server's threads all share the one file.

- **`web.py`** — a small Flask + Authlib app: a *second front-end onto the same
  DB*, not Discord-aware. Members log in via Discord OAuth2 (`identify guilds`
  scopes); the `/callback` gate only creates a session if the user is a member of
  `WATCH_GUILD_ID`, so a non-member can neither see the list nor vote. `/` renders
  the games (ones the logged-in user hasn't voted on yet first, then worst-score
  first within each group) with per-user vote state; `/vote` (POST,
  CSRF-checked) toggles a +1/-1. Opens a `Database` per request via Flask's `g`,
  closed on teardown. Requires extra env vars (`DISCORD_CLIENT_ID/SECRET`,
  `DISCORD_REDIRECT_URI`, `WEB_SECRET_KEY`); run with gunicorn (see
  `docker-compose.yml`'s `web` service). Templates live in `templates/`.

Data flow: Steam link posted → `extract_app_ids` → `fetch_game_details` →
`upsert_game` + `record_mention` → 🎮 (new) / 👀 (already listed) reaction. `/add` (`_build_add`, slash-only)
drives the same `upsert_game` + `record_mention` path from the slash argument
instead of a posted message, with the actor taken from `interaction.user` rather
than `message.author`. Because a slash interaction has no linkable channel
message, `_build_add` stores the *negated* interaction id as the mention's
`message_id` — it stays unique (so the mention is still idempotent) but is
flagged non-linkable; see the `added_by` note below. Recall: both `!games` and
`!details` run `parse_duration` → `games_since(now - delta)` (except `!games
all`, which skips the filter and uses `db.all_games()`). `!details` then
refreshes review standing live per game (`fetch_review_summary` +
`update_reviews`) and renders one embed each; `!games` just sorts A–Z and sends
a compact text list. Review refresh is intentionally live-at-call (sentiment
drifts); price/header image stay as snapshots from ingest/`!refresh`.

## Conventions and gotchas

- **Most commands have two front-ends sharing one core.** Command logic lives in
  `_build_games` / `_build_details` / `_build_remove` / `_build_add` /
  `_build_help` (and `_refresh_all`), which return a list of `_Outbound`
  (content and/or embeds) and never touch Discord I/O directly. The prefix command sends those via `_send_ctx` (to the
  channel); the slash command defers ephemerally and sends via
  `_send_interaction` (`followup.send(..., ephemeral=True)`). `_build_add` is the
  exception — it has only the `/add` slash front-end (there's no `!add`). When changing a
  command's behavior, edit the `_build_*` function so both stay in sync — don't
  reimplement logic in a handler. Slash handlers must `defer(ephemeral=True)`
  before any Steam fetch, or the 3-second interaction deadline is missed.
- **The auto-cull is a second self-gating daily loop, built exactly like the
  announcement.** `cull_loop` wakes daily at the stored `cull_time`, returns
  early unless `cull_enabled == "1"`, then calls `db.cull_below(cull_threshold)`
  and reports removals to the channel. Like the announcement it's `start()`ed
  unconditionally in `setup_hook` and pointed at the stored time via
  `change_interval`; enable/disable/threshold are just `settings` writes
  (`/cull_enable [threshold] [at]`, `/cull_disable`, `/cull_status`). Same
  fixed `Australia/Brisbane` timezone as the announcement. Voting itself happens
  only in the web app — the bot never exposes a vote command. When changing cull
  behavior, keep the threshold semantics in `db.cull_below` (inclusive `<=`,
  voted-games-only), not in the loop.
- **The game-night announcement is a self-gating daily loop, not a cron.**
  `announce_loop` (`discord.ext.tasks`) wakes once a day at the configured
  wall-clock time, then returns early unless the stored `announce_enabled` flag
  is `"1"` *and* today is the stored `announce_weekday` (default Friday). It's
  `start()`ed unconditionally in `setup_hook`; enable/disable is just a
  `settings` write, so there's no loop to stop/restart. The day and time are
  set via `/announce_enable`'s optional `day`/`at` args; changing the time also
  calls `announce_loop.change_interval(time=…)` to move the daily wake-up (the
  weekday is just a gate, so it needs no loop change). The
  timezone is fixed to `Australia/Brisbane` — that's AEST (UTC+10) year-round
  with no DST, so "4pm AEST" stays 4pm; using a DST-observing zone like
  `Australia/Sydney` would shift the wall-clock time half the year. Requires the
  `tzdata` package for `ZoneInfo` on slim images. The on-demand `!suggest` /
  `/suggest` command and the scheduled loop both build their message from the
  shared `_suggest_picks(count)` (random sample) + `_refresh_picks` (live
  re-fetch) + `_announcement_message` helpers, so the two stay identical — change
  those, not one call site. `_announcement_message` appends a pointer to the
  voting web app when the optional `WEB_APP_URL` env var is set (URL wrapped in
  `<...>` to suppress its preview embed); unset, the line is omitted so the bot
  runs fine without the web app. `!pick` / `/pick` (`_build_pick`) reuses the same
  `_suggest_picks(1)` + `_refresh_picks` + `_suggestion_embed` path for a *single*
  game, differing only in the reply wording (`PICK_PREAMBLE`, "you will be
  playing…") — so it shares the live-price refresh and adder credit for free. Unlike `!games` (plain text via `_compact_line`), suggestions
  render each pick as a minimal `_suggestion_embed` — title + price (linked to
  the store), the header/banner image, and an "added by …" line — so the title
  image jogs the memory without the full store card. It sets only
  title/url/image, which is what keeps Discord from expanding its own big store
  preview (the same reason `_compact_line` wraps URLs in `<...>`).
  `ANNOUNCE_PICKS` (3) stays well under Discord's 10-embeds cap. Suggestions are
  the one place price is refreshed live: `_refresh_picks` re-fetches each sampled
  game (same path as `!refresh`, scoped to the picks), upserts it, and copies the
  fresh price/name/image onto the in-memory pick so a current sale shows — a
  failed fetch keeps the stored snapshot. Because it fetches Steam, `_build_suggest`
  is async and the `/suggest` handler must `defer` first.
- **Timestamps are always stored as ISO-8601 UTC.** `record_mention` uses
  `message.created_at` (the Discord message time, not "now"), and all
  comparisons normalize via `.astimezone(timezone.utc)`. Keep this invariant —
  mixing naive/local times will silently break the time-window queries.
- **`message_content` intent is required** and must also be enabled in the
  Discord developer portal, or message text arrives empty.
- Duration windows (`parse_duration`) accept `s/m/h/d/w`; a bare number means
  days. Add new units in `_UNIT_SECONDS` and the regex together. The literal
  `all` (see `_ALL_WINDOWS`) is special-cased *before* `parse_duration` in
  `_build_games`: it skips the time filter, calls `db.all_games()`, and renders
  each line with `_compact_line(..., show_age=True)`, appending
  `_humanize_age(g.first_mentioned)`. `first_mentioned` is `MIN(created_at)` from
  the `games_since` query (a derived column, not stored — so no migration), and
  is the only `_build_*` that uses the age annotation. `all` is `!games`-only;
  `!details`/`_build_details` still go through `parse_duration`.
- Config is environment-driven via `.env` (loaded by `python-dotenv`); the three
  required vars raise `KeyError` at import if missing, which is intentional.
  `DB_PATH` defaults to `games.db`; Docker overrides it to `/data/games.db`.
- **Schema changes use the migration hooks in `db.py`, not just the `CREATE
  TABLE` DDL.** `CREATE TABLE IF NOT EXISTS` does not alter existing databases,
  so adding a column means appending to `_MIGRATIONS` (keyed by column name →
  `ALTER TABLE … ADD COLUMN`), and removing one means adding it to
  `_DROPPED_COLUMNS`. `_migrate()` reconciles both against `PRAGMA table_info`
  on every startup. Keep the live DDL, the migration dict, and the `GameDetails`
  / `GameMention` dataclasses in sync.
- `added_by` in recall is the game's **single original adder**: the earliest
  mention in the window (an `adder` CTE = `ROW_NUMBER` partitioned by `app_id`
  only, `rn = 1`), so every "added by" / poster label shows just that one person,
  linked back to the message that added the game via `_jump_url` (guild/channel
  are constants since only one channel is watched). Everyone who *later* mentions
  a game is still recorded as a `mentions` row (poster credit, `mention_count`),
  but is not shown — the display is adder-only by design. Note the window caveat:
  in a time-bounded recall (`!games 7d`) the "adder" is the earliest mention
  *within that window*, matching `first_mentioned`; only `all`/suggestions
  (epoch-wide) show the true all-time adder. A **negative** `message_id` is a
  sentinel for a `/add` mention with no linkable message — real Discord snowflakes
  are always positive — and `_format_adder` renders that adder as plain (escaped)
  text instead of a masked link. The value is encoded as `name<char30>message_id`
  (a control-char separator that can't occur in a display name) and decoded in
  `_parse_mentioner`.
- The compact `!games` list is plain text and must look like
  `**Name** — <store_url> \[poster-links\]`: the game URL stays wrapped in
  `<...>` to suppress the per-game store preview embed, and the surrounding
  brackets are backslash-escaped so the masked adder link inside them renders
  cleanly (an unescaped `[` collides with masked-link syntax). Masked links do
  render in plain messages here. Don't make the game name itself a masked link —
  a bare URL in `[name](url)` re-triggers the preview embeds.
