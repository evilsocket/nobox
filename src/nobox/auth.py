"""GitHub token resolution.

Order of precedence:
  1. GH_TOKEN env var
  2. GITHUB_TOKEN env var
  3. `gh auth token` subprocess (preferred user path)
"""

from __future__ import annotations

import os
import shutil
import subprocess


class AuthError(RuntimeError):
    pass


def resolve_token() -> str:
    """Return a GitHub token or raise AuthError with an actionable message."""
    for var in ("GH_TOKEN", "GITHUB_TOKEN"):
        v = os.environ.get(var)
        if v:
            return v.strip()

    gh = shutil.which("gh")
    if gh is not None:
        try:
            out = subprocess.run(
                [gh, "auth", "token"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            raise AuthError(f"`gh auth token` failed to run: {e}") from e
        token = (out.stdout or "").strip()
        if out.returncode == 0 and token:
            return token

    raise AuthError(
        "No GitHub token found. Either:\n"
        "  - Set GH_TOKEN or GITHUB_TOKEN, or\n"
        "  - Run: gh auth login --scopes 'repo gist'"
    )


def resolve_user_login(token: str) -> str:
    """Look up the authenticated user's login. Imported lazily to avoid cycles."""
    from nobox.github_client import GitHubClient

    client = GitHubClient(token)
    return client.get_authenticated_user()["login"]
