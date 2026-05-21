"""Inbox lifecycle: create, list, load, delete.

An inbox is a GitHub issue plus a row in our local `inbox` table plus a
directory under $XDG_STATE_HOME/nobox/inboxes/<name>/ holding optional PGP
keys and the local IMAP password.
"""

from __future__ import annotations

import json
import secrets
import shutil
import time
from dataclasses import asdict
from pathlib import Path

from nobox import auth, message, pgp, state
from nobox.github_client import GitHubClient, GitHubError
from nobox.state import InboxRow


class InboxError(RuntimeError):
    pass


def create_inbox(
    client: GitHubClient,
    *,
    name: str,
    repo: str,
    pgp_enabled: bool = False,
    user_pgp_key_path: Path | None = None,
    pop_mode: bool = True,
) -> InboxRow:
    if not repo or "/" not in repo:
        raise InboxError("repo must be OWNER/REPO")

    with state.connect() as conn:
        if state.get_inbox(conn, name) is not None:
            raise InboxError(
                f"inbox {name!r} already exists locally. "
                f"Remove it first with `nobox delete-inbox --name {name}` "
                f"(add --remote to also close the GitHub issue)."
            )

    user = client.get_authenticated_user()
    user_login = user["login"]

    owner, repo_name = repo.split("/", 1)
    repo_info = client.get_repo(owner, repo_name)
    if not repo_info.get("has_issues"):
        raise InboxError(f"repo {repo!r} has issues disabled")

    inbox_dir = state.inbox_dir(name)

    # PGP keypair (optional)
    pgp_pub_armored: str | None = None
    pgp_fp = ""
    user_fp = ""
    if pgp_enabled:
        if user_pgp_key_path is None:
            raise InboxError("--user-pgp-key PATH is required with --pgp")
        keys = pgp.generate_inbox_keypair(uid=f"nobox-{name} <noreply@nobox.local>")
        pgp_pub_armored = keys.pub_armored
        pgp_fp = keys.fingerprint
        state.write_secret_file(inbox_dir / "pgp.priv.asc", keys.priv_armored)
        (inbox_dir / "pgp.pub.asc").write_text(keys.pub_armored)
        user_key = Path(user_pgp_key_path).read_text()
        (inbox_dir / "user.pub.asc").write_text(user_key)
        user_fp = pgp.fingerprint_of(user_key)

    # Local IMAP password
    imap_password = secrets.token_urlsafe(16)
    state.write_secret_file(inbox_dir / "imap.password", imap_password)

    # Build seed body and create the GitHub object
    uid_validity = int(time.time())
    body = message.build_inbox_body(
        user_login=user_login,
        name=name,
        pgp_pub_armored=pgp_pub_armored,
        uid_validity=uid_validity,
    )

    # Issue title becomes the meaningful part of GitHub's notification email
    # subject ("[owner/repo] <title> (Issue #N)"), so keep it tight.
    issue = client.create_issue(
        owner, repo_name,
        title=name,
        body=body,
    )
    issue_number = int(issue["number"])
    uid_validity = uid_validity ^ issue_number

    row = InboxRow(
        name=name,
        kind="issue",
        repo=repo,
        issue_number=issue_number,
        gist_id=None,
        user_login=user_login,
        created_at=_now_iso(),
        uid_validity=uid_validity,
        pgp_enabled=pgp_enabled,
        imap_password=imap_password,
        pop_mode=pop_mode,
    )
    with state.connect() as conn:
        state.insert_inbox(conn, row)

    # Persist a human-readable copy of the inbox config alongside the keys
    (inbox_dir / "inbox.json").write_text(
        json.dumps(
            {
                **{k: v for k, v in asdict(row).items() if k != "imap_password"},
                "pgp_fingerprint": pgp_fp or None,
                "user_pgp_fingerprint": user_fp or None,
            },
            indent=2,
        )
    )

    return row


def list_inboxes() -> list[InboxRow]:
    with state.connect() as conn:
        return state.list_inboxes(conn)


def load_inbox(name: str) -> InboxRow:
    with state.connect() as conn:
        row = state.get_inbox(conn, name)
    if row is None:
        raise InboxError(f"no inbox named {name!r}")
    return row


def delete_inbox(
    name: str,
    *,
    local_only: bool = False,
    client: GitHubClient | None = None,
) -> dict:
    """Fully delete an inbox: GitHub side + local state.

    Default behaviour wipes every comment on the inbox issue then tries the
    GraphQL deleteIssue mutation. If that mutation is denied (no repo-admin
    perm), title/body are cleared and the issue closed. Local rows + the
    per-inbox state dir are always removed (cascades to messages/drafts/etags
    via FK ON DELETE).

    Pass `local_only=True` to keep the GitHub issue intact and only drop the
    local mirror — useful when you want to start fresh against the same issue.
    """
    row = load_inbox(name)
    actions: dict = {"local": False, "remote": None}

    if not local_only:
        own = client is None
        c = client or GitHubClient(auth.resolve_token())
        try:
            actions["remote"] = _wipe_issue(c, row)
        finally:
            if own:
                c.close()

    with state.connect() as conn:
        state.delete_inbox(conn, name)
    inbox_dir = state.state_dir() / "inboxes" / name
    if inbox_dir.exists():
        shutil.rmtree(inbox_dir, ignore_errors=True)
    actions["local"] = True
    return actions


def default_inbox_name() -> str | None:
    cfg = state.config_dir() / "config.toml"
    if not cfg.exists():
        return None
    # Minimal TOML: just look for `default_inbox = "name"`
    for line in cfg.read_text().splitlines():
        line = line.strip()
        if line.startswith("default_inbox") and "=" in line:
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def url_for(inbox: InboxRow) -> str:
    if inbox.repo and inbox.issue_number is not None:
        return f"https://github.com/{inbox.repo}/issues/{inbox.issue_number}"
    return ""


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _wipe_issue(c: GitHubClient, row: InboxRow) -> str:
    """Hard-delete an inbox issue: drop comments, then try GraphQL delete.

    GitHub's REST API can't delete issues; the GraphQL `deleteIssue` mutation
    can, but requires repo-admin permission. If GraphQL refuses, we wipe the
    title/body and close — the next-best minimisation.
    """
    assert row.repo and row.issue_number is not None
    owner, repo = row.repo.split("/", 1)

    comments_dropped = 0
    try:
        comments = c.list_all_issue_comments(owner, repo, row.issue_number)
    except GitHubError as e:
        if e.status == 404:
            return f"issue {row.repo}#{row.issue_number} already gone"
        raise

    for comment in comments:
        try:
            c.delete_issue_comment(owner, repo, int(comment["id"]))
            comments_dropped += 1
        except GitHubError as e:
            if e.status == 404:
                continue
            raise

    try:
        issue = c.get_issue(owner, repo, row.issue_number)
    except GitHubError as e:
        if e.status == 404:
            return (
                f"deleted {comments_dropped} comments; "
                f"issue {row.repo}#{row.issue_number} already gone"
            )
        raise

    node_id = issue.get("node_id")
    if node_id:
        try:
            c.delete_issue_via_graphql(node_id)
            return (
                f"deleted issue {row.repo}#{row.issue_number} "
                f"(+ {comments_dropped} comments)"
            )
        except GitHubError:
            pass

    c.update_issue(
        owner,
        repo,
        row.issue_number,
        title="(deleted)",
        body="",
        state="closed",
    )
    return (
        f"wiped+closed issue {row.repo}#{row.issue_number} "
        f"(+ {comments_dropped} comments; GraphQL delete unavailable)"
    )
