"""SQLite storage for watched games and their mentions.

Two tables:
  * games    -- one row per Steam app, the latest known details.
  * mentions -- one row each time the app is linked in the watched channel.
A game can be mentioned many times; recall queries join the two.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from steam import GameDetails

_SCHEMA = """
CREATE TABLE IF NOT EXISTS games (
    app_id              INTEGER PRIMARY KEY,
    name                TEXT NOT NULL,
    url                 TEXT NOT NULL,
    short_description   TEXT,
    header_image        TEXT,
    is_free             INTEGER NOT NULL DEFAULT 0,
    price               TEXT,
    review_summary      TEXT,
    review_total        INTEGER NOT NULL DEFAULT 0,
    review_positive_pct INTEGER
);

CREATE TABLE IF NOT EXISTS mentions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id     INTEGER NOT NULL REFERENCES games(app_id),
    message_id INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    user_name  TEXT NOT NULL,
    created_at TEXT NOT NULL,           -- ISO-8601 UTC
    UNIQUE(app_id, message_id)          -- ignore the same link in the same message
);

CREATE INDEX IF NOT EXISTS idx_mentions_created_at ON mentions(created_at);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS votes (
    app_id   INTEGER NOT NULL REFERENCES games(app_id),
    user_id  INTEGER NOT NULL,           -- Discord user id
    vote     INTEGER NOT NULL,           -- +1 (up) or -1 (down)
    voted_at TEXT NOT NULL,              -- ISO-8601 UTC
    PRIMARY KEY (app_id, user_id)        -- one current vote per user per game
);
"""


# Columns added after the initial schema; applied to pre-existing databases.
_MIGRATIONS = {
    "review_summary": "ALTER TABLE games ADD COLUMN review_summary TEXT",
    "review_total": "ALTER TABLE games ADD COLUMN review_total INTEGER NOT NULL DEFAULT 0",
    "review_positive_pct": "ALTER TABLE games ADD COLUMN review_positive_pct INTEGER",
}

# Columns removed in a later version; dropped from pre-existing databases.
_DROPPED_COLUMNS = ("screenshot",)


@dataclass
class Mentioner:
    name: str  # display name at the time of mention
    message_id: int  # the user's earliest message linking this game


def _parse_mentioner(value: str | None) -> "Mentioner | None":
    """Decode a single "name<US>message_id" pair (the game's original adder)."""
    if not value:
        return None
    name, _, message_id = value.partition("\x1e")
    return Mentioner(name=name, message_id=int(message_id))


@dataclass
class GameMention:
    app_id: int
    name: str
    url: str
    short_description: str
    price: str | None
    is_free: bool
    header_image: str | None
    review_summary: str | None
    review_total: int
    review_positive_pct: int | None
    mention_count: int
    added_by: Mentioner | None  # the original adder (earliest mention) + link target
    last_mentioned: datetime
    first_mentioned: datetime  # earliest mention — how long it's been on the list


class Database:
    def __init__(self, path: str):
        # The bot and the (separate-process) web app both open this same file, so
        # run in WAL mode: readers don't block the writer and vice versa. A busy
        # timeout lets a write wait out a concurrent one instead of raising
        # "database is locked". check_same_thread=False lets a threaded web server
        # reuse one connection across request threads (sqlite3 serializes calls).
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """Reconcile databases created by an earlier version: add new columns and
        drop ones that have since been removed."""
        existing = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(games)").fetchall()
        }
        for column, ddl in _MIGRATIONS.items():
            if column not in existing:
                self._conn.execute(ddl)
        for column in _DROPPED_COLUMNS:
            if column in existing:
                self._conn.execute(f"ALTER TABLE games DROP COLUMN {column}")

    def upsert_game(self, game: GameDetails) -> None:
        """Insert or refresh the stored details for a game."""
        self._conn.execute(
            """
            INSERT INTO games (app_id, name, url, short_description,
                               header_image, is_free, price,
                               review_summary, review_total, review_positive_pct)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(app_id) DO UPDATE SET
                name=excluded.name,
                url=excluded.url,
                short_description=excluded.short_description,
                header_image=excluded.header_image,
                is_free=excluded.is_free,
                price=excluded.price,
                review_summary=excluded.review_summary,
                review_total=excluded.review_total,
                review_positive_pct=excluded.review_positive_pct
            """,
            (
                game.app_id,
                game.name,
                game.url,
                game.short_description,
                game.header_image,
                int(game.is_free),
                game.price,
                game.review_summary,
                game.review_total,
                game.review_positive_pct,
            ),
        )
        self._conn.commit()

    def record_mention(
        self,
        app_id: int,
        message_id: int,
        user_id: int,
        user_name: str,
        when: datetime,
    ) -> bool:
        """Record that a game was mentioned. Returns False if it was a duplicate."""
        cur = self._conn.execute(
            """
            INSERT OR IGNORE INTO mentions
                (app_id, message_id, user_id, user_name, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (app_id, message_id, user_id, user_name, when.astimezone(timezone.utc).isoformat()),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def games_since(self, since: datetime) -> list[GameMention]:
        """Games mentioned at or after `since`, most-recently-mentioned first."""
        rows = self._conn.execute(
            """
            -- The game's *original adder*: the single earliest mention (in the
            -- window), whose message is what we link back to for context.
            WITH adder AS (
                SELECT app_id, user_name, message_id,
                       ROW_NUMBER() OVER (
                           PARTITION BY app_id
                           ORDER BY created_at, id
                       ) AS rn
                FROM mentions
                WHERE created_at >= :since
            )
            SELECT g.app_id, g.name, g.url, g.short_description, g.price, g.is_free,
                   g.header_image,
                   g.review_summary, g.review_total, g.review_positive_pct,
                   COUNT(m.id)        AS mention_count,
                   MAX(m.created_at)  AS last_mentioned,
                   MIN(m.created_at)  AS first_mentioned,
                   -- "name<US>message_id"; the control-char separator can't appear
                   -- in a display name, so splitting it back apart is safe.
                   (SELECT a.user_name || char(30) || a.message_id
                    FROM adder a
                    WHERE a.app_id = g.app_id AND a.rn = 1) AS added_by
            FROM mentions m
            JOIN games g ON g.app_id = m.app_id
            WHERE m.created_at >= :since
            GROUP BY g.app_id
            ORDER BY last_mentioned DESC
            """,
            {"since": since.astimezone(timezone.utc).isoformat()},
        ).fetchall()

        return [
            GameMention(
                app_id=r["app_id"],
                name=r["name"],
                url=r["url"],
                short_description=r["short_description"] or "",
                price=r["price"],
                is_free=bool(r["is_free"]),
                header_image=r["header_image"],
                review_summary=r["review_summary"],
                review_total=r["review_total"],
                review_positive_pct=r["review_positive_pct"],
                mention_count=r["mention_count"],
                added_by=_parse_mentioner(r["added_by"]),
                last_mentioned=datetime.fromisoformat(r["last_mentioned"]),
                first_mentioned=datetime.fromisoformat(r["first_mentioned"]),
            )
            for r in rows
        ]

    def update_reviews(
        self,
        app_id: int,
        review_summary: str | None,
        review_total: int,
        review_positive_pct: int | None,
    ) -> None:
        """Refresh only the review columns for a game (used at recall time)."""
        self._conn.execute(
            """
            UPDATE games
            SET review_summary=?, review_total=?, review_positive_pct=?
            WHERE app_id=?
            """,
            (review_summary, review_total, review_positive_pct, app_id),
        )
        self._conn.commit()

    def remove_game(self, app_id: int) -> str | None:
        """Delete a game and all of its mentions.

        Returns the removed game's name, or None if it wasn't stored.
        """
        row = self._conn.execute(
            "SELECT name FROM games WHERE app_id=?", (app_id,)
        ).fetchone()
        if row is None:
            return None
        self._conn.execute("DELETE FROM votes WHERE app_id=?", (app_id,))
        self._conn.execute("DELETE FROM mentions WHERE app_id=?", (app_id,))
        self._conn.execute("DELETE FROM games WHERE app_id=?", (app_id,))
        self._conn.commit()
        return row["name"]

    def all_app_ids(self) -> list[int]:
        """Every app currently stored, for backfilling/refreshing details."""
        rows = self._conn.execute("SELECT app_id FROM games ORDER BY app_id").fetchall()
        return [r["app_id"] for r in rows]

    def all_games(self) -> list[GameMention]:
        """Every stored game with its mention info (for random suggestions)."""
        return self.games_since(datetime.fromtimestamp(0, timezone.utc))

    # --- Voting (driven by the companion web app) ---------------------------

    def cast_vote(self, app_id: int, user_id: int, vote: int) -> None:
        """Record a user's +1/-1 vote for a game, replacing any prior vote."""
        if vote not in (1, -1):
            raise ValueError("vote must be +1 or -1")
        self._conn.execute(
            """
            INSERT INTO votes (app_id, user_id, vote, voted_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(app_id, user_id) DO UPDATE SET
                vote=excluded.vote, voted_at=excluded.voted_at
            """,
            (app_id, user_id, vote, datetime.now(timezone.utc).isoformat()),
        )
        self._conn.commit()

    def clear_vote(self, app_id: int, user_id: int) -> None:
        """Remove a user's vote for a game (un-vote / toggle off)."""
        self._conn.execute(
            "DELETE FROM votes WHERE app_id=? AND user_id=?", (app_id, user_id)
        )
        self._conn.commit()

    def vote_summary(self) -> dict[int, dict[str, int]]:
        """Per-game tallies: {app_id: {"score", "up", "down"}} for voted games.

        Games with no votes are omitted; callers should treat them as all-zero.
        """
        rows = self._conn.execute(
            """
            SELECT app_id,
                   COALESCE(SUM(vote), 0)                        AS score,
                   SUM(CASE WHEN vote > 0 THEN 1 ELSE 0 END)     AS up,
                   SUM(CASE WHEN vote < 0 THEN 1 ELSE 0 END)     AS down
            FROM votes
            GROUP BY app_id
            """
        ).fetchall()
        return {
            r["app_id"]: {"score": r["score"], "up": r["up"], "down": r["down"]}
            for r in rows
        }

    def user_votes(self, user_id: int) -> dict[int, int]:
        """Map of app_id -> this user's current vote (+1/-1), for the web UI."""
        rows = self._conn.execute(
            "SELECT app_id, vote FROM votes WHERE user_id=?", (user_id,)
        ).fetchall()
        return {r["app_id"]: r["vote"] for r in rows}

    def cull_below(self, threshold: int) -> list[tuple[int, str]]:
        """Delete games whose net vote score is below `threshold`.

        Only games that have received at least one vote are eligible — a game
        nobody has voted on (score effectively 0) is never culled, so freshly
        added games can't be swept away before anyone has seen them. Returns the
        (app_id, name) pairs removed.
        """
        rows = self._conn.execute(
            """
            SELECT g.app_id AS app_id, g.name AS name, SUM(v.vote) AS score
            FROM games g
            JOIN votes v ON v.app_id = g.app_id
            GROUP BY g.app_id
            HAVING SUM(v.vote) < ?
            """,
            (threshold,),
        ).fetchall()
        removed = [(r["app_id"], r["name"]) for r in rows]
        for app_id, _ in removed:
            self.remove_game(app_id)
        return removed

    def get_setting(self, key: str) -> str | None:
        """Read a persisted key/value setting, or None if unset."""
        row = self._conn.execute(
            "SELECT value FROM settings WHERE key=?", (key,)
        ).fetchone()
        return row["value"] if row else None

    def set_setting(self, key: str, value: str) -> None:
        """Persist a key/value setting, overwriting any existing value."""
        self._conn.execute(
            """
            INSERT INTO settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            (key, value),
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()
