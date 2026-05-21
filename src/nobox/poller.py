"""Incremental comment polling.

Reused by `read-inbox`, `doctor`, and the IMAP server's SELECT path.
Fetches new/updated comments from GitHub and upserts them into the local
SQLite store. Pure side effect — does not return decrypted bodies; callers
read via `state.list_messages`.

When `inbox.pop_mode` is True (default), every comment that lands in the
local store is then deleted from GitHub — POP3-style fetch-and-delete.
Persist first, delete second, so a failed delete still leaves a usable
local copy.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from nobox import message as msg_mod
from nobox import parser, state
from nobox.github_client import GitHubError
from nobox.state import InboxRow, MessageRow

if TYPE_CHECKING:
    from nobox.github_client import GitHubClient

log = logging.getLogger(__name__)


def sync_inbox(client: GitHubClient, inbox: InboxRow, *, since: str | None = None) -> int:
    """Pull new comments for `inbox` into SQLite. Returns count of new messages.

    On first call (no prior watermark), paginates fully.
    On subsequent calls, uses `since` to limit the window.
    Idempotent — upserts on comment_id.
    """
    comments = _collect_comments(client, inbox, since)
    new_count = _upsert_all(inbox, comments)
    if inbox.pop_mode and comments:
        _pop_all(client, inbox, [int(c["id"]) for c in comments])
    return new_count


# ---------- fetch ---------------------------------------------------------


def _collect_comments(
    client: GitHubClient, inbox: InboxRow, since: str | None
) -> list[dict]:
    """Fully paginate before mutating. Avoids pagination skew when popping."""
    assert inbox.repo and inbox.issue_number is not None
    out: list[dict] = []
    owner, repo = inbox.repo.split("/", 1)
    for page in client.list_issue_comments(owner, repo, inbox.issue_number, since=since):
        if page.status == 304 or page.data is None:
            continue
        out.extend(page.data)
    return out


# ---------- upsert --------------------------------------------------------


def _upsert_all(inbox: InboxRow, comments: list[dict]) -> int:
    """Translate raw GitHub comment dicts → MessageRows; upsert. Returns new-count.

    System-tagged comments (e.g. doctor sentinels) are skipped — they're
    diagnostic artifacts the agent should never see. pop_mode still deletes
    them remotely on the next sync.
    """
    n = 0
    with state.connect() as conn:
        existing_inbox = state.get_inbox(conn, inbox.name)
        if existing_inbox is None:
            return 0
        owner_login = existing_inbox.user_login
        for c in comments:
            comment_id = int(c["id"])
            author = (c.get("user") or {}).get("login") or "unknown"
            body_raw = c.get("body") or ""
            updated_at = c.get("updated_at") or c.get("created_at") or ""
            created_at = c.get("created_at") or updated_at

            meta = msg_mod.extract_meta(body_raw)
            if meta is not None and meta.get("kind") == "sentinel":
                # diagnostic noise — drop on the floor
                continue
            body_clean = msg_mod.strip_meta(body_raw) if meta else body_raw
            if author == owner_login and meta is not None and meta.get("direction") == "out":
                direction = "out"
                body_trimmed = body_clean
                in_reply = meta.get("in_reply_to")
            else:
                direction = "in"
                body_trimmed = parser.trim_reply(body_clean)
                in_reply = None

            existing_msg = state.get_message(conn, comment_id)
            flags = existing_msg.flags if existing_msg else []
            read_at = existing_msg.read_at if existing_msg else None

            row = MessageRow(
                comment_id=comment_id,
                inbox_name=inbox.name,
                direction=direction,
                author_login=author,
                created_at=created_at,
                updated_at=updated_at,
                body_raw=body_raw,
                body_trimmed=body_trimmed,
                body_decrypted=None,
                subject=(meta or {}).get("subject") or f"Re: {inbox.name}",
                in_reply_to_id=int(in_reply) if isinstance(in_reply, int)
                else (int(in_reply) if isinstance(in_reply, str) and in_reply.isdigit() else None),
                flags=flags,
                read_at=read_at,
                raw_metadata=meta,
            )
            state.upsert_message(conn, row)
            if existing_msg is None:
                n += 1
    return n


# ---------- pop -----------------------------------------------------------


def _pop_all(client: GitHubClient, inbox: InboxRow, comment_ids: list[int]) -> None:
    """Delete each comment from GitHub. Suppresses 404; warns on other errors.

    Local copy is already persisted by the time we get here, so a failed pop
    just means a stale comment lingers on the GitHub side — recoverable.
    """
    assert inbox.repo
    owner, repo = inbox.repo.split("/", 1)
    for cid in comment_ids:
        try:
            client.delete_issue_comment(owner, repo, cid)
        except GitHubError as e:
            if e.status == 404:
                continue  # already gone — fine
            log.warning(
                "nobox: could not pop comment %s from %s (%s): %s",
                cid, inbox.name, e.status, e.body,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("nobox: pop %s raised %s", cid, e)


# ---------- watermark -----------------------------------------------------


def latest_watermark(inbox_name: str) -> str | None:
    """Return ISO created_at of the most recent message we have for this inbox."""
    with state.connect() as conn:
        rows = state.list_messages(conn, inbox_name, limit=None)
    if not rows:
        return None
    return max(r.created_at for r in rows if r.created_at)
