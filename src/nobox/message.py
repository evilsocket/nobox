"""Comment-body metadata codec + message helpers.

Outgoing nobox-authored comments append an HTML-comment block carrying their
identity/threading metadata. The block is invisible in the GitHub web UI but
preserved in the comment's markdown source, so a future read can recover the
metadata from the GitHub side alone (the in-band requirement).

Format (a single line of JSON between the open and close markers):

    <body>

    <!-- nobox-meta
    {"v":1,"id":"…","direction":"out","ts":"…","subject":"…", …}
    -->

We never edit *incoming* (user-authored) comments to add metadata, because
PATCH on a comment authored by another user returns 403. Incoming comments
are tracked entirely in SQLite, keyed by the immutable GitHub comment id.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass

META_OPEN = "<!-- nobox-meta"
META_CLOSE = "-->"
INBOX_MARKER_RE = re.compile(
    r"<!--\s*nobox-inbox\s+(?P<kv>[^>]+?)-->", re.IGNORECASE
)
META_RE = re.compile(
    r"<!--\s*nobox-meta\s*\n(?P<body>.*?)\n\s*-->",
    re.DOTALL | re.IGNORECASE,
)

META_VERSION = 1


@dataclass
class OutgoingMeta:
    id: str
    direction: str  # always "out" for what we generate
    ts: str
    subject: str
    in_reply_to: int | None
    pgp: bool
    flags: list[str]

    def to_block(self) -> str:
        payload = {
            "v": META_VERSION,
            "id": self.id,
            "direction": self.direction,
            "ts": self.ts,
            "subject": self.subject,
            "in_reply_to": self.in_reply_to,
            "pgp": self.pgp,
            "flags": self.flags,
        }
        return f"{META_OPEN}\n{json.dumps(payload, separators=(',', ':'))}\n{META_CLOSE}"


def new_outgoing_meta(
    *,
    subject: str,
    in_reply_to: int | None,
    pgp: bool,
    flags: list[str] | None = None,
) -> OutgoingMeta:
    return OutgoingMeta(
        id=uuid.uuid4().hex,
        direction="out",
        ts=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        subject=subject,
        in_reply_to=in_reply_to,
        pgp=pgp,
        flags=flags or [],
    )


def build_comment_body(body: str, meta: OutgoingMeta) -> str:
    return f"{body.rstrip()}\n\n{meta.to_block()}\n"


def extract_meta(body: str) -> dict | None:
    """Return the parsed nobox-meta dict if present, else None."""
    if not body:
        return None
    m = META_RE.search(body)
    if m is None:
        return None
    try:
        return json.loads(m.group("body"))
    except (ValueError, json.JSONDecodeError):
        return None


def strip_meta(body: str) -> str:
    """Return the body with any trailing nobox-meta block removed."""
    if not body:
        return body
    return META_RE.sub("", body).rstrip() + "\n"


# ---------- inbox body marker --------------------------------------------


def build_inbox_body(
    *,
    user_login: str,
    name: str,
    pgp_pub_armored: str | None,
    uid_validity: int,
) -> str:
    """Issue/gist seed body.

    Contains:
      - The nobox ASCII logo + version
      - @mention of the user
      - Brief intro: what nobox is and how to use it
      - Optional collapsible PGP pubkey block
      - Trailing nobox-inbox HTML marker (used by recovery)
    """
    # Imported here to avoid a top-of-module import cycle with __init__.
    from nobox import __version__
    from nobox._logo import banner_markdown

    lines = [
        banner_markdown(__version__),
        "",
        f"@{user_login}",
        "",
        f"This is your **nobox** inbox: `{name}`.",
        "",
        "**What this is** — a free email-style mailbox between you and an AI",
        "agent, transported over a single GitHub issue.",
        "",
        "**How to use it**",
        "",
        "- **You → agent**: reply to the email notifications GitHub sends you",
        "  for this issue. Your reply appears here as a comment; the agent",
        "  reads it via `nobox read-inbox`.",
        "- **Agent → you**: the agent posts comments here via `nobox send`;",
        "  GitHub emails you each one.",
        "",
        "No SMTP/IMAP setup on your side — your existing email client is the",
        "whole user interface. The agent side talks to the GitHub API.",
    ]
    if pgp_pub_armored:
        lines.extend(
            [
                "",
                "<details><summary>Inbox PGP public key (encrypt replies to this)</summary>",
                "",
                "```text",
                pgp_pub_armored.strip(),
                "```",
                "",
                "</details>",
            ]
        )
    lines.extend(
        [
            "",
            f"<!-- nobox-inbox v=1 name={name} pgp={'true' if pgp_pub_armored else 'false'} "
            f"uid_validity={uid_validity} -->",
            "",
        ]
    )
    return "\n".join(lines)


def parse_inbox_marker(body: str) -> dict | None:
    """Parse the trailing nobox-inbox marker. Returns a dict of k=v pairs."""
    m = INBOX_MARKER_RE.search(body or "")
    if m is None:
        return None
    out: dict[str, str] = {}
    for tok in m.group("kv").strip().split():
        if "=" in tok:
            k, v = tok.split("=", 1)
            out[k] = v
    return out
