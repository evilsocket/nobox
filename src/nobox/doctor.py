"""Preflight + end-to-end self-test.

Posts a sentinel comment on the inbox issue and prints what the user should
check next (the email should land in their inbox). GitHub doesn't expose
notification preferences via API, so the ground-truth "did the email arrive"
check is unavoidably visual — we just make the failure modes legible.
"""

from __future__ import annotations

import time

from nobox import auth
from nobox import inbox as inbox_mod
from nobox.github_client import GitHubClient

SELF_NOTIFY_FIX = (
    "GitHub suppresses email notifications for your OWN actions by default,\n"
    "and nobox acts as you (via your token). If you don't get the sentinel\n"
    "email, the fix is:\n"
    "\n"
    "  1. Visit https://github.com/settings/notifications\n"
    "  2. Under 'Email notification preferences', enable\n"
    "     'Include your own updates'\n"
    "  3. Repost a sentinel with `nobox doctor --name <inbox>`\n"
    "\n"
    "Alternative: authenticate nobox with a separate bot GitHub account\n"
    "that @mentions you. Different actor = normal notification path."
)


def run(*, name: str | None = None) -> dict:
    """Run preflight checks. If `name` given, also post a sentinel comment."""
    result: dict = {"ok": True, "checks": []}

    def check(label: str, fn):
        try:
            value = fn()
            result["checks"].append({"name": label, "ok": True, "info": value})
        except Exception as e:
            result["ok"] = False
            result["checks"].append({"name": label, "ok": False, "error": str(e)})

    try:
        token = auth.resolve_token()
    except auth.AuthError as e:
        return {"ok": False, "checks": [{"name": "auth", "ok": False, "error": str(e)}]}

    with GitHubClient(token) as c:
        authed_login: str | None = None
        try:
            authed_login = c.get_authenticated_user()["login"]
            result["checks"].append(
                {"name": "authenticated user", "ok": True, "info": authed_login}
            )
        except Exception as e:
            result["ok"] = False
            result["checks"].append(
                {"name": "authenticated user", "ok": False, "error": str(e)}
            )

        check(
            "rate limit",
            lambda: {
                k: c.get_rate_limit()["resources"]["core"][k]
                for k in ("remaining", "limit")
            },
        )

        if name:
            try:
                row = inbox_mod.load_inbox(name)
            except inbox_mod.InboxError as e:
                result["ok"] = False
                result["checks"].append(
                    {"name": f"inbox:{name}", "ok": False, "error": str(e)}
                )
                return result

            same_actor = authed_login is not None and authed_login == row.user_login
            result["self_action"] = same_actor
            if same_actor:
                result["self_action_warning"] = (
                    f"Inbox owner ({row.user_login!r}) is the same account as the "
                    f"token holder. If you don't receive the sentinel email, enable "
                    f"'Include your own updates' at "
                    f"https://github.com/settings/notifications."
                )

            check(
                f"repo {row.repo} has_issues",
                lambda: c.get_repo(*row.repo.split("/", 1))["has_issues"],
            )
            check(
                f"issue #{row.issue_number} state",
                lambda: c.get_issue(*row.repo.split("/", 1), row.issue_number)["state"],
            )

            try:
                sentinel = _sentinel(c, row)
                result["checks"].append(
                    {"name": "sentinel comment", "ok": True, "info": sentinel}
                )
            except Exception as e:
                result["ok"] = False
                result["checks"].append(
                    {"name": "sentinel comment", "ok": False, "error": str(e)}
                )

            result["sentinel_hint"] = SELF_NOTIFY_FIX

    return result


def _sentinel(c: GitHubClient, inbox) -> dict:
    import json as _json
    import uuid as _uuid

    meta = {
        "v": 1,
        "id": _uuid.uuid4().hex,
        "kind": "sentinel",
        "direction": "system",
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    body = (
        "**nobox doctor sentinel** — if you received an email with this "
        "comment, your end-to-end loop is working.\n\n"
        f"_(posted at {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())})_\n"
        "\n<!-- nobox-meta\n" + _json.dumps(meta, separators=(",", ":")) + "\n-->\n"
    )
    owner, repo = inbox.repo.split("/", 1)
    comment = c.post_issue_comment(owner, repo, inbox.issue_number, body)
    return {"comment_id": int(comment["id"]), "url": comment.get("html_url")}
