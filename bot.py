"""Discord bot entrypoint.

Watches one channel in one server for Steam store links. Each link is resolved
to game details (via Steam's appdetails API), stored in SQLite, and can be
recalled with the `!games` command, e.g. `!games 5d` for the last 5 days.
"""
from __future__ import annotations

import calendar
import os
import random
import re
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

import steam
from db import Database

load_dotenv()

TOKEN = os.environ["DISCORD_TOKEN"]
WATCH_GUILD_ID = int(os.environ["WATCH_GUILD_ID"])
WATCH_CHANNEL_ID = int(os.environ["WATCH_CHANNEL_ID"])
# Slash commands are registered to this one guild so syncs land instantly
# (global syncs take up to an hour to propagate).
WATCH_GUILD = discord.Object(id=WATCH_GUILD_ID)
COMMAND_PREFIX = os.environ.get("COMMAND_PREFIX", "!")
DB_PATH = os.environ.get("DB_PATH", "games.db")

# Cap how many games a single !games call will render (each is its own embed).
MAX_GAMES_SHOWN = 20

# Weekly "game night" announcement. The time-of-day is configurable at runtime
# (stored in the DB); the timezone and weekday are fixed. Australia/Brisbane is
# AEST (UTC+10) year-round with no daylight saving, so 4pm always means 4pm AEST.
ANNOUNCE_TZ = ZoneInfo("Australia/Brisbane")
DEFAULT_ANNOUNCE_WEEKDAY = 4  # Monday=0 … Friday=4 (the stored default)
DEFAULT_ANNOUNCE_TIME = "16:00"  # HH:MM, 24-hour
ANNOUNCE_PICKS = 3
_HHMM_RE = re.compile(r"^\s*([01]?\d|2[0-3]):([0-5]\d)\s*$")

# e.g. "5d", "12h", "1w", "30m". Bare number defaults to days.
_DURATION_RE = re.compile(r"^\s*(\d+)\s*([smhdw]?)\s*$", re.IGNORECASE)
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}

# Windows that mean "every stored game, ignore the time filter".
_ALL_WINDOWS = {"all", "*", "everything"}


def parse_duration(text: str) -> timedelta | None:
    """Parse a window like '5d' or '12h' into a timedelta, or None if invalid."""
    match = _DURATION_RE.match(text)
    if not match:
        return None
    amount = int(match.group(1))
    unit = (match.group(2) or "d").lower()
    return timedelta(seconds=amount * _UNIT_SECONDS[unit])


intents = discord.Intents.default()
intents.message_content = True  # required to read message text; enable in the dev portal

# help_command=None drops discord.py's built-in !help so our own can take over.
bot = commands.Bot(command_prefix=COMMAND_PREFIX, intents=intents, help_command=None)
db = Database(DB_PATH)


@bot.event
async def setup_hook() -> None:
    bot.http_session = aiohttp.ClientSession()
    # Push the slash-command definitions to the watched guild (instant).
    await bot.tree.sync(guild=WATCH_GUILD)
    # Always run the weekly announcement loop; it self-gates on the stored
    # enabled flag, so toggling is just a DB write. Point it at the stored time.
    announce_loop.change_interval(time=_stored_announce_time())
    announce_loop.start()


@bot.event
async def on_ready() -> None:
    print(f"Logged in as {bot.user} — watching channel {WATCH_CHANNEL_ID}")


@bot.event
async def on_message(message: discord.Message) -> None:
    # Ignore the bot's own messages and anything outside the watched channel,
    # but still let commands (e.g. !games) be processed everywhere.
    if message.author == bot.user:
        return

    if (
        message.guild
        and message.guild.id == WATCH_GUILD_ID
        and message.channel.id == WATCH_CHANNEL_ID
    ):
        await _handle_steam_links(message)

    await bot.process_commands(message)


async def _handle_steam_links(message: discord.Message) -> None:
    app_ids = steam.extract_app_ids(message.content)
    if not app_ids:
        return

    stored: list[str] = []
    for app_id in app_ids:
        details = await steam.fetch_game_details(bot.http_session, app_id)
        if details is None:
            continue
        db.upsert_game(details)
        is_new = db.record_mention(
            app_id=app_id,
            message_id=message.id,
            user_id=message.author.id,
            user_name=message.author.display_name,
            when=message.created_at,
        )
        if is_new:
            stored.append(details.name)

    if stored:
        await message.add_reaction("🎮")


# --- Command core -----------------------------------------------------------
# The actual recall/remove/refresh logic lives in builder functions that return
# a list of `_Outbound` messages, decoupled from how they're delivered. Prefix
# commands send them to the channel; slash commands send them back to the
# invoking user as ephemeral followups. This keeps the two front-ends in sync.


@dataclass
class _Outbound:
    """One message to deliver: text, embeds, or both."""
    content: str | None = None
    embeds: list[discord.Embed] = field(default_factory=list)


def _bad_window(window: str) -> list[_Outbound]:
    return [
        _Outbound(
            content=f"Couldn't read `{window}`. Try a number plus s/m/h/d/w, e.g. `5d`."
        )
    ]


async def _build_details(window: str) -> list[_Outbound]:
    """Rich, per-game recall within a time window."""
    delta = parse_duration(window)
    if delta is None:
        return _bad_window(window)

    since = datetime.now(timezone.utc) - delta
    results = db.games_since(since)
    if not results:
        return [_Outbound(content=f"No games mentioned in the last {window}.")]

    # One embed per game so each can carry its own header image. Cap by recency
    # so a busy window doesn't spam (Discord sends at most 10 embeds/msg), then
    # display the kept games A–Z.
    capped = results[:MAX_GAMES_SHOWN]
    capped.sort(key=lambda g: g.name.lower())

    # Review sentiment drifts over time, so pull it live at recall time (recalls
    # are infrequent). On a failed fetch we keep the stored snapshot.
    for g in capped:
        reviews = await steam.fetch_review_summary(bot.http_session, g.app_id)
        if reviews is not None:
            g.review_summary, g.review_total, g.review_positive_pct = reviews
            db.update_reviews(g.app_id, *reviews)

    messages = [
        _Outbound(content=f"**Games mentioned in the last {window}** ({len(capped)} shown):")
    ]
    for i in range(0, len(capped), 10):
        messages.append(_Outbound(embeds=[_game_embed(g) for g in capped[i : i + 10]]))
    if len(results) > MAX_GAMES_SHOWN:
        messages.append(_Outbound(content=f"…and {len(results) - MAX_GAMES_SHOWN} more."))
    return messages


def _build_games(window: str) -> list[_Outbound]:
    """Compact A–Z list of games in a window: name, link, and who added it.

    The special window `all` ignores the time filter and lists every stored
    game, annotating each with how long it's been on the list.
    """
    if window.strip().lower() in _ALL_WINDOWS:
        results = db.all_games()
        if not results:
            return [_Outbound(content="No games stored yet.")]
        results.sort(key=lambda g: g.name.lower())
        lines = [f"**All games on the list** ({len(results)}):"]
        lines += [
            _compact_line(i, g, show_age=True) for i, g in enumerate(results, start=1)
        ]
        return [_Outbound(content=chunk) for chunk in _chunk_lines(lines)]

    delta = parse_duration(window)
    if delta is None:
        return _bad_window(window)

    since = datetime.now(timezone.utc) - delta
    results = db.games_since(since)
    if not results:
        return [_Outbound(content=f"No games mentioned in the last {window}.")]

    results.sort(key=lambda g: g.name.lower())
    lines = [f"**Games mentioned in the last {window}** ({len(results)}):"]
    lines += [_compact_line(i, g) for i, g in enumerate(results, start=1)]
    return [_Outbound(content=chunk) for chunk in _chunk_lines(lines)]


async def _build_suggest() -> list[_Outbound]:
    """Random game-night picks — the same message as the weekly announcement."""
    picks = _suggest_picks()
    if picks is None:
        return [_Outbound(content="No games stored yet.")]
    await _refresh_picks(picks)
    return [_announcement_message(picks, ON_DEMAND_PREAMBLE)]


async def _refresh_picks(picks) -> None:
    """Re-fetch each pick's Steam details so the suggestion shows the live price.

    Persists the fresh details (same path as `!refresh`, but scoped to the few
    sampled games) and copies price/name/image onto the in-memory pick so the
    embed reflects it without a re-query. A failed fetch leaves the stored
    snapshot in place. Keep this before `_announcement_message` in both the
    on-demand and scheduled paths so the two stay in step.
    """
    for g in picks:
        details = await steam.fetch_game_details(bot.http_session, g.app_id)
        if details is None:
            continue
        db.upsert_game(details)
        g.name = details.name
        g.url = details.url
        g.header_image = details.header_image
        g.is_free = details.is_free
        g.price = details.price


def _build_help() -> list[_Outbound]:
    """A short blurb on what the bot does, then each command and its use."""
    p = COMMAND_PREFIX
    lines = [
        "**Game Keeper** — I monitor the channel for Steam game mentions for "
        "later recall.",
        "",
        f"**Commands** (most work as both a `{p}`-prefix message and a `/`-slash "
        "command; slash commands reply privately to you):",
        f"• `{p}games [window]` / `/games` — compact A–Z list of games in a time "
        "window (`5d`, `12h`, … default `7d`, or `all` for every game with its age)",
        f"• `{p}details [window]` / `/details` — richer per-game cards: price, live "
        "review standing, image, and app id",
        f"• `{p}remove <link|id>` / `/remove` — remove a game and all its mentions",
        f"• `{p}refresh` / `/refresh` — re-fetch Steam details for every stored game",
        f"• `{p}suggest` / `/suggest` — a few random game-night picks",
        f"• `{p}help` / `/help` — this message",
        "",
        "**Slash-only:**",
        "• `/add <link|id>` — add a game without posting its link in the channel",
        "• `/announce_enable [day] [at]`, `/announce_disable`, `/announce_status` "
        "— manage the weekly game-night suggestions post",
        "",
        "Time windows accept `s`/`m`/`h`/`d`/`w`; a bare number means days.",
    ]
    return [_Outbound(content="\n".join(lines))]


def _compact_line(index: int, g, show_age: bool = False) -> str:
    """One game as `N. **Name** — <url> \\[posters\\]` (the compact list format).

    The store URL stays wrapped in <...> to suppress its preview embed, and the
    surrounding brackets are escaped so the masked poster links render cleanly.
    With `show_age`, append how long the game has been on the list (used by
    `!games all`).
    """
    who = _format_mentioners(g.mentioned_by) if g.mentioned_by else "unknown"
    line = f"{index}. **{g.name}** — <{g.url}> \\[{who}\\]"
    if show_age:
        line += f" · added {_humanize_age(g.first_mentioned)}"
    return line


def _humanize_age(since: datetime) -> str:
    """Coarse 'how long ago' for a first mention, e.g. '3 months ago', 'today'."""
    delta = datetime.now(timezone.utc) - since.astimezone(timezone.utc)
    days = delta.days
    if days >= 730:
        return f"{days // 365} years ago"
    if days >= 365:
        return "1 year ago"
    if days >= 60:
        return f"{days // 30} months ago"
    if days >= 30:
        return "1 month ago"
    if days >= 2:
        return f"{days} days ago"
    if days == 1:
        return "1 day ago"
    return "today"


def _build_remove(target: str) -> list[_Outbound]:
    """Remove game(s) by Steam link or app id, with all of their mentions."""
    app_ids = steam.extract_app_ids(target)
    # Also accept bare numeric app ids (e.g. "268130 440") alongside full links.
    app_ids += [int(tok) for tok in target.split() if tok.isdigit()]
    app_ids = list(dict.fromkeys(app_ids))  # de-dup, preserve order

    if not app_ids:
        return [_Outbound(content="Usage: provide a Steam link or app id, e.g. `268130`.")]

    removed: list[str] = []
    missing: list[int] = []
    for app_id in app_ids:
        name = db.remove_game(app_id)
        if name is not None:
            removed.append(name)
        else:
            missing.append(app_id)

    lines = []
    if removed:
        lines.append("🗑️ Removed: " + ", ".join(f"**{n}**" for n in removed))
    if missing:
        lines.append("Not in the list: " + ", ".join(f"`{a}`" for a in missing))
    return [_Outbound(content="\n".join(lines))]


async def _build_add(
    target: str,
    *,
    user_id: int,
    user_name: str,
    source_id: int,
    when: datetime,
) -> list[_Outbound]:
    """Add game(s) to the list by Steam link or app id, crediting the caller.

    Mirrors the channel link-capture path (`_handle_steam_links`): resolve each
    app id, upsert its details, and record a mention. Driven by the `/add` slash
    argument rather than a posted message, so a game can be added privately
    without pasting its link in the watched channel. (Posting the link in the
    channel already captures it, so there's no prefix `!add` — `/add` is for the
    add-without-posting case.)

    A slash interaction has no channel message to link back to (the jump-url
    machinery assumes the watched channel). So we store the *negated* `source_id`
    (the interaction id) as the mention's `message_id`: still unique, so the
    mention stays idempotent, but flagged non-linkable. Real snowflakes are
    always positive, so a negative `message_id` can only mean "no message", and
    the recall renderer shows these posters as plain text instead of a dead link.
    """
    app_ids = steam.extract_app_ids(target)
    # Also accept bare numeric app ids (e.g. "268130 440") alongside full links.
    app_ids += [int(tok) for tok in target.split() if tok.isdigit()]
    app_ids = list(dict.fromkeys(app_ids))  # de-dup, preserve order

    if not app_ids:
        return [_Outbound(content="Usage: provide a Steam link or app id, e.g. `268130`.")]

    already = set(db.all_app_ids())
    added: list[str] = []
    existing: list[str] = []
    failed: list[int] = []
    for app_id in app_ids:
        details = await steam.fetch_game_details(bot.http_session, app_id)
        if details is None:
            failed.append(app_id)
            continue
        db.upsert_game(details)
        # Record the mention regardless so the caller is credited even for a game
        # already on the list; bucket the reply by whether it was already stored.
        db.record_mention(
            app_id=app_id,
            message_id=-source_id,
            user_id=user_id,
            user_name=user_name,
            when=when,
        )
        (existing if app_id in already else added).append(details.name)

    lines: list[str] = []
    if added:
        lines.append("🎮 Added: " + ", ".join(f"**{n}**" for n in added))
    if existing:
        lines.append("Already on the list: " + ", ".join(f"**{n}**" for n in existing))
    if failed:
        lines.append("Couldn't find: " + ", ".join(f"`{a}`" for a in failed))
    return [_Outbound(content="\n".join(lines))]


async def _refresh_all() -> tuple[int, int]:
    """Re-fetch Steam details for every stored game. Returns (updated, total)."""
    app_ids = db.all_app_ids()
    updated = 0
    for app_id in app_ids:
        details = await steam.fetch_game_details(bot.http_session, app_id)
        if details is not None:
            db.upsert_game(details)
            updated += 1
    return updated, len(app_ids)


def _chunk_lines(lines: list[str]) -> list[str]:
    """Group lines into messages each under Discord's 2000-char limit."""
    chunks: list[str] = []
    buffer = ""
    for line in lines:
        if len(buffer) + len(line) + 1 > 1900:
            chunks.append(buffer)
            buffer = ""
        buffer += ("\n" if buffer else "") + line
    if buffer:
        chunks.append(buffer)
    return chunks


async def _send_ctx(ctx: commands.Context, messages: list[_Outbound]) -> None:
    for m in messages:
        kwargs = {}
        if m.content is not None:
            kwargs["content"] = m.content
        if m.embeds:
            kwargs["embeds"] = m.embeds
        await ctx.send(**kwargs)


async def _send_interaction(
    interaction: discord.Interaction, messages: list[_Outbound]
) -> None:
    for m in messages:
        kwargs = {"ephemeral": True}
        if m.content is not None:
            kwargs["content"] = m.content
        if m.embeds:
            kwargs["embeds"] = m.embeds
        await interaction.followup.send(**kwargs)


# --- Prefix commands (post to the channel) ----------------------------------


@bot.command(name="details")
async def details(ctx: commands.Context, window: str = "7d") -> None:
    """Rich, per-game recall within a time window, e.g. `!details 5d`."""
    await _send_ctx(ctx, await _build_details(window))


@bot.command(name="games")
async def games(ctx: commands.Context, window: str = "7d") -> None:
    """Compact A–Z list of games in a window, e.g. `!games 5d` or `!games all`."""
    await _send_ctx(ctx, _build_games(window))


@bot.command(name="remove")
async def remove(ctx: commands.Context, *, target: str = "") -> None:
    """Remove game(s) by Steam link or app id, e.g. `!remove 268130`."""
    await _send_ctx(ctx, _build_remove(target))


@bot.command(name="refresh")
async def refresh(ctx: commands.Context) -> None:
    """Re-fetch Steam details (header image, reviews, price) for every stored game."""
    app_ids = db.all_app_ids()
    if not app_ids:
        await ctx.send("No games stored yet.")
        return
    notice = await ctx.send(f"Refreshing {len(app_ids)} game(s)…")
    updated, total = await _refresh_all()
    await notice.edit(content=f"Refreshed {updated}/{total} game(s).")


@bot.command(name="suggest")
async def suggest(ctx: commands.Context) -> None:
    """Post a few random game-night suggestions (same as the weekly announcement)."""
    await _send_ctx(ctx, await _build_suggest())


@bot.command(name="help")
async def help_cmd(ctx: commands.Context) -> None:
    """Show what the bot does and list its commands."""
    await _send_ctx(ctx, _build_help())


# --- Slash commands (reply privately to the invoking user) ------------------
# Each defers ephemerally first: the recall/refresh paths hit Steam's API and
# would otherwise blow past the 3-second interaction-response deadline.


@bot.tree.command(
    name="details",
    description="Rich, per-game recall of games posted in a time window (private reply)",
    guild=WATCH_GUILD,
)
@app_commands.describe(window="Time window like 5d, 12h, 1w (default 7d)")
async def slash_details(interaction: discord.Interaction, window: str = "7d") -> None:
    await interaction.response.defer(ephemeral=True)
    await _send_interaction(interaction, await _build_details(window))


@bot.tree.command(
    name="games",
    description="Compact A–Z list of games posted in a time window (private reply)",
    guild=WATCH_GUILD,
)
@app_commands.describe(window="Time window like 5d, 12h, 1w (default 7d), or 'all'")
async def slash_games(interaction: discord.Interaction, window: str = "7d") -> None:
    await interaction.response.defer(ephemeral=True)
    await _send_interaction(interaction, _build_games(window))


@bot.tree.command(
    name="add",
    description="Add a game to the list by Steam link or app id (private reply)",
    guild=WATCH_GUILD,
)
@app_commands.describe(target="Steam link(s) or app id(s), e.g. 268130")
async def slash_add(interaction: discord.Interaction, target: str) -> None:
    await interaction.response.defer(ephemeral=True)
    await _send_interaction(
        interaction,
        await _build_add(
            target,
            user_id=interaction.user.id,
            user_name=interaction.user.display_name,
            source_id=interaction.id,
            when=datetime.now(timezone.utc),
        ),
    )


@bot.tree.command(
    name="remove",
    description="Remove game(s) from the list by Steam link or app id (private reply)",
    guild=WATCH_GUILD,
)
@app_commands.describe(target="Steam link(s) or app id(s), e.g. 268130")
async def slash_remove(interaction: discord.Interaction, target: str) -> None:
    await interaction.response.defer(ephemeral=True)
    await _send_interaction(interaction, _build_remove(target))


@bot.tree.command(
    name="refresh",
    description="Re-fetch Steam details for every stored game (private reply)",
    guild=WATCH_GUILD,
)
async def slash_refresh(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    updated, total = await _refresh_all()
    if total == 0:
        await interaction.followup.send("No games stored yet.", ephemeral=True)
    else:
        await interaction.followup.send(
            f"Refreshed {updated}/{total} game(s).", ephemeral=True
        )


@bot.tree.command(
    name="suggest",
    description="Get a few random game-night suggestions (private reply)",
    guild=WATCH_GUILD,
)
async def slash_suggest(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    await _send_interaction(interaction, await _build_suggest())


@bot.tree.command(
    name="help",
    description="Show what the bot does and list its commands (private reply)",
    guild=WATCH_GUILD,
)
async def slash_help(interaction: discord.Interaction) -> None:
    # No Steam fetch, so respond immediately (help is a single short message).
    await interaction.response.send_message(_build_help()[0].content, ephemeral=True)


# --- Weekly game-night announcement -----------------------------------------
# A daily loop fires at the configured wall-clock time; it only posts on the
# announce weekday (Friday) and only while the stored flag is set. The time is
# changed at runtime via change_interval (see the announce slash commands).


def _stored_announce_hhmm() -> tuple[int, int]:
    raw = db.get_setting("announce_time") or DEFAULT_ANNOUNCE_TIME
    hh, mm = raw.split(":")
    return int(hh), int(mm)


def _stored_announce_weekday() -> int:
    raw = db.get_setting("announce_weekday")
    return int(raw) if raw is not None else DEFAULT_ANNOUNCE_WEEKDAY


def _stored_announce_time() -> dtime:
    hh, mm = _stored_announce_hhmm()
    return dtime(hour=hh, minute=mm, tzinfo=ANNOUNCE_TZ)


def _parse_hhmm(text: str) -> tuple[int, int] | None:
    match = _HHMM_RE.match(text)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def _suggest_picks():
    """Random sample of stored games for a suggestion, or None if none stored."""
    games = db.all_games()
    if not games:
        return None
    return random.sample(games, min(ANNOUNCE_PICKS, len(games)))


SCHEDULED_PREAMBLE = (
    "🎲 **Time to choose the game for tonight!** Here are some suggestions:"
)
ON_DEMAND_PREAMBLE = "Here are some suggestions:"


def _announcement_message(picks, preamble: str = SCHEDULED_PREAMBLE) -> _Outbound:
    """The game-night message: a preamble plus one banner embed per pick.

    The preamble and the `!games` pointer are the message content; each pick is a
    minimal `_suggestion_embed` so the title image shows without the full store
    card. `ANNOUNCE_PICKS` (3) stays well under Discord's 10-embeds/message cap.
    """
    content = f"{preamble}\nOr use `!games` for more suggestions."
    return _Outbound(content=content, embeds=[_suggestion_embed(g) for g in picks])


@tasks.loop(time=dtime(hour=16, minute=0, tzinfo=ANNOUNCE_TZ))
async def announce_loop() -> None:
    if db.get_setting("announce_enabled") != "1":
        return
    if datetime.now(ANNOUNCE_TZ).weekday() != _stored_announce_weekday():
        return

    channel = bot.get_channel(WATCH_CHANNEL_ID)
    if channel is None:
        return
    picks = _suggest_picks()
    if picks is None:
        return

    await _refresh_picks(picks)
    msg = _announcement_message(picks)
    await channel.send(content=msg.content, embeds=msg.embeds)


@announce_loop.before_loop
async def _before_announce() -> None:
    await bot.wait_until_ready()


@bot.tree.command(
    name="announce_enable",
    description="Enable the weekly game-night suggestions (optionally set day/time)",
    guild=WATCH_GUILD,
)
@app_commands.describe(
    day="Optional day of week to announce on",
    at="Optional announce time as 24-hour HH:MM AEST, e.g. 16:00",
)
@app_commands.choices(
    day=[app_commands.Choice(name=calendar.day_name[i], value=i) for i in range(7)]
)
async def slash_announce_enable(
    interaction: discord.Interaction,
    day: app_commands.Choice[int] | None = None,
    at: str | None = None,
) -> None:
    if at is not None:
        parsed = _parse_hhmm(at)
        if parsed is None:
            await interaction.response.send_message(
                f"Couldn't read `{at}`. Use a 24-hour time like `16:00`.",
                ephemeral=True,
            )
            return
        hh, mm = parsed
        db.set_setting("announce_time", f"{hh:02d}:{mm:02d}")
        announce_loop.change_interval(time=dtime(hour=hh, minute=mm, tzinfo=ANNOUNCE_TZ))

    if day is not None:
        db.set_setting("announce_weekday", str(day.value))

    db.set_setting("announce_enabled", "1")
    wd = _stored_announce_weekday()
    hh, mm = _stored_announce_hhmm()
    await interaction.response.send_message(
        f"✅ Game-night suggestions **enabled** — every **{calendar.day_name[wd]} at "
        f"{hh:02d}:{mm:02d} AEST** in <#{WATCH_CHANNEL_ID}>.",
        ephemeral=True,
    )


@bot.tree.command(
    name="announce_disable",
    description="Disable the Friday game-night suggestions",
    guild=WATCH_GUILD,
)
async def slash_announce_disable(interaction: discord.Interaction) -> None:
    db.set_setting("announce_enabled", "0")
    await interaction.response.send_message(
        "🛑 Game-night suggestions **disabled**.", ephemeral=True
    )


@bot.tree.command(
    name="announce_status",
    description="Show whether the weekly game-night suggestions are on, and when",
    guild=WATCH_GUILD,
)
async def slash_announce_status(interaction: discord.Interaction) -> None:
    enabled = db.get_setting("announce_enabled") == "1"
    wd = _stored_announce_weekday()
    hh, mm = _stored_announce_hhmm()
    state = "**enabled**" if enabled else "**disabled**"
    await interaction.response.send_message(
        f"Game-night suggestions are {state} — set for **{calendar.day_name[wd]} "
        f"{hh:02d}:{mm:02d} AEST** in <#{WATCH_CHANNEL_ID}>.",
        ephemeral=True,
    )


def _suggestion_embed(g) -> discord.Embed:
    """Minimal card for a game-night pick: name + price (linked), the title
    banner, and who added it.

    Deliberately sparse next to `_game_embed` — the point is just the header image
    to jog the memory of what the game is, not the full store card. Setting only
    title/url/image keeps Discord from rendering its own big store preview (which
    is why the compact `!games` list wraps URLs in <...> to suppress).
    """
    embed = discord.Embed(
        title=f"{g.name} — {_format_price(g)}", url=g.url, color=discord.Color.blurple()
    )
    if g.mentioned_by:
        embed.description = f"added by {_format_mentioners(g.mentioned_by)}"
    if g.header_image:
        embed.set_image(url=g.header_image)
    return embed


def _game_embed(g) -> discord.Embed:
    count = f" • mentioned ×{g.mention_count}" if g.mention_count > 1 else ""
    embed = discord.Embed(
        title=f"{g.name} — {_format_price(g)}{count}",
        url=g.url,
        color=discord.Color.blue(),
    )
    review = _format_review(g)
    if review:
        embed.add_field(name="Reviews", value=review, inline=False)
    if g.mentioned_by:
        embed.add_field(
            name="Mentioned by",
            value=_format_mentioners(g.mentioned_by),
            inline=False,
        )
    # Surface the app id so it's easy to copy for `!remove`. Inline code renders
    # as a tap-to-copy block on mobile and selects cleanly on desktop.
    embed.add_field(
        name="App ID",
        value=f"`{g.app_id}` · remove with `!remove {g.app_id}`",
        inline=False,
    )
    if g.short_description:
        embed.description = _truncate(g.short_description, 280)
    # The header image is the title banner Discord shows in link previews.
    if g.header_image:
        embed.set_image(url=g.header_image)
    return embed


def _format_price(g) -> str:
    if g.is_free:
        return "Free"
    return g.price or "—"


def _format_review(g) -> str | None:
    if not g.review_summary:
        return None
    if g.review_positive_pct is not None and g.review_total:
        return f"⭐ {g.review_summary} ({g.review_positive_pct}% of {g.review_total:,})"
    return f"⭐ {g.review_summary}"


def _truncate(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _jump_url(message_id: int) -> str:
    """Deep link to the original message. All mentions come from the one watched
    channel, so the guild/channel ids are constants."""
    return (
        f"https://discord.com/channels/"
        f"{WATCH_GUILD_ID}/{WATCH_CHANNEL_ID}/{message_id}"
    )


def _md_link(text: str, url: str) -> str:
    """A masked link, escaping the brackets that would otherwise break it.
    (Masked links render only inside embeds, not plain messages.)"""
    safe = text.replace("[", "\\[").replace("]", "\\]")
    return f"[{safe}]({url})"


def _format_mentioners(mentioners, budget: int = 1000) -> str:
    """Comma-separated links to each poster's first mention, trimmed to fit an
    embed field/description without ever cutting a link in half."""
    parts: list[str] = []
    used = 0
    for i, m in enumerate(mentioners):
        # A negative message_id marks a command-added mention with no linkable
        # message (see `_build_add`): render the name as plain (escaped) text.
        if m.message_id < 0:
            label = m.name.replace("[", "\\[").replace("]", "\\]")
        else:
            label = _md_link(m.name, _jump_url(m.message_id))
        extra = len(label) + (2 if parts else 0)  # ", " join
        if used + extra > budget:
            parts.append(f"…(+{len(mentioners) - i} more)")
            break
        parts.append(label)
        used += extra
    return ", ".join(parts)


if __name__ == "__main__":
    try:
        bot.run(TOKEN)
    finally:
        db.close()
