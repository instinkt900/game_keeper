"""Discord bot entrypoint.

Watches one channel in one server for Steam store links. Each link is resolved
to game details (via Steam's appdetails API), stored in SQLite, and can be
recalled with the `!games` command, e.g. `!games 5d` for the last 5 days.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
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

# e.g. "5d", "12h", "1w", "30m". Bare number defaults to days.
_DURATION_RE = re.compile(r"^\s*(\d+)\s*([smhdw]?)\s*$", re.IGNORECASE)
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


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

bot = commands.Bot(command_prefix=COMMAND_PREFIX, intents=intents)
db = Database(DB_PATH)


@bot.event
async def setup_hook() -> None:
    bot.http_session = aiohttp.ClientSession()
    # Push the slash-command definitions to the watched guild (instant).
    await bot.tree.sync(guild=WATCH_GUILD)


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
    """Compact A–Z list of games in a window: name, link, and who added it."""
    delta = parse_duration(window)
    if delta is None:
        return _bad_window(window)

    since = datetime.now(timezone.utc) - delta
    results = db.games_since(since)
    if not results:
        return [_Outbound(content=f"No games mentioned in the last {window}.")]

    results.sort(key=lambda g: g.name.lower())
    lines = [f"**Games mentioned in the last {window}** ({len(results)}):"]
    for i, g in enumerate(results, start=1):
        who = _format_mentioners(g.mentioned_by) if g.mentioned_by else "unknown"
        # <url> shows the plain link without a preview embed; the bracket chars
        # are escaped so the masked name-links inside them render cleanly.
        lines.append(f"{i}. **{g.name}** — <{g.url}> \\[{who}\\]")

    return [_Outbound(content=chunk) for chunk in _chunk_lines(lines)]


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
    """Compact A–Z list of games in a window, e.g. `!games 5d`."""
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
@app_commands.describe(window="Time window like 5d, 12h, 1w (default 7d)")
async def slash_games(interaction: discord.Interaction, window: str = "7d") -> None:
    await interaction.response.defer(ephemeral=True)
    await _send_interaction(interaction, _build_games(window))


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
        link = _md_link(m.name, _jump_url(m.message_id))
        extra = len(link) + (2 if parts else 0)  # ", " join
        if used + extra > budget:
            parts.append(f"…(+{len(mentioners) - i} more)")
            break
        parts.append(link)
        used += extra
    return ", ".join(parts)


if __name__ == "__main__":
    try:
        bot.run(TOKEN)
    finally:
        db.close()
