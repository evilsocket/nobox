---
name: nobox
description: Async email-style messaging with the human user via a GitHub issue used as a mailbox. Reach for the nobox tools whenever the user wants to be reached asynchronously (they're stepping away, the task will take a while, they ask to "leave a note", "ping me later", "check messages", or "email me when you're done"). Provides read/send/reply tools that talk to a GitHub-backed inbox; the human reads and replies through their normal email client. Optional transparent PGP.
---

# nobox

`nobox` is an async messaging channel between you (an AI agent) and the human
user. The transport is a GitHub issue — you read and post via the GitHub API
through the `nobox` MCP server; the human reads notification emails and
replies from their normal mail client. You never speak to each other in real
time; treat this like email.

## When to reach for it

- The user says "leave me a note", "drop me a message", "ping me when you're
  done", "check my inbox", "send me an email about it".
- You finish long-running work while the user is away and want to notify them.
- The user wants a durable, threaded record they can read at leisure.

Do **not** reach for it in normal interactive turns — those use the regular
chat channel. nobox is specifically for *async*, *durable*, *email-style*
delivery.

## Tools (exposed by the `nobox` MCP server)

| Tool | Purpose | When |
|------|---------|------|
| `list_inboxes` | enumerate configured inboxes | start of session, to learn what's available |
| `read_inbox(name)` | list previews of **incoming** messages (from the human) | first call, to scan |
| `read_message(name, comment_id)` | full body of one message; marks it \Seen | only for messages you intend to act on |
| `send_message(name, body)` | post a new message; user gets the email | normal outbound |
| `send_message(name, body, in_reply_to=<id>)` | thread a reply under a specific incoming message | keep conversation context |
| `mark_read` / `mark_unread` | flag management; doesn't touch GitHub | rare, mostly for housekeeping |
| `create_inbox` | bootstrap a new inbox | **never autonomously** — ask the user first |
| `delete_inbox` | wipe an inbox | **never autonomously** — ask the user first |
| `doctor` | round-trip self-test (posts a sentinel) | only when the user reports the loop is broken |

## Mental model

- **Direction.** `read_inbox` returns messages with `direction: "in"` — those
  are messages **from the user to you**. Your own posts have `direction:
  "out"` and don't appear in `read_inbox` output (they're in the local
  archive but not the unread queue).
- **POP3-style retention** (default). Once a message is persisted locally,
  it's deleted from the GitHub issue. The local SQLite store is the full,
  permanent archive; the GitHub issue is just a transient transport. So the
  issue page may look empty even though there's full history available.
- **Latency.** The human replies via email. Expect minutes-to-hours between
  your `send_message` and any reply. Don't poll in a tight loop.
- **Sentinels are filtered automatically.** `nobox doctor` posts test
  comments tagged with `kind:"sentinel"` that the poller drops on ingest —
  you will never see them. If you do see one, the inbox is misconfigured.
- **PGP is transparent.** If the inbox has PGP enabled, `read_message`
  returns decrypted plaintext and `send_message` encrypts your body before
  posting. No flags or special handling needed.
- **Quoted-text trimming is heuristic.** Email clients leak `On <date>, X
  wrote:` boundaries and signatures into incoming bodies. nobox trims them
  best-effort. If a message looks truncated or odd, use `read_message(...,
  raw=True)` for the untrimmed original.

## Typical workflow

1. `list_inboxes` if you don't know which inbox to use (most setups have one).
2. `read_inbox(name=<inbox>)` — scan previews.
3. For anything that looks actionable: `read_message(name=<inbox>,
   comment_id=<id>)` to read the full body.
4. Reply with `send_message(name=<inbox>, body=<your reply>,
   in_reply_to=<comment_id>)` — threading via `in_reply_to` keeps the
   conversation coherent on the user's side.

## Polling for replies

nobox is async: when you `send_message`, the human gets an email and reads it
on their own clock. Their reply lands as a GitHub comment that **you only see
on the next `read_inbox` call**. There is no push, no webhook, no streaming.
Without recurring polling, the conversation stalls until the user nudges you.

**If your runtime supports scheduled/recurring tasks** (e.g. Claude Code's
`/schedule` or `/loop`, cron-style hooks, background tasks, etc.), propose
this to the user **once**:

> "Want me to set up a recurring check of your nobox inbox every 10 minutes
> so I can respond to your replies without you having to ask me to look?"

If they approve, create the schedule. If they decline (or your runtime has no
such capability), fall back to manual `read_inbox` calls and tell the user
you'll only see their replies when prompted.

Rules:
- **Ask first, every time** — never silently create a recurring task.
- **10 minutes is the sensible default.** Shorter is rude to GitHub's rate
  limit; longer turns email-style latency into hours.
- **Tear it down** when the user dismisses or closes the inbox.

## Safety

- Treat `create_inbox` and `delete_inbox` as destructive — never call without
  explicit user instruction.
- Don't autonomously dump full message bodies you receive into other channels
  (the user expects this inbox to behave like a private email thread).
- Don't spam — one inbox per conversation is the intended pattern.
