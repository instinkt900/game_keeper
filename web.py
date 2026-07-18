"""Companion web app: a Discord-OAuth-gated page for voting games off the list.

A deliberately small Flask app that shares the bot's SQLite database (see the
WAL note in ``db.py``). Members of the watched guild log in with Discord, see the
current list of games, and cast one +1 or -1 vote per game. The bot's ``cull_loop``
periodically deletes games whose net score falls below a configurable threshold.

Access is gated twice over: only a logged-in Discord user who is a **member of
``WATCH_GUILD_ID``** ever gets a session, and every page/action requires that
session. A non-member who completes the OAuth flow is shown an "access denied"
page and given no session, so they can neither see the list nor vote.

Run locally:   .venv/bin/python -m flask --app web run --port 8000
In production:  gunicorn -b 0.0.0.0:8000 web:app   (see docker-compose web service)
"""
from __future__ import annotations

import os
import secrets

from authlib.integrations.flask_client import OAuth
from dotenv import load_dotenv
from flask import (
    Flask,
    abort,
    g,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from db import Database

load_dotenv()

# Reused from the bot's config: this is the one server whose members may vote.
WATCH_GUILD_ID = os.environ["WATCH_GUILD_ID"]  # kept as a string for id comparison
DB_PATH = os.environ.get("DB_PATH", "games.db")

# Web-app-only settings. These live alongside the bot's vars in the same .env so
# a staged vs. production split is just a different env file (see README).
DISCORD_CLIENT_ID = os.environ["DISCORD_CLIENT_ID"]
DISCORD_CLIENT_SECRET = os.environ["DISCORD_CLIENT_SECRET"]
# The OAuth2 redirect must exactly match one registered on the Discord app, so
# it differs per environment (localhost for staging, your domain for prod).
DISCORD_REDIRECT_URI = os.environ["DISCORD_REDIRECT_URI"]

app = Flask(__name__)
# Signs the session cookie. Must be stable across restarts (or everyone is logged
# out on every deploy) and secret; generate one with `python -c "import secrets;
# print(secrets.token_hex(32))"`.
app.secret_key = os.environ["WEB_SECRET_KEY"]
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,  # JS can't read the session cookie
    # Lax (not Strict) so the cookie is still sent on the top-level redirect back
    # from discord.com to /callback, while blocking cross-site POSTs to /vote.
    SESSION_COOKIE_SAMESITE="Lax",
    # Set COOKIE_SECURE=1 in any HTTPS deployment so the session cookie is never
    # transmitted over plaintext HTTP. Left off by default for local http testing.
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "").lower()
    in ("1", "true", "yes"),
)

oauth = OAuth(app)
oauth.register(
    name="discord",
    client_id=DISCORD_CLIENT_ID,
    client_secret=DISCORD_CLIENT_SECRET,
    access_token_url="https://discord.com/api/oauth2/token",
    authorize_url="https://discord.com/oauth2/authorize",
    api_base_url="https://discord.com/api/",
    # `identify` gives us the user's id/name; `guilds` lets us confirm membership.
    client_kwargs={
        "scope": "identify guilds",
        "token_endpoint_auth_method": "client_secret_post",
    },
)


# --- Database (one connection per request, closed on teardown) ---------------
# Opening a fresh Database per request keeps each request thread on its own
# connection; WAL mode (set in db.py) makes concurrent access with the bot safe.


def get_db() -> Database:
    if "db" not in g:
        g.db = Database(DB_PATH)
    return g.db


@app.teardown_appcontext
def _close_db(_exc: object) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


# --- Auth helpers ------------------------------------------------------------


def current_user() -> dict | None:
    """The logged-in, verified guild member, or None."""
    return session.get("user")


def _require_user() -> dict:
    user = current_user()
    if user is None:
        abort(403)
    return user


def _csrf_token() -> str:
    """A per-session token guarding the vote POST against cross-site forgery."""
    token = session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf"] = token
    return token


# --- Routes ------------------------------------------------------------------


@app.route("/")
def index():
    user = current_user()
    if user is None:
        return render_template("login.html")

    db = get_db()
    games = db.all_games()
    tallies = db.vote_summary()
    mine = db.user_votes(int(user["id"]))

    # Attach vote data to each game. Sort so the games *this user* hasn't voted on
    # yet float to the top (the ones needing their attention), and games they've
    # already voted on sink to the bottom. Within each group, keep the worst score
    # first (nearest the cull line), then name.
    rows = []
    for game in games:
        t = tallies.get(game.app_id, {"score": 0, "up": 0, "down": 0})
        rows.append(
            {
                "game": game,
                "score": t["score"],
                "up": t["up"],
                "down": t["down"],
                "my_vote": mine.get(game.app_id, 0),
            }
        )
    rows.sort(
        key=lambda r: (r["my_vote"] != 0, r["score"], r["game"].name.lower())
    )

    return render_template(
        "index.html",
        user=user,
        rows=rows,
        csrf_token=_csrf_token(),
    )


@app.route("/login")
def login():
    return oauth.discord.authorize_redirect(DISCORD_REDIRECT_URI)


@app.route("/callback")
def callback():
    token = oauth.discord.authorize_access_token()
    profile = oauth.discord.get("users/@me", token=token).json()
    guilds = oauth.discord.get("users/@me/guilds", token=token).json()

    # Gate on membership: the user must be in the watched guild to get a session.
    guild_ids = {guild.get("id") for guild in guilds} if isinstance(guilds, list) else set()
    if WATCH_GUILD_ID not in guild_ids:
        session.pop("user", None)
        return render_template("denied.html"), 403

    session["user"] = {
        "id": profile["id"],
        "name": profile.get("global_name") or profile["username"],
    }
    return redirect(url_for("index"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


@app.route("/vote", methods=["POST"])
def vote():
    user = _require_user()
    if not secrets.compare_digest(
        request.form.get("csrf", ""), session.get("csrf", "")
    ):
        abort(400)

    try:
        app_id = int(request.form["app_id"])
        direction = int(request.form["direction"])  # +1 or -1
    except (KeyError, ValueError):
        abort(400)
    if direction not in (1, -1):
        abort(400)

    db = get_db()
    user_id = int(user["id"])
    # Toggle: clicking your existing vote clears it; otherwise set the new one.
    if db.user_votes(user_id).get(app_id) == direction:
        db.clear_vote(app_id, user_id)
    else:
        db.cast_vote(app_id, user_id, direction)
    return redirect(url_for("index"))


if __name__ == "__main__":
    app.run(port=int(os.environ.get("WEB_PORT", "8000")))
