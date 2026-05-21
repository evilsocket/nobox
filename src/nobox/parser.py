"""Email-reply trimming.

GitHub posts the raw email body verbatim when a user replies. Most clients
include quoted history and a signature; we strip those before showing the
message to the agent. The raw body is always retained in SQLite so `--raw`
can recover the original.

Rules (applied in order):
  1. Cut at the first "On <date>, <name> wrote:" boundary (and any
     localised variants we know about).
  2. Cut at the first signature delimiter line: "-- " (dash dash space).
  3. Strip trailing lines that begin with "> " (quote markers).

Preserves markdown image links GitHub injects for email attachments.
"""

from __future__ import annotations

import re

# "On Wed, May 21, 2026 at 1:23 PM, Foo <foo@bar> wrote:"
# "On 2026-05-21 13:23, foo wrote:"
# Multiple language variants are added on demand; the English one covers
# Gmail / Apple Mail / Outlook defaults.
_REPLY_BOUNDARY = re.compile(
    r"^\s*On\s.{1,200}\bwrote:\s*$",
    re.MULTILINE,
)

# Signature delimiter per RFC 3676 §4.3 (sig dash-dash-space).
_SIG_DELIM = re.compile(r"^--\s?$", re.MULTILINE)

# Trailing quoted block: contiguous lines starting with "> ".
_QUOTE_TRAILING = re.compile(r"(?:\n(?:>.*|))+\s*\Z")


def trim_reply(body: str) -> str:
    if not body:
        return body

    # 1. Quoted-boundary cut: everything from "On … wrote:" onward.
    m = _REPLY_BOUNDARY.search(body)
    if m is not None:
        body = body[: m.start()]

    # 2. Signature delimiter.
    m = _SIG_DELIM.search(body)
    if m is not None:
        body = body[: m.start()]

    # 3. Trailing quoted lines.
    body = _QUOTE_TRAILING.sub("", body)

    return body.rstrip() + ("\n" if body and not body.endswith("\n") else "")


def looks_quoted(line: str) -> bool:
    return line.startswith("> ") or line == ">"
