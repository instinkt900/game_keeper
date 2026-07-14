"""Parse Steam links out of message text and fetch game details from Steam."""
from __future__ import annotations

import re
from dataclasses import dataclass

import aiohttp

# Matches store.steampowered.com/app/<appid> (any trailing slug) and the bare
# steamcommunity.com/app/<appid> hub. The negative lookahead excludes community
# sub-sections like /discussions/, /workshop/, /guides/ — those link to a thread
# *about* a game, not a recommendation of it, so they must not capture the app.
_STEAM_APP_RE = re.compile(
    r"https?://(?:"
    r"store\.steampowered\.com/app/(\d+)"
    r"|steamcommunity\.com/app/(\d+)(?!\d|/\S)"
    r")",
    re.IGNORECASE,
)

_APPDETAILS_URL = "https://store.steampowered.com/api/appdetails"
_APPREVIEWS_URL = "https://store.steampowered.com/appreviews/{app_id}"

# Steam prices in the currency of the request's country. Without an explicit `cc`
# Steam geolocates by outbound IP and any game lacking a price for that region
# falls back to another currency (e.g. an unreleased title reporting JPY), so
# pin it. This bot is Australia-based (fixed AEST clock), hence AUD.
_DEFAULT_COUNTRY = "au"


@dataclass
class GameDetails:
    app_id: int
    name: str
    url: str
    short_description: str
    header_image: str
    is_free: bool
    price: str | None  # human-readable, e.g. "$19.99", or None if free/unknown
    review_summary: str | None  # e.g. "Very Positive", or None if no reviews yet
    review_total: int  # total number of reviews
    review_positive_pct: int | None  # 0-100, or None if no reviews yet


def extract_app_ids(text: str) -> list[int]:
    """Return the distinct Steam app IDs referenced in a block of text, in order."""
    seen: dict[int, None] = {}
    for match in _STEAM_APP_RE.finditer(text):
        app_id = match.group(1) or match.group(2)
        seen.setdefault(int(app_id), None)
    return list(seen)


async def fetch_game_details(
    session: aiohttp.ClientSession, app_id: int, cc: str = _DEFAULT_COUNTRY
) -> GameDetails | None:
    """Look up a single app via Steam's public appdetails endpoint.

    `cc` is the ISO country code that fixes the price currency (default AUD).
    Returns None if the app is missing/unavailable (e.g. region-locked, delisted).
    """
    params = {"appids": str(app_id), "cc": cc, "l": "english"}
    async with session.get(_APPDETAILS_URL, params=params) as resp:
        resp.raise_for_status()
        payload = await resp.json()

    entry = payload.get(str(app_id))
    if not entry or not entry.get("success"):
        return None
    data = entry["data"]

    price = None
    if not data.get("is_free") and "price_overview" in data:
        price = data["price_overview"].get("final_formatted")

    reviews = await fetch_review_summary(session, app_id)
    summary, total, positive_pct = reviews if reviews is not None else (None, 0, None)

    return GameDetails(
        app_id=app_id,
        name=data.get("name", f"App {app_id}"),
        url=f"https://store.steampowered.com/app/{app_id}/",
        short_description=data.get("short_description", ""),
        header_image=data.get("header_image", ""),
        is_free=bool(data.get("is_free")),
        price=price,
        review_summary=summary,
        review_total=total,
        review_positive_pct=positive_pct,
    )


async def fetch_review_summary(
    session: aiohttp.ClientSession, app_id: int
) -> tuple[str | None, int, int | None] | None:
    """Fetch the aggregate review standing for an app.

    Steam's appreviews endpoint is separate from appdetails; with num_per_page=0
    we get only the summary. Returns:
      * None                       -- the request failed (so callers can keep
                                       a previously stored value rather than
                                       overwriting it with nothing);
      * (None, total, None)        -- the app genuinely has no reviews yet;
      * (summary, total, pct)      -- a usable review standing.
    """
    params = {
        "json": "1",
        "num_per_page": "0",
        "language": "all",
        "purchase_type": "all",
    }
    try:
        async with session.get(
            _APPREVIEWS_URL.format(app_id=app_id), params=params
        ) as resp:
            resp.raise_for_status()
            payload = await resp.json()
    except aiohttp.ClientError:
        return None

    if payload.get("success") != 1:
        return None
    summary = payload.get("query_summary", {})

    total = summary.get("total_reviews", 0)
    desc = summary.get("review_score_desc")
    if desc in (None, "No user reviews") or total == 0:
        return None, total, None

    positive = summary.get("total_positive", 0)
    positive_pct = round(positive / total * 100) if total else None
    return desc, total, positive_pct
