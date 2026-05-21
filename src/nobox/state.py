"""SQLite-backed local state + XDG path helpers.

Source of truth for *mutable* state (flags, read status, drafts). The remote
GitHub side stores its own immutable identity (comment IDs) plus, for
outgoing nobox-authored comments only, an in-band metadata block.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path

from platformdirs import (
    user_cache_dir,
    user_config_dir,
    user_data_dir,
    user_state_dir,
)

APP_NAME = "nobox"


# ---------- XDG paths -----------------------------------------------------


def config_dir() -> Path:
    p = Path(user_config_dir(APP_NAME))
    p.mkdir(parents=True, exist_ok=True)
    return p


def data_dir() -> Path:
    p = Path(user_data_dir(APP_NAME))
    p.mkdir(parents=True, exist_ok=True)
    return p


def state_dir() -> Path:
    p = Path(user_state_dir(APP_NAME))
    p.mkdir(parents=True, exist_ok=True)
    return p


def cache_dir() -> Path:
    p = Path(user_cache_dir(APP_NAME))
    p.mkdir(parents=True, exist_ok=True)
    return p


def db_path() -> Path:
    return data_dir() / "inboxes.db"


def inbox_dir(name: str) -> Path:
    """Per-inbox private dir (keys, passwords). Lives under XDG_STATE_HOME."""
    p = state_dir() / "inboxes" / name
    p.mkdir(parents=True, exist_ok=True)
    return p


# ---------- Row dataclasses -----------------------------------------------


@dataclass
class InboxRow:
    name: str
    kind: str  # "issue" | "gist"
    repo: str | None
    issue_number: int | None
    gist_id: str | None
    user_login: str
    created_at: str  # ISO-8601 UTC
    uid_validity: int
    pgp_enabled: bool
    imap_password: str
    pop_mode: bool = True  # POP3-style: delete from GitHub after persist


@dataclass
class MessageRow:
    comment_id: int
    inbox_name: str
    direction: str  # "in" | "out"
    author_login: str
    created_at: str  # GitHub created_at
    updated_at: str
    body_raw: str
    body_trimmed: str
    body_decrypted: str | None
    subject: str
    in_reply_to_id: int | None
    flags: list[str] = field(default_factory=list)
    read_at: str | None = None
    raw_metadata: dict | None = None


@dataclass
class DraftRow:
    draft_id: str
    inbox_name: str
    body: str
    in_reply_to_id: int | None
    created_at: str


# ---------- Connection ----------------------------------------------------


_SCHEMA = """
CREATE TABLE IF NOT EXISTS inbox (
  name           TEXT PRIMARY KEY,
  kind           TEXT NOT NULL CHECK (kind IN ('issue','gist')),
  repo           TEXT,
  issue_number   INTEGER,
  gist_id        TEXT,
  user_login     TEXT NOT NULL,
  created_at     TEXT NOT NULL,
  uid_validity   INTEGER NOT NULL,
  pgp_enabled    INTEGER NOT NULL DEFAULT 0,
  imap_password  TEXT NOT NULL,
  pop_mode       INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS message (
  comment_id      INTEGER PRIMARY KEY,
  inbox_name      TEXT NOT NULL REFERENCES inbox(name) ON DELETE CASCADE,
  direction       TEXT NOT NULL CHECK (direction IN ('in','out')),
  author_login    TEXT,
  created_at      TEXT NOT NULL,
  updated_at      TEXT,
  body_raw        TEXT NOT NULL,
  body_trimmed    TEXT NOT NULL,
  body_decrypted  TEXT,
  subject         TEXT,
  in_reply_to_id  INTEGER,
  flags           TEXT NOT NULL DEFAULT '[]',
  read_at         TEXT,
  raw_metadata    TEXT
);

CREATE INDEX IF NOT EXISTS idx_message_inbox_created
  ON message(inbox_name, created_at);

CREATE TABLE IF NOT EXISTS etag_cache (
  endpoint      TEXT PRIMARY KEY,
  etag          TEXT,
  last_modified TEXT,
  fetched_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS draft (
  draft_id        TEXT PRIMARY KEY,
  inbox_name      TEXT NOT NULL REFERENCES inbox(name) ON DELETE CASCADE,
  body            TEXT NOT NULL,
  in_reply_to_id  INTEGER,
  created_at      TEXT NOT NULL
);
"""


@contextmanager
def connect(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    p = path or db_path()
    conn = sqlite3.connect(p, isolation_level=None)  # autocommit
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    try:
        conn.executescript(_SCHEMA)
        _migrate(conn)
        yield conn
    finally:
        conn.close()


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after the initial schema. Idempotent."""
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(inbox)").fetchall()}
    if "pop_mode" not in cols:
        conn.execute("ALTER TABLE inbox ADD COLUMN pop_mode INTEGER NOT NULL DEFAULT 1")


# ---------- Inbox CRUD ----------------------------------------------------


def insert_inbox(conn: sqlite3.Connection, row: InboxRow) -> None:
    conn.execute(
        """INSERT INTO inbox
           (name, kind, repo, issue_number, gist_id, user_login,
            created_at, uid_validity, pgp_enabled, imap_password, pop_mode)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (
            row.name, row.kind, row.repo, row.issue_number, row.gist_id,
            row.user_login, row.created_at, row.uid_validity,
            int(row.pgp_enabled), row.imap_password, int(row.pop_mode),
        ),
    )


def get_inbox(conn: sqlite3.Connection, name: str) -> InboxRow | None:
    r = conn.execute("SELECT * FROM inbox WHERE name = ?", (name,)).fetchone()
    return _row_to_inbox(r) if r else None


def list_inboxes(conn: sqlite3.Connection) -> list[InboxRow]:
    rows = conn.execute("SELECT * FROM inbox ORDER BY created_at ASC").fetchall()
    return [_row_to_inbox(r) for r in rows]


def delete_inbox(conn: sqlite3.Connection, name: str) -> None:
    conn.execute("DELETE FROM inbox WHERE name = ?", (name,))


def _row_to_inbox(r: sqlite3.Row) -> InboxRow:
    return InboxRow(
        name=r["name"],
        kind=r["kind"],
        repo=r["repo"],
        issue_number=r["issue_number"],
        gist_id=r["gist_id"],
        user_login=r["user_login"],
        created_at=r["created_at"],
        uid_validity=r["uid_validity"],
        pgp_enabled=bool(r["pgp_enabled"]),
        imap_password=r["imap_password"],
        pop_mode=bool(r["pop_mode"]) if "pop_mode" in r.keys() else True,  # noqa: SIM118
    )


# ---------- Message CRUD --------------------------------------------------


def upsert_message(conn: sqlite3.Connection, m: MessageRow) -> None:
    conn.execute(
        """INSERT INTO message
           (comment_id, inbox_name, direction, author_login,
            created_at, updated_at, body_raw, body_trimmed, body_decrypted,
            subject, in_reply_to_id, flags, read_at, raw_metadata)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(comment_id) DO UPDATE SET
             updated_at=excluded.updated_at,
             body_raw=excluded.body_raw,
             body_trimmed=excluded.body_trimmed,
             body_decrypted=excluded.body_decrypted,
             subject=excluded.subject,
             in_reply_to_id=excluded.in_reply_to_id,
             raw_metadata=excluded.raw_metadata""",
        (
            m.comment_id, m.inbox_name, m.direction, m.author_login,
            m.created_at, m.updated_at, m.body_raw, m.body_trimmed,
            m.body_decrypted, m.subject, m.in_reply_to_id,
            json.dumps(m.flags), m.read_at,
            json.dumps(m.raw_metadata) if m.raw_metadata is not None else None,
        ),
    )


def get_message(conn: sqlite3.Connection, comment_id: int) -> MessageRow | None:
    r = conn.execute(
        "SELECT * FROM message WHERE comment_id = ?", (comment_id,)
    ).fetchone()
    return _row_to_message(r) if r else None


def list_messages(
    conn: sqlite3.Connection,
    inbox_name: str,
    *,
    direction: str | None = None,
    unread_only: bool = False,
    since: str | None = None,
    limit: int | None = None,
) -> list[MessageRow]:
    sql = "SELECT * FROM message WHERE inbox_name = ?"
    params: list = [inbox_name]
    if direction is not None:
        sql += " AND direction = ?"
        params.append(direction)
    if unread_only:
        sql += " AND (flags NOT LIKE '%\"\\\\Seen\"%')"
    if since is not None:
        sql += " AND created_at >= ?"
        params.append(since)
    sql += " ORDER BY created_at ASC"
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    rows = conn.execute(sql, params).fetchall()
    return [_row_to_message(r) for r in rows]


def set_flags(conn: sqlite3.Connection, comment_id: int, flags: list[str]) -> None:
    read_at = _now_iso() if "\\Seen" in flags else None
    conn.execute(
        "UPDATE message SET flags = ?, read_at = COALESCE(read_at, ?) WHERE comment_id = ?",
        (json.dumps(flags), read_at, comment_id),
    )


def add_flag(conn: sqlite3.Connection, comment_id: int, flag: str) -> None:
    m = get_message(conn, comment_id)
    if m is None:
        return
    if flag in m.flags:
        return
    set_flags(conn, comment_id, [*m.flags, flag])


def remove_flag(conn: sqlite3.Connection, comment_id: int, flag: str) -> None:
    m = get_message(conn, comment_id)
    if m is None:
        return
    if flag not in m.flags:
        return
    set_flags(conn, comment_id, [f for f in m.flags if f != flag])


def _row_to_message(r: sqlite3.Row) -> MessageRow:
    return MessageRow(
        comment_id=r["comment_id"],
        inbox_name=r["inbox_name"],
        direction=r["direction"],
        author_login=r["author_login"],
        created_at=r["created_at"],
        updated_at=r["updated_at"],
        body_raw=r["body_raw"],
        body_trimmed=r["body_trimmed"],
        body_decrypted=r["body_decrypted"],
        subject=r["subject"],
        in_reply_to_id=r["in_reply_to_id"],
        flags=json.loads(r["flags"] or "[]"),
        read_at=r["read_at"],
        raw_metadata=json.loads(r["raw_metadata"]) if r["raw_metadata"] else None,
    )


# ---------- ETag cache ----------------------------------------------------


def get_etag(conn: sqlite3.Connection, endpoint: str) -> tuple[str | None, str | None]:
    r = conn.execute(
        "SELECT etag, last_modified FROM etag_cache WHERE endpoint = ?",
        (endpoint,),
    ).fetchone()
    if r is None:
        return (None, None)
    return (r["etag"], r["last_modified"])


def set_etag(
    conn: sqlite3.Connection,
    endpoint: str,
    etag: str | None,
    last_modified: str | None,
) -> None:
    conn.execute(
        """INSERT INTO etag_cache (endpoint, etag, last_modified, fetched_at)
           VALUES (?,?,?,?)
           ON CONFLICT(endpoint) DO UPDATE SET
             etag=excluded.etag,
             last_modified=excluded.last_modified,
             fetched_at=excluded.fetched_at""",
        (endpoint, etag, last_modified, _now_iso()),
    )


# ---------- Drafts --------------------------------------------------------


def insert_draft(conn: sqlite3.Connection, d: DraftRow) -> None:
    conn.execute(
        """INSERT INTO draft (draft_id, inbox_name, body, in_reply_to_id, created_at)
           VALUES (?,?,?,?,?)""",
        (d.draft_id, d.inbox_name, d.body, d.in_reply_to_id, d.created_at),
    )


def list_drafts(conn: sqlite3.Connection, inbox_name: str) -> list[DraftRow]:
    rows = conn.execute(
        "SELECT * FROM draft WHERE inbox_name = ? ORDER BY created_at ASC",
        (inbox_name,),
    ).fetchall()
    return [
        DraftRow(
            draft_id=r["draft_id"],
            inbox_name=r["inbox_name"],
            body=r["body"],
            in_reply_to_id=r["in_reply_to_id"],
            created_at=r["created_at"],
        )
        for r in rows
    ]


def delete_draft(conn: sqlite3.Connection, draft_id: str) -> None:
    conn.execute("DELETE FROM draft WHERE draft_id = ?", (draft_id,))


# ---------- Helpers -------------------------------------------------------


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def inbox_to_dict(row: InboxRow) -> dict:
    d = asdict(row)
    d.pop("imap_password", None)  # never leak via JSON
    return d


def write_secret_file(path: Path, content: str) -> None:
    """Write a file with mode 0600. Used for private keys + IMAP passwords."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, content.encode("utf-8"))
    finally:
        os.close(fd)
