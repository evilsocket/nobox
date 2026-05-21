"""Domain service layer: orchestrates github_client + state + pgp.

CLI, MCP server, and IMAP server all call into here so the business logic
lives in one place.
"""

from __future__ import annotations

from dataclasses import asdict

from nobox import auth, poller, state
from nobox import message as msg_mod
from nobox.github_client import GitHubClient
from nobox.state import InboxRow, MessageRow


def _client() -> GitHubClient:
    return GitHubClient(auth.resolve_token())


def fetch(inbox: InboxRow) -> int:
    """Sync new comments from GitHub into local SQLite. Returns new-count."""
    with _client() as c:
        since = poller.latest_watermark(inbox.name)
        return poller.sync_inbox(c, inbox, since=since)


def read_inbox(
    inbox: InboxRow,
    *,
    unread_only: bool = False,
    limit: int | None = 20,
    since: str | None = None,
    decrypt: bool = True,
) -> list[dict]:
    fetch(inbox)
    with state.connect() as conn:
        rows = state.list_messages(
            conn, inbox.name,
            direction="in",  # previews default to incoming
            unread_only=unread_only,
            since=since,
            limit=limit,
        )
    return [_preview(r, inbox=inbox, decrypt=decrypt) for r in rows]


def read_message(inbox: InboxRow, comment_id: int, *, raw: bool = False) -> dict:
    fetch(inbox)
    with state.connect() as conn:
        row = state.get_message(conn, comment_id)
    if row is None:
        raise LookupError(f"message {comment_id} not found in inbox {inbox.name!r}")
    body = _decrypted_or_trimmed(row, inbox)
    if raw:
        body = row.body_raw
    # Mark seen on demand-read (not on preview list).
    if "\\Seen" not in row.flags:
        with state.connect() as conn:
            state.add_flag(conn, comment_id, "\\Seen")
        row.flags.append("\\Seen")
    return _to_dict(row, body=body)


def send_message(
    inbox: InboxRow,
    body: str,
    *,
    in_reply_to: int | None = None,
) -> dict:
    payload_body = _maybe_encrypt(body, inbox)
    meta = msg_mod.new_outgoing_meta(
        subject=f"Re: {inbox.name}",
        in_reply_to=in_reply_to,
        pgp=inbox.pgp_enabled,
        flags=["\\Seen"],  # we wrote it, of course it's seen
    )
    comment_body = msg_mod.build_comment_body(payload_body, meta)

    assert inbox.repo and inbox.issue_number is not None
    owner, repo = inbox.repo.split("/", 1)
    with _client() as c:
        comment = c.post_issue_comment(owner, repo, inbox.issue_number, comment_body)

    row = MessageRow(
        comment_id=int(comment["id"]),
        inbox_name=inbox.name,
        direction="out",
        author_login=inbox.user_login,
        created_at=comment.get("created_at") or "",
        updated_at=comment.get("updated_at") or "",
        body_raw=comment_body,
        body_trimmed=payload_body,
        body_decrypted=body if inbox.pgp_enabled else None,
        subject=meta.subject,
        in_reply_to_id=in_reply_to,
        flags=["\\Seen"],
        read_at=meta.ts,
        raw_metadata={
            "v": 1, "id": meta.id, "direction": "out", "ts": meta.ts,
            "subject": meta.subject, "in_reply_to": in_reply_to,
            "pgp": inbox.pgp_enabled, "flags": ["\\Seen"],
        },
    )
    with state.connect() as conn:
        state.upsert_message(conn, row)
    return _to_dict(row, body=body)


def mark_flag(inbox: InboxRow, comment_id: int, flag: str, *, add: bool) -> None:
    with state.connect() as conn:
        if add:
            state.add_flag(conn, comment_id, flag)
        else:
            state.remove_flag(conn, comment_id, flag)


# ---------- presentation --------------------------------------------------


PREVIEW_LIMIT = 500


def _preview(row: MessageRow, *, inbox: InboxRow, decrypt: bool) -> dict:
    body = _decrypted_or_trimmed(row, inbox) if decrypt else row.body_trimmed
    return {
        "comment_id": row.comment_id,
        "from": row.author_login,
        "ts": row.created_at,
        "subject": row.subject,
        "flags": row.flags,
        "direction": row.direction,
        "preview": (body[:PREVIEW_LIMIT] + ("…" if len(body) > PREVIEW_LIMIT else "")) if body else "",
    }


def _to_dict(row: MessageRow, *, body: str | None = None) -> dict:
    d = asdict(row)
    if body is not None:
        d["body"] = body
    return d


def _decrypted_or_trimmed(row: MessageRow, inbox: InboxRow) -> str:
    if not inbox.pgp_enabled:
        return row.body_trimmed
    if row.body_decrypted:
        return row.body_decrypted
    # Lazy decrypt
    from nobox import pgp
    block = pgp.find_pgp_block(row.body_trimmed) or pgp.find_pgp_block(row.body_raw)
    if not block:
        return row.body_trimmed
    inbox_dir = state.inbox_dir(inbox.name)
    priv_path = inbox_dir / "pgp.priv.asc"
    if not priv_path.exists():
        return row.body_trimmed
    try:
        plain = pgp.decrypt_and_verify(
            block,
            priv_path.read_text(),
            signer_pub_armored=None,
        )
    except Exception:
        return row.body_trimmed
    # Cache decrypted body
    with state.connect() as conn:
        cached = state.get_message(conn, row.comment_id)
        if cached:
            cached.body_decrypted = plain
            state.upsert_message(conn, cached)
    return plain


def _maybe_encrypt(plain: str, inbox: InboxRow) -> str:
    if not inbox.pgp_enabled:
        return plain
    from nobox import pgp
    inbox_dir = state.inbox_dir(inbox.name)
    priv = (inbox_dir / "pgp.priv.asc").read_text()
    user_pub = (inbox_dir / "user.pub.asc").read_text()
    armored = pgp.encrypt_and_sign(plain, user_pub, priv)
    return f"```text\n{armored.strip()}\n```"


def write_user_pubkey(inbox: InboxRow, key_text: str) -> None:
    (state.inbox_dir(inbox.name) / "user.pub.asc").write_text(key_text)
