---
title: MCP server
description: nobox ships a FastMCP stdio server. Wire it into Claude Code, Claude Desktop, Cursor, or any MCP-compatible client to give an AI agent async email-style messaging over GitHub.
---

# MCP server

`nobox mcp` runs a [Model Context Protocol](https://modelcontextprotocol.io/)
server over stdio. The server has its own `instructions` block that's
shipped to the model when the tools are loaded, plus per-tool docstrings.

## Wire-up

### Claude Code

```bash
claude mcp add --transport stdio nobox -- nobox mcp
```

That's it - Claude Code will spawn `nobox mcp` as a subprocess when needed,
read the server-level instructions, and expose the tools to the model.

### Claude Desktop, Cursor, others

Add a stdio MCP entry pointing at the `nobox` binary with the `mcp`
argument. Example (Claude Desktop `mcpServers` block):

```json
{
 "nobox": {
 "command": "nobox",
 "args": ["mcp"]
 }
}
```

## Tools exposed

| Tool | Purpose |
|------|---------|
| `list_inboxes` | Enumerate configured inboxes. |
| `read_inbox(name, unread_only=true, limit=20)` | List **previews** (500 chars) of incoming messages. |
| `read_message(name, comment_id, raw=false)` | Full body of a single message. Marks it `\Seen`. |
| `send_message(name, body, in_reply_to=null)` | Post a new comment; user gets the notification email. |
| `mark_read(name, comment_id)` / `mark_unread(...)` | Toggle the local `\Seen` flag. |
| `create_inbox(name, repo, pgp=false, ...)` | ⚠ Destructive - never call without explicit user instruction. |
| `delete_inbox(name, local_only=false)` | ⚠ Destructive - full wipe (issue + local) by default. |
| `doctor(name=null)` | Diagnostic; posts a sentinel comment that the poller filters out. |

`read_inbox` returns previews only on purpose - calling `read_message` for
every item would dump full bodies into the agent's context every poll
cycle. The agent should `read_inbox` first, decide which messages matter,
then `read_message` only those.

## Server-level instructions

When the server starts it ships an `instructions` block describing what
nobox is, when to use the tools, and the operational semantics the agent
needs to know (POP3-style retention, that read_inbox only returns
incoming, that doctor sentinels are auto-filtered, the polling pattern,
PGP transparency, safety rules around create/delete). MCP clients
typically surface this to the model alongside the tool list.

## Polling pattern

nobox is **async with no push channel**. After `send_message`, the human
reads the notification email on their own clock and replies hours later.
You only see their reply on the next `read_inbox` call.

The MCP `instructions` block tells the agent to ask the user **once**:

> Want me to set up a recurring check of your nobox inbox every 10 minutes
> so I can respond to your replies without you having to nudge me?

...and to create the schedule only after explicit user approval. Recommended
interval is **10 minutes** - fast enough to feel responsive over email,
slow enough to stay well within GitHub's 5000 req/hour rate limit even
across many inboxes.

The exact mechanism depends on the runtime: Claude Code has `/schedule`
and `/loop`; other clients have cron-style hooks, background routines, or
nothing at all. If the runtime has no scheduling support, the agent falls
back to manual `read_inbox` calls.

## Output shape

All tools return JSON-serialisable dicts. `read_inbox` returns a list of:

```json
{
 "comment_id": 4508692935,
 "from": "evilsocket",
 "ts": "2026-05-21T13:24:18Z",
 "subject": "Re: eddy",
 "flags": [],
 "direction": "in",
 "preview": "hello, can you read me?..."
}
```

`read_message` returns the same plus a full `body` field (post-trim and
post-decrypt). Pass `raw=true` to get the untrimmed/un-decrypted original.

## Rate-limit + caching

The underlying client uses ETag-conditional GETs (`If-None-Match`) for
every list operation. A 304 response costs zero quota, so a 10-minute poll
on an idle inbox is essentially free. The client also tracks
`X-RateLimit-Remaining`/`Reset` and backs off with jitter if you ever hit
the wall.
