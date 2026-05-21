"""FastMCP server exposing nobox over stdio.

Wire-up:
    claude mcp add --transport stdio nobox -- nobox mcp

Token-cost rule: `read_inbox` returns previews only (500 chars). `read_message`
fetches a full body on demand. Keeps Claude's context cheap.
"""

from __future__ import annotations

from pathlib import Path

from mcp.server.fastmcp import FastMCP

from nobox import inbox as inbox_mod
from nobox import service, state

SERVER_INSTRUCTIONS = """\
nobox is an async, email-style messaging channel between you and the human
user. A GitHub issue is the transport: you read/post via the API through
these tools; the human reads notification emails and replies via their
normal mail client. Treat this like email — async, durable, NOT real-time.

Reach for these tools when the user wants to be reached later, asks you to
"leave a note", "ping me when done", "check my inbox", or to follow up
asynchronously. Do not use for normal interactive turns.

Key semantics:
  * read_inbox returns ONLY incoming messages (from the human), as 500-char
    previews. Call read_message for a full body — and only for messages you
    intend to act on; read_message auto-marks the message \\Seen.
  * Outgoing messages (your own posts) are not surfaced by read_inbox; they
    live in the local archive.
  * Default behaviour is POP3-style: comments are deleted from GitHub once
    persisted locally. The GitHub issue may look empty even though the local
    SQLite has full history.
  * Latency is real (minutes to hours between your send and a reply). Don't
    poll in a tight loop.
  * Diagnostic "doctor sentinel" comments are filtered automatically — you
    will never see them in read_inbox output.
  * PGP, when enabled on an inbox, is transparent (read_message decrypts;
    send_message encrypts).
  * Use send_message(..., in_reply_to=<comment_id>) to thread replies; the
    human's email client renders the thread coherently.

Polling: nobox is async with no push channel — you only see new replies on
the next read_inbox call. If your runtime supports scheduled/recurring tasks
(Claude Code's /schedule, /loop, cron-style hooks, background routines,
etc.), ask the user ONCE whether they'd like a recurring check every 10
minutes, and only create the schedule after explicit approval. Never set up
a recurring task silently. If the runtime has no such capability, fall back
to manual checks and tell the user you'll only see replies when prompted.

Safety: create_inbox and delete_inbox are destructive. Never call either
without explicit user instruction.
"""

mcp = FastMCP("nobox", instructions=SERVER_INSTRUCTIONS)


@mcp.tool()
def list_inboxes() -> list[dict]:
    """Enumerate configured nobox inboxes.

    Returns one dict per inbox: name, repo, issue_number, user_login,
    created_at, pgp_enabled, pop_mode, url. Call this at the start of a
    session if you don't know which inbox the user wants to use.
    """
    rows = inbox_mod.list_inboxes()
    return [
        {
            **state.inbox_to_dict(r),
            "url": inbox_mod.url_for(r),
        }
        for r in rows
    ]


@mcp.tool()
def read_inbox(
    name: str,
    unread_only: bool = True,
    limit: int = 20,
) -> list[dict]:
    """List 500-char previews of INCOMING messages (from the human user).

    Returns: list of {comment_id, from, ts, subject, flags, preview, direction}.
    Only messages with direction='in' are returned — your own outgoing posts
    are not surfaced here.

    This is the first call to make when checking for new messages. For any
    message you intend to act on, follow up with `read_message` to get the
    full body (cheaper than dumping every full body up front).

    Notes:
      - unread_only=True (default) skips messages already marked \\Seen.
      - Doctor-sentinel diagnostic comments are filtered automatically.
      - pop_mode means messages are deleted from GitHub once persisted; the
        full history still lives locally and is returned here.
    """
    row = inbox_mod.load_inbox(name)
    return service.read_inbox(row, unread_only=unread_only, limit=limit)


@mcp.tool()
def read_message(name: str, comment_id: int, raw: bool = False) -> dict:
    """Fetch the full body of a single message. Marks it \\Seen automatically.

    Use this only for messages you intend to act on — calling it for every
    message wastes context. Quoted-reply text and signatures are trimmed by
    default; pass raw=True for the untrimmed original (useful if the trim
    looks wrong, e.g. with non-English email clients).

    If the inbox has PGP enabled, the returned body is already decrypted.
    """
    row = inbox_mod.load_inbox(name)
    return service.read_message(row, comment_id, raw=raw)


@mcp.tool()
def send_message(name: str, body: str, in_reply_to: int | None = None) -> dict:
    """Post a comment on the inbox issue. GitHub emails the user a notification.

    Use `in_reply_to=<comment_id>` when responding to a specific incoming
    message — it threads the conversation so the human's email client renders
    it coherently. Omit it for unprompted notifications ("task done", etc.).

    The body is plain markdown. If the inbox has PGP enabled, encryption is
    transparent (you provide plaintext; the tool encrypts to the user's
    public key before posting).
    """
    row = inbox_mod.load_inbox(name)
    return service.send_message(row, body, in_reply_to=in_reply_to)


@mcp.tool()
def mark_read(name: str, comment_id: int) -> dict:
    """Mark a message \\Seen locally without fetching its full body.

    Read flags live in the local SQLite store, not on GitHub. Rare in normal
    flow — `read_message` already marks \\Seen as a side effect.
    """
    row = inbox_mod.load_inbox(name)
    service.mark_flag(row, comment_id, "\\Seen", add=True)
    return {"ok": True, "comment_id": comment_id, "flag": "\\Seen"}


@mcp.tool()
def mark_unread(name: str, comment_id: int) -> dict:
    """Remove the \\Seen flag from a message (local only)."""
    row = inbox_mod.load_inbox(name)
    service.mark_flag(row, comment_id, "\\Seen", add=False)
    return {"ok": True, "comment_id": comment_id, "flag": "-\\Seen"}


@mcp.tool()
def create_inbox(
    name: str,
    repo: str,
    pgp: bool = False,
    user_pgp_key_path: str | None = None,
    pop_mode: bool = True,
) -> dict:
    """Create a new inbox backed by a GitHub issue.

    ⚠ Destructive: only call when the user explicitly asks for a new inbox.
    Do not create autonomously.

    `repo` is "OWNER/REPO" — must be a repo the authenticated user owns
    with issues enabled. For PGP, pass `pgp=True` and `user_pgp_key_path`
    pointing at the user's armored public key. `pop_mode=True` (default)
    is the recommended POP3-style: comments are deleted from GitHub once
    persisted locally.
    """
    from nobox.auth import resolve_token
    from nobox.github_client import GitHubClient

    with GitHubClient(resolve_token()) as c:
        row = inbox_mod.create_inbox(
            c,
            name=name,
            repo=repo,
            pgp_enabled=pgp,
            user_pgp_key_path=Path(user_pgp_key_path) if user_pgp_key_path else None,
            pop_mode=pop_mode,
        )
    return {
        **state.inbox_to_dict(row),
        "url": inbox_mod.url_for(row),
    }


@mcp.tool()
def delete_inbox(name: str, local_only: bool = False) -> dict:
    """Fully delete an inbox: GitHub issue + local state.

    ⚠ Destructive: only call when the user explicitly asks.

    Default wipes every comment, then deletes the GitHub issue via GraphQL
    (falls back to title/body wipe + close if GraphQL is denied), and clears
    all local SQLite rows + per-inbox keys. Pass `local_only=True` to keep
    the GitHub issue and only drop the local mirror.
    """
    return inbox_mod.delete_inbox(name, local_only=local_only)


@mcp.tool()
def doctor(name: str | None = None) -> dict:
    """Run preflight + post a sentinel round-trip comment if `name` is given.

    Diagnostic only. The sentinel comment is tagged so it never appears in
    `read_inbox`. Use when the user reports the loop is broken and you want
    to confirm auth, repo access, and notification delivery end-to-end.
    """
    from nobox import doctor as doctor_mod

    return doctor_mod.run(name=name)


def run_stdio() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    run_stdio()
