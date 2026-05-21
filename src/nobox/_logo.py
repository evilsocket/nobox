"""ASCII logo loader.

Two flavours bundled with the package:

  _logo.ansi  — true-color ANSI (sunset gradient), for terminal banners
  _logo.txt   — plain UTF-8 box-drawing only, for embedding in markdown
                (GitHub issue bodies, README, etc.)

Both produced via `npx oh-my-logo NOBOX sunset --filled`.
"""

from __future__ import annotations

import re
from pathlib import Path

_HERE = Path(__file__).parent

# Matches CSI escape sequences (color + cursor + erase) so the plain version
# we ship through markdown stays free of stray terminal control bytes.
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def _clean(text: str) -> str:
    """Strip ANSI escapes and any leading/trailing blank lines."""
    text = _ANSI_RE.sub("", text)
    lines = text.splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


def _strip_blank_wrappers_keep_ansi(text: str) -> str:
    """Drop blank lines at start/end without touching ANSI bytes mid-line."""
    lines = text.splitlines()
    while lines and not _ANSI_RE.sub("", lines[0]).strip():
        lines.pop(0)
    while lines and not _ANSI_RE.sub("", lines[-1]).strip():
        lines.pop()
    return "\n".join(lines)


LOGO_COLORED = _strip_blank_wrappers_keep_ansi((_HERE / "_logo.ansi").read_text())
LOGO_PLAIN = _clean((_HERE / "_logo.txt").read_text())

# Visible width = max line length of the plain (non-ANSI) form.
LOGO_WIDTH = max((len(line) for line in LOGO_PLAIN.splitlines()), default=0)


def banner(version: str, *, colored: bool = True) -> str:
    """Logo + right-aligned version string. Multi-line; trailing newline included."""
    logo = LOGO_COLORED if colored else LOGO_PLAIN
    tag = f"v{version}"
    pad = " " * max(1, LOGO_WIDTH - len(tag))
    return f"{logo.rstrip()}\n{pad}{tag}\n"


def banner_markdown(version: str) -> str:
    """Logo wrapped in a fenced code block for embedding in markdown."""
    tag = f"v{version}"
    pad = " " * max(1, LOGO_WIDTH - len(tag))
    body = LOGO_PLAIN.rstrip() + "\n" + pad + tag
    return f"```text\n{body}\n```"
