---
title: Local IMAP server
description: nobox serve-imap runs a loopback IMAP4rev1 bridge on 127.0.0.1:1143 that translates IMAP operations into GitHub REST calls. Read, reply, and idle from Thunderbird or mutt.
---

# Local IMAP server

`nobox serve-imap` runs an IMAP4rev1 server on the loopback interface that
translates IMAP operations into GitHub REST calls. Point any mail client at
it (Thunderbird, Apple Mail, mutt, neomutt, ...) and use a nobox inbox as a
regular mailbox - including **composing replies** that get posted to GitHub
as real comments.

Requires the `[imap]` install extra (Twisted).

## Run it

```bash
nobox serve-imap [--host 127.0.0.1] [--port 1143]
```

The server **refuses to bind to anything other than the loopback
interface** by design - there's no TLS, and the IMAP password is
generated per inbox at create time. Loopback-only keeps that acceptable.

## Mailbox structure

One IMAP user per nobox inbox. Username = inbox name; password =
`$XDG_STATE_HOME/nobox/inboxes/<name>/imap.password`.

| Folder | Source | Mutability |
|--------|--------|------------|
| `INBOX` | direction=`in` messages, not `\Deleted` | STORE flags |
| `Sent` | direction=`out` messages | **APPEND posts a new comment**, STORE flags |
| `Drafts` | rows from the local `draft` table | APPEND / STORE / EXPUNGE (local-only) |
| `Trash` | messages with the local `\Deleted` flag | EXPUNGE clears flag |

UIDs are GitHub comment IDs; UIDVALIDITY is
`inbox.uid_validity = created_at_unix XOR issue_id` and stays stable as
long as the backing issue exists.

## Protocol scope

| Command | Status |
|---------|--------|
| `CAPABILITY` | ✓ |
| `LOGIN` (PLAIN, loopback only) | ✓ |
| `LIST` / `LSUB` | ✓ |
| `SELECT` / `EXAMINE` | ✓ |
| `STATUS` | ✓ |
| `UID FETCH` (BODY[], BODYSTRUCTURE, ENVELOPE, FLAGS, INTERNALDATE) | ✓ |
| `UID SEARCH` | ✓ |
| `UID STORE` (flag changes notify IDLE clients) | ✓ |
| `APPEND` to `Drafts` (local-only stash) | ✓ |
| `APPEND` to `Sent` (posts a real GitHub comment) | ✓ |
| `IDLE` (push notifications via background polling) | ✓ |
| `NOOP` / `CHECK` / `CLOSE` / `LOGOUT` / `EXPUNGE` | ✓ |
| `COPY` / `MOVE` | ✗ (use `STORE +FLAGS \Deleted` + `EXPUNGE` to delete) |

### APPEND to Sent - composing replies from your mail client

When you reply to or compose a message in a mail client that's set up to
"save sent messages on the server" (the default for Thunderbird with an
IMAP+SMTP account), the client appends the outgoing RFC822 message to the
`Sent` folder via IMAP. nobox intercepts that APPEND and treats it as
"send this":

1. The RFC822 bytes are parsed; the text/plain body is extracted (or
  text/html stripped if there's no plaintext alternative).
2. The `In-Reply-To` / `References` header is matched against nobox's
  synthesised `<comment-{id}@nobox.local>` Message-ID format; if a hit,
  the post is threaded under that comment.
3. The body is posted via the GitHub REST API exactly as if you'd run
  `nobox send`.

Result: replies in Thunderbird Just Work. PGP encryption (if the inbox
has it enabled) is applied transparently before posting.

> **Caveat:** mail clients that require SMTP to "send" before they
> APPEND to Sent won't work without an SMTP shim. Configure your client
> to use "IMAP for outgoing mail" mode, or set up a stub SMTP server
> that does nothing (some clients accept this).

### IDLE - push notifications

When a connected client issues `IDLE`, nobox attaches a background
`LoopingCall` that polls GitHub every 60s. When new comments land in the
backing issue:

1. The poller persists them to SQLite (same path as `nobox read-inbox`).
2. If the message count grew, `IMAP4Server.newMessages(exists, recent)`
  fires, which sends an untagged `EXISTS` response to the client.
3. The client wakes up, can `DONE` the IDLE, fetch the new mail, and
  re-enter IDLE.

`STORE` operations also notify all attached clients via `flagsChanged`,
so two clients connected to the same inbox stay in sync on read/unread
state.

## Envelope synthesis

GitHub comments aren't RFC822 messages, so nobox synthesises the
envelope when serving them:

| Header | Value |
|--------|-------|
| `Message-ID` | `<comment-{id}@nobox.local>` |
| `In-Reply-To` / `References` | Chained via the local `in_reply_to_id` graph |
| `Date` | GitHub `created_at` (RFC 2822-formatted) |
| `From` | `"<author-login> via GitHub" <noreply@github.com>` |
| `To` | `"<your-login>" <<your-login>@nobox.local>` |
| `Subject` | The `subject` field from nobox metadata, or `Re: <inbox-name>` |
| Body | Trimmed text/plain; PGP-decrypted if applicable |

The synthesised `Message-ID` round-trips through `In-Reply-To` on the
client side, which is how APPEND-to-Sent reconstructs the `in_reply_to`
threading on the GitHub side.

## Thunderbird setup

1. Add a new mail account, e.g. `daily@nobox.local`.
2. Server type: **IMAP**, port `1143`, hostname `localhost`.
3. Connection security: **None** (loopback).
4. Authentication: **Normal password**.
5. Username: your inbox name (e.g. `daily`).
6. Password: `cat ~/.local/state/nobox/inboxes/daily/imap.password`.
7. For Sent-from-IMAP behaviour, configure outgoing mail to use the same
  account and check **"Save sent messages on the server"** under the
  account settings. SMTP can be set to anything (or a dummy server);
  nobox handles the actual posting via the IMAP APPEND.

Thunderbird may complain about the unencrypted connection - confirm
that's fine since the traffic never leaves your machine.

## mutt setup

```muttrc
set folder = "imap://daily@localhost:1143"
set imap_pass = "`cat ~/.local/state/nobox/inboxes/daily/imap.password`"
set ssl_starttls = no
set ssl_force_tls = no
mailboxes = "imap://daily@localhost:1143/INBOX"

# Compose replies that land via APPEND-to-Sent
set record = "imap://daily@localhost:1143/Sent"
```

## Sync triggers

- `SELECT` / `STATUS` / `FETCH` triggers a one-shot GitHub poll before
  serving so the client sees fresh data on demand.
- `IDLE` triggers a 60s `LoopingCall` that polls in the background.
- `APPEND` to Sent triggers a single POST via the GitHub API.

## Offline mode

For offline use, set `NOBOX_OFFLINE=1` in the environment. The IMAP
server will serve straight from the local SQLite mirror, skipping
GitHub round trips on every FETCH. APPEND-to-Sent still tries to POST
(that's the whole point), so wire up GitHub auth if you want to
exercise that path; otherwise stick to APPEND-to-Drafts or `STORE` for
offline browsing.
