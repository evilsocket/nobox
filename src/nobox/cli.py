"""Click-based CLI dispatch."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import click

from nobox import __version__, service, state
from nobox import inbox as inbox_mod
from nobox._logo import banner


def _should_print_banner() -> bool:
    """Show the banner only when it adds value:
      - stdout must be a TTY (avoid polluting JSON / IMAP / MCP / piped output)
      - any explicit machine-readable flag in argv suppresses it
      - the `mcp` subcommand never gets a banner (stdio MCP protocol)
    """
    if not sys.stdout.isatty():
        return False
    if "--json" in sys.argv:
        return False
    return not (len(sys.argv) > 1 and sys.argv[1] == "mcp")


def _print_banner() -> None:
    if _should_print_banner():
        click.echo(banner(__version__, colored=True))


def _resolve_inbox(name: str | None):
    if name:
        return inbox_mod.load_inbox(name)

    name = inbox_mod.default_inbox_name()
    if name:
        return inbox_mod.load_inbox(name)

    # Fall back: if there's exactly one inbox, just use it.
    all_inboxes = inbox_mod.list_inboxes()
    if len(all_inboxes) == 1:
        return all_inboxes[0]
    if not all_inboxes:
        raise click.ClickException(
            "no inboxes exist yet. Run `nobox create-inbox …` first."
        )
    names = ", ".join(i.name for i in all_inboxes)
    raise click.ClickException(
        f"multiple inboxes ({names}). Pass --name to pick one, "
        f"or set `default_inbox = \"<name>\"` in "
        f"~/.config/nobox/config.toml."
    )


def _dump(value, as_json: bool) -> None:
    if as_json:
        click.echo(json.dumps(value, indent=2, default=str))
        return
    if isinstance(value, list):
        for item in value:
            click.echo(_fmt(item))
    else:
        click.echo(_fmt(value))


def _fmt(obj) -> str:
    if isinstance(obj, dict):
        if "comment_id" in obj:
            preview = obj.get("preview") or obj.get("body") or ""
            preview = preview.replace("\n", " ⏎ ")[:120]
            flags = ",".join(obj.get("flags") or []) or "-"
            return f"[{obj['comment_id']}] {obj.get('ts','')} {obj.get('from','?'):<20} ({flags}) {preview}"
        if "name" in obj and "kind" in obj:
            return (
                f"{obj['name']:<20} kind={obj['kind']:<6} "
                f"{obj.get('repo') or obj.get('gist_id') or '-'} "
                f"pgp={obj.get('pgp_enabled')}"
            )
    return json.dumps(obj, default=str)


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__)
def main() -> None:
    """nobox — GitHub-as-email inbox for AI ↔ human async messaging."""
    _print_banner()


@main.command("create-inbox")
@click.option("--repo", required=True, help="OWNER/REPO of the GitHub repo to host the inbox issue")
@click.option("--name", required=True, help="Local name for this inbox")
@click.option("--pgp", "use_pgp", is_flag=True, help="Generate inbox PGP keypair + encrypt traffic")
@click.option("--user-pgp-key", type=click.Path(exists=True, path_type=Path), help="User's public key path")
@click.option(
    "--no-pop",
    "no_pop",
    is_flag=True,
    help="Keep comments on GitHub after fetching. Default is POP3-style: "
    "delete each comment from GitHub once it's persisted locally.",
)
def cmd_create_inbox(repo, name, use_pgp, user_pgp_key, no_pop):
    """Create a new inbox backed by a GitHub issue on the given repo."""
    from nobox.auth import resolve_token
    from nobox.github_client import GitHubClient

    with GitHubClient(resolve_token()) as c:
        row = inbox_mod.create_inbox(
            c,
            name=name,
            repo=repo,
            pgp_enabled=use_pgp,
            user_pgp_key_path=user_pgp_key,
            pop_mode=not no_pop,
        )
    url = inbox_mod.url_for(row)
    click.echo(f"✓ inbox {name!r} created → {url}")
    click.echo(
        f"  pop_mode: {'on (delete from GitHub after read)' if row.pop_mode else 'off (retain on GitHub)'}"
    )
    click.echo("")
    click.echo(click.style("⚠  CRITICAL SETUP STEP", fg="yellow", bold=True))
    click.echo(
        "GitHub silently suppresses email notifications for your own actions"
    )
    click.echo("by default. Since nobox uses your token, this breaks the loop.")
    click.echo("")
    click.echo(
        "Fix once at https://github.com/settings/notifications →"
    )
    click.echo(
        "  Email notification preferences → enable 'Include your own updates'"
    )
    click.echo("")
    click.echo(f"Then run `nobox doctor --name {name}` to verify the round-trip.")


@main.command("list-inboxes")
@click.option("--json", "as_json", is_flag=True)
def cmd_list_inboxes(as_json):
    """List configured inboxes."""
    rows = inbox_mod.list_inboxes()
    items = [state.inbox_to_dict(r) for r in rows]
    if not items:
        click.echo("no inboxes")
        return
    _dump(items, as_json)


@main.command("delete-inbox")
@click.option("--name", required=True, help="Inbox name to delete")
@click.option(
    "--local-only",
    is_flag=True,
    help="Keep the GitHub issue intact; only drop the local mirror.",
)
@click.option("--force", "-f", is_flag=True, help="Skip confirmation prompt.")
def cmd_delete_inbox(name, local_only, force):
    """Delete an inbox completely (GitHub issue + local state).

    By default this wipes every comment on the inbox issue, then deletes the
    issue (or wipes title/body + closes it if you lack admin rights), and
    clears the local SQLite rows + per-inbox key directory. Pass --local-only
    to keep the GitHub issue intact.
    """
    row = inbox_mod.load_inbox(name)
    url = inbox_mod.url_for(row)
    if not force:
        what = "local only (GitHub issue kept)" if local_only else "GitHub issue + local state"
        click.echo(f"About to delete inbox {name!r} — {what}")
        click.echo(f"  url: {url or '(no remote)'}")
        click.confirm("Proceed?", abort=True)
    actions = inbox_mod.delete_inbox(name, local_only=local_only)
    click.echo("✓ local: removed")
    if actions["remote"] is not None:
        click.echo(f"✓ remote: {actions['remote']}")


@main.command("read-inbox")
@click.option("--name", help="Inbox name (defaults to config.toml default_inbox)")
@click.option("--unread-only", is_flag=True)
@click.option("--limit", type=int, default=20)
@click.option("--since", help="ISO timestamp; only return messages newer than this")
@click.option("--json", "as_json", is_flag=True)
def cmd_read_inbox(name, unread_only, limit, since, as_json):
    """List previews of incoming messages."""
    row = _resolve_inbox(name)
    msgs = service.read_inbox(row, unread_only=unread_only, limit=limit, since=since)
    _dump(msgs, as_json)


@main.command("read-message")
@click.option("--name", help="Inbox name")
@click.option("--id", "comment_id", type=int, required=True)
@click.option("--raw", is_flag=True, help="Return the untrimmed/un-decrypted raw body")
@click.option("--json", "as_json", is_flag=True)
def cmd_read_message(name, comment_id, raw, as_json):
    """Read a single message in full (marks it \\Seen)."""
    row = _resolve_inbox(name)
    msg = service.read_message(row, comment_id, raw=raw)
    _dump(msg, as_json)


@main.command("send")
@click.option("--name", help="Inbox name")
@click.option("--body", required=True, help="Message body")
@click.option("--in-reply-to", type=int, default=None)
@click.option("--json", "as_json", is_flag=True)
def cmd_send(name, body, in_reply_to, as_json):
    """Send a new message (posts a comment, user gets the notification email)."""
    row = _resolve_inbox(name)
    out = service.send_message(row, body, in_reply_to=in_reply_to)
    _dump(out, as_json)


@main.command("reply")
@click.option("--name", help="Inbox name")
@click.option("--id", "comment_id", type=int, required=True)
@click.option("--body", required=True)
@click.option("--json", "as_json", is_flag=True)
def cmd_reply(name, comment_id, body, as_json):
    """Reply to a specific message id."""
    row = _resolve_inbox(name)
    out = service.send_message(row, body, in_reply_to=comment_id)
    _dump(out, as_json)


@main.command("mark-read")
@click.option("--name", help="Inbox name")
@click.option("--id", "comment_id", type=int, required=True)
def cmd_mark_read(name, comment_id):
    row = _resolve_inbox(name)
    service.mark_flag(row, comment_id, "\\Seen", add=True)
    click.echo(f"✓ {comment_id} \\Seen")


@main.command("mark-unread")
@click.option("--name", help="Inbox name")
@click.option("--id", "comment_id", type=int, required=True)
def cmd_mark_unread(name, comment_id):
    row = _resolve_inbox(name)
    service.mark_flag(row, comment_id, "\\Seen", add=False)
    click.echo(f"✓ {comment_id} unread")


@main.command("delete")
@click.option("--name", help="Inbox name")
@click.option("--id", "comment_id", type=int, required=True)
def cmd_delete(name, comment_id):
    """Mark a message \\Deleted locally (does NOT delete the GitHub comment)."""
    row = _resolve_inbox(name)
    service.mark_flag(row, comment_id, "\\Deleted", add=True)
    click.echo(f"✓ {comment_id} \\Deleted (local only)")


@main.command("doctor")
@click.option("--name", help="If given, also runs the sentinel-comment round-trip")
@click.option("--json", "as_json", is_flag=True)
def cmd_doctor(name, as_json):
    """Preflight checks + optional sentinel round-trip."""
    from nobox import doctor

    result = doctor.run(name=name)
    if as_json:
        click.echo(json.dumps(result, indent=2, default=str))
    else:
        _render_doctor(result)
    if not result.get("ok", False):
        sys.exit(1)


def _render_doctor(result: dict) -> None:
    for chk in result.get("checks", []):
        mark = "✓" if chk["ok"] else "✗"
        colour = "green" if chk["ok"] else "red"
        line = f"{click.style(mark, fg=colour)} {chk['name']}"
        detail = chk.get("info") if chk["ok"] else chk.get("error")
        if detail is not None:
            line += f" — {detail}"
        click.echo(line)
    if result.get("self_action"):
        click.echo("")
        click.echo(click.style("⚠  Self-action detected", fg="yellow", bold=True))
        click.echo(result["self_action_warning"])
    if "sentinel_hint" in result:
        click.echo("")
        click.echo(click.style("Sentinel posted — what to do next:", bold=True))
        click.echo(result["sentinel_hint"])


@main.command("serve-imap")
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", type=int, default=1143, show_default=True)
def cmd_serve_imap(host, port):
    """Run a local IMAP server. Bind to 127.0.0.1 only."""
    try:
        from nobox.imap_server import serve
    except ImportError as e:
        raise click.ClickException(
            "IMAP support requires the [imap] extra: pip install 'nobox[imap]'"
        ) from e
    if host not in ("127.0.0.1", "::1", "localhost"):
        raise click.ClickException(
            f"refusing to bind to {host!r}; IMAP server is loopback-only"
        )
    serve(host=host, port=port)


@main.command("mcp")
def cmd_mcp():
    """Run the MCP server over stdio (for AI clients like Claude Code)."""
    from nobox.mcp_server import run_stdio

    run_stdio()


@main.command("install-skill")
@click.option(
    "--project",
    "project_dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Install the skill under <DIR>/.claude/skills/nobox/ (project-scope). "
    "Default: ~/.claude/skills/nobox/",
)
def cmd_install_skill(project_dir):
    """Copy the bundled SKILL.md into Claude Code's skills dir."""
    src = Path(__file__).parent / "skill" / "SKILL.md"
    if not src.exists():
        raise click.ClickException(f"bundled skill not found at {src}")
    if project_dir:
        dst_dir = project_dir / ".claude" / "skills" / "nobox"
    else:
        dst_dir = Path.home() / ".claude" / "skills" / "nobox"
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / "SKILL.md"
    shutil.copyfile(src, dst)
    click.echo(f"✓ installed → {dst}")


if __name__ == "__main__":
    main()
