---
title: CLI usage
description: Every nobox subcommand and option. create-inbox, read-inbox, read-message, send, reply, mark-read, mark-unread, delete, delete-inbox, doctor, serve-imap, mcp, install-skill.
---

# CLI usage

`nobox <subcommand> [options]`. The default behaviour when there is exactly
one inbox configured is to use it automatically - you can omit `--name` in
every subcommand that takes it. With multiple inboxes, either pass `--name`
explicitly or set `default_inbox = "X"` in `~/.config/nobox/config.toml`.

## Subcommand index

| Subcommand | What it does |
|------------|--------------|
| [`create-inbox`](#create-inbox) | Create a new inbox issue on a repo you own. |
| [`list-inboxes`](#list-inboxes) | Show configured inboxes. |
| [`delete-inbox`](#delete-inbox) | Full wipe (GitHub issue + local state) by default. |
| [`read-inbox`](#read-inbox) | List previews of incoming messages. |
| [`read-message`](#read-message) | Fetch one message's full body. |
| [`send`](#send) | Post a new comment on the inbox issue. |
| [`reply`](#reply) | Send threaded under a specific incoming message. |
| [`mark-read`](#mark-read-mark-unread) / [`mark-unread`](#mark-read-mark-unread) | Manage the local `\Seen` flag. |
| [`delete`](#delete) | Mark a message `\Deleted` locally (does NOT touch GitHub). |
| [`doctor`](#doctor) | End-to-end sanity check + sentinel comment. |
| [`serve-imap`](#serve-imap) | Run the loopback IMAP4 bridge. |
| [`mcp`](#mcp) | Run the FastMCP stdio server. |
| [`install-skill`](#install-skill) | Drop the bundled SKILL.md into Claude Code. |

---

## `create-inbox`

Create a new inbox backed by a GitHub issue on a repo you own.

```bash
nobox create-inbox \
 --repo OWNER/REPO \
 --name <local-name> \
 [--pgp --user-pgp-key PATH] \
 [--no-pop]
```

| Option | Default | Meaning |
|--------|---------|---------|
| `--repo` | required | `OWNER/REPO` of a repo you own with issues enabled. |
| `--name` | required | Local identifier. Becomes the GitHub issue title (so notification emails read `[OWNER/REPO] <name> (Issue #N)`). |
| `--pgp` | off | Generate an inbox PGP keypair and encrypt traffic. Requires `--user-pgp-key`. |
| `--user-pgp-key PATH` | - | Path to your armored public key file. |
| `--no-pop` | pop on | Disable POP3-style behaviour: comments stay on GitHub after they're persisted locally. |

The issue body is seeded with the nobox logo + a short intro telling the
user how to talk to the agent (reply to email notifications). A
`<!-- nobox-inbox v=1 ... -->` marker is appended so the issue can be
recognised later.

## `list-inboxes`

```bash
nobox list-inboxes [--json]
```

Prints one row per inbox (`name`, `kind=issue`, `repo`, `pgp=...`).

## `delete-inbox`

```bash
nobox delete-inbox --name <name> [--local-only] [--force]
```

**Default behaviour is a full wipe**: every comment on the inbox issue is
dropped, then GraphQL `deleteIssue` is attempted. If GraphQL refuses (no
repo-admin perms), `title`/`body` are cleared and the issue closed. Local
SQLite rows + the per-inbox state dir are always removed.

| Option | Default | Meaning |
|--------|---------|---------|
| `--local-only` | off | Keep the GitHub issue intact; only drop the local mirror. |
| `--force`, `-f` | off | Skip the confirmation prompt. |

## `read-inbox`

```bash
nobox read-inbox [--name X] [--unread-only] [--limit N] [--since ISO] [--json]
```

Returns **previews** (500 chars) of incoming messages (`direction="in"`).
Outgoing messages your agent has posted are NOT surfaced here; they live in
the local archive.

| Option | Default | Meaning |
|--------|---------|---------|
| `--unread-only` | off | Skip messages already marked `\Seen`. |
| `--limit N` | 20 | Cap the number of rows returned. |
| `--since ISO` | - | Only messages newer than this ISO-8601 timestamp. |
| `--json` | off | Emit a JSON array (banner suppressed). |

Doctor-sentinel comments are filtered automatically.

## `read-message`

```bash
nobox read-message --name X --id COMMENT_ID [--raw] [--json]
```

Returns the full body. **Marks the message `\Seen`** as a side effect.
Pass `--raw` to get the untrimmed/un-decrypted original (useful when the
quoted-text trim misfires on non-English email clients).

## `send`

```bash
nobox send --name X --body "..." [--in-reply-to COMMENT_ID] [--json]
```

Posts a new comment on the inbox issue. The user gets a GitHub
notification email. `--in-reply-to` threads the message under a specific
incoming message - the human's email client renders the thread coherently.

If the inbox has PGP enabled, the body is encrypted to the user's public
key transparently.

## `reply`

```bash
nobox reply --name X --id COMMENT_ID --body "..." [--json]
```

Shorthand for `send --in-reply-to COMMENT_ID`.

## `mark-read` / `mark-unread` { #mark-read-mark-unread }

```bash
nobox mark-read --name X --id COMMENT_ID
nobox mark-unread --name X --id COMMENT_ID
```

Manages the local `\Seen` flag. Doesn't touch GitHub. Rare in normal flow - 
`read-message` already marks `\Seen`.

## `delete`

```bash
nobox delete --name X --id COMMENT_ID
```

Marks a message `\Deleted` **locally only**. Does NOT delete the GitHub
comment (use `delete-inbox --remote` if you want full remote cleanup).

## `doctor`

```bash
nobox doctor [--name X] [--json]
```

Runs preflight checks (auth, rate limit, `has_issues`, issue state) and - 
if `--name` is given - posts a `**nobox doctor sentinel**` comment so you
can confirm the email loop works end-to-end. The sentinel is tagged with a
`kind:"sentinel"` meta block so the poller drops it on ingest, never to be
seen by your agent.

## `serve-imap`

```bash
nobox serve-imap [--host 127.0.0.1] [--port 1143]
```

Starts a loopback-only IMAP4rev1 server. Requires the `[imap]` extra. See
the [Local IMAP](imap.md) page for the protocol scope and Thunderbird/mutt
setup.

## `mcp`

```bash
nobox mcp
```

Runs the FastMCP stdio server. Wire into Claude Code with:

```bash
claude mcp add --transport stdio nobox -- nobox mcp
```

See the [MCP](mcp.md) page for the full tool catalogue.

## `install-skill`

```bash
nobox install-skill [--project DIR]
```

Copies the bundled `SKILL.md` into `~/.claude/skills/nobox/SKILL.md` by
default, or into `<DIR>/.claude/skills/nobox/` with `--project` for
project-scoped installs. See the [Claude Code Skill](skill.md) page.

---

## State paths

XDG-compliant (set via `platformdirs`):

| Purpose | Path |
|---------|------|
| Config | `$XDG_CONFIG_HOME/nobox/config.toml` |
| SQLite archive | `$XDG_DATA_HOME/nobox/inboxes.db` |
| Per-inbox keys + IMAP password | `$XDG_STATE_HOME/nobox/inboxes/<name>/` |
| ETag cache | `$XDG_CACHE_HOME/nobox/` |

The per-inbox directory contains the inbox config snapshot (`inbox.json`),
the PGP keypair if enabled (`pgp.priv.asc`, `pgp.pub.asc`, `user.pub.asc`),
and the IMAP password. Private files are mode `0600`.
