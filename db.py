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


def _parse_mentioners(concat: str | None) -> list["Mentioner"]:
    """Decode the GROUP_CONCAT blob of "name<RS>message_id" pairs."""
    if not concat:
        return []
    out: list[Mentioner] = []
    for entry in concat.split("\x1f"):
        name, _, message_id = entry.partition("\x1e")
        out.append(Mentioner(name=name, message_id=int(message_id)))
    return out


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
    mentioned_by: list[Mentioner]  # distinct posters + a link target per poster
    last_mentioned: datetime


class Database:
    def __init__(self, path: str):
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
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
            -- One row per (game, poster): the poster's *earliest* mention, which
            -- is the message we link back to for context.
            WITH first_mention AS (
                SELECT app_id, user_name, message_id, created_at,
                       ROW_NUMBER() OVER (
                           PARTITION BY app_id, user_name
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
                   -- "name<RS>message_id" pairs joined by <US>; the control-char
                   -- separators can't appear in display names, so splitting is safe
                   (SELECT GROUP_CONCAT(
                               f.user_name || char(30) || f.message_id, char(31))
                    FROM first_mention f
                    WHERE f.app_id = g.app_id AND f.rn = 1) AS mentioned_by
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
                mentioned_by=_parse_mentioners(r["mentioned_by"]),
                last_mentioned=datetime.fromisoformat(r["last_mentioned"]),
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
        self._conn.execute("DELETE FROM mentions WHERE app_id=?", (app_id,))
        self._conn.execute("DELETE FROM games WHERE app_id=?", (app_id,))
        self._conn.commit()
        return row["name"]

    def all_app_ids(self) -> list[int]:
        """Every app currently stored, for backfilling/refreshing details."""
        rows = self._conn.execute("SELECT app_id FROM games ORDER BY app_id").fetchall()
        return [r["app_id"] for r in rows]

    def close(self) -> None:
        self._conn.close()
