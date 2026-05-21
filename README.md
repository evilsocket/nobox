# nobox

A GitHub-as-email inbox for AI ↔ human async messaging.

nobox lets you use a single GitHub repository to host multiple inboxes —
each one a GitHub issue — as free, durable, optionally PGP-encrypted
mailboxes. The human replies to GitHub's notification emails through their
normal email client; an AI agent reads and posts via the GitHub REST API.
Same conversation, two protocols.

## Quick start

```bash
# Install straight from the repo (Python 3.11+)
uv tool install 'git+https://github.com/evilsocket/nobox'
# with IMAP + PGP extras:
uv tool install 'nobox[all] @ git+https://github.com/evilsocket/nobox'
# pipx works too:
pipx install 'nobox[all] @ git+https://github.com/evilsocket/nobox'

# Auth (only needed once)
gh auth login --scopes 'repo'

# Create an inbox in a repo you own
nobox create-inbox --repo evilsocket/notes --name daily

# Verify end-to-end (posts a sentinel comment, asks you to confirm the email)
nobox doctor --name daily

# Drop the SKILL.md into ~/.claude/skills/nobox/ so Claude Code knows when
# to reach for the MCP tools and how to behave around them.
nobox install-skill

# Then, in your agent of choice, just say:
#
#   "Check your nobox every 10 minutes and respond to my messages."
#
# The skill explains the polling pattern and the agent will ask you to
# confirm setting up the recurring task before scheduling anything.
```

## How it works

A [friend](https://github.com/zangarmarsh) and I were talking about how to
give an AI agent a *public* email inbox without standing up an MTA,
registering a domain, or paying for one of the hosted services. The
sticking point was the MTA itself — there's no nice, free way to get an
internet-facing email address you can hand to an agent. Alessandro pointed
out the GitHub notification system: every comment notification arrives from
a `reply+<base32>@reply.github.com` address, and replies to that address
are posted back as issue comments. We spent the night reverse-engineering
what the base32 token carries and how GitHub validates it. **nobox is the
result.**

When somebody comments on a GitHub issue you're subscribed to (or
@mentioned in), GitHub emails you from `reply+<TOKEN>@reply.github.com`.
Replies to that address are posted back to the same thread as a comment.
nobox is just glue around that: the human uses their normal email client,
the agent uses the GitHub REST API, nobox stays on the agent side.

The token isn't opaque. It's a structured binary blob — base32-encoded
without padding, 26–30 bytes on the wire (the old hex variant was twice as
long, compressed to base32 around 2022):

```
uid(4) || mac(10) || thread_id(4) || msgpack([kind, subject_id])
```

- **`uid`** — recipient's GitHub user ID as a big-endian uint32, in the
  clear. The first 7 base32 chars of any token directly identify the
  recipient — no API call needed. Leaked notification emails are therefore
  trivially attributable to a specific GitHub user.
- **`mac`** — 80-bit authenticator that empirically depends on `uid`,
  `thread_id`, and almost certainly the `(kind, subject_id)` tuple too. No
  field can be mutated without invalidating it. The keying material rotates
  on password reset, which is why old reply tokens stop working once you
  change your password.
- **`thread_id`** — big-endian uint32, a global notification-delivery
  counter shared across all recipients of one notification event but unique
  per delivery. Currently in the 1.78B range, growing ~1B every six months,
  which means GitHub has to migrate the field before it hits 2³² in roughly
  two years.
- **`msgpack` tail** — a 2-element array `[kind, subject_id]`. `kind` is a
  single ASCII char (`'i'` issue and `'g'` gist confirmed in the wild;
  `'p'` / `'c'` / `'d'` / `'r'` inferred for PRs / commit comments /
  discussions / releases). `subject_id` is GitHub's global database ID for
  the subject (not the per-repo `#N`), encoded with msgpack's int-tag
  autosizing (`ce` + 4 bytes for uint32, `cf` + 8 bytes for uint64). The
  token has no repo field — GitHub recovers the repo server-side by joining
  on `subject_id`, and externally you can do the same via GraphQL
  `node(id: "MDU6SXNzdWU…")`.

A legacy hex-encoded variant predates the base32 cutover:
`uid(4) || mac(20) || msgpack([thread_id_u64, [kind, subject_id]])`. Same
fields, longer MAC, `thread_id` inside the msgpack as a uint64, hex on the
wire (60+ chars). GitHub's `metroplex` daemon still accepts both, so
archived notification emails from before ~2022 remain valid until the
recipient's next password reset.

At the SMTP layer there's effectively no validation. The six round-robin
MX hosts (`in-{5..10}.smtp.github.com`, on a shared cert with
`*.smtp.ghe.com` covering Enterprise Cloud) accept any syntactically valid
recipient, any sender, any token mutation, and even arbitrary local-part
prefixes (`notifications+`, `noreply+`, `postmaster`, etc.) with `250 OK`.
All real token validation happens post-DATA inside metroplex — so SMTP
probing yields no liveness oracle, and the MAC is the only thing stopping
forgery.

Open questions: whether metroplex still enforces the From-header →
verified-email check the 2011 docs described (current docs are silent), and
whether the kind-char dispatch has a format-confusion bug between the old
and new token layouts.

nobox itself doesn't decode any of this. It only needs the GitHub REST API
on the agent side and the user's regular reply-to-email behaviour on the
human side; the token research is just *why we know this works*.

> **⚠ Never share a `reply+<TOKEN>@reply.github.com` address with anyone.
> Each token is effectively a scoped bearer credential — it lets whoever
> holds it post a comment _as you_, on that specific issue, simply by
> SMTPing a reply. The MAC stops forgery, but if the token itself leaks
> (forwarded email, screenshot, paste into a chat), the holder can
> impersonate you on that thread. The only way to revoke an outstanding
> reply token is to change your GitHub password — that rotates the keying
> material and invalidates every still-live token at once.**

## Surfaces

- **CLI** — `nobox <subcommand>`. Same binary serves everything.
- **MCP server** — `nobox mcp` runs a FastMCP stdio server. Wire into Claude
  Code: `claude mcp add --transport stdio nobox -- nobox mcp`.
- **Claude Code Skill** — `nobox install-skill` drops a `SKILL.md` into
  `~/.claude/skills/nobox/` so Claude knows when to use the MCP tools.
- **Local IMAP server** — `nobox serve-imap` exposes inboxes on
  `127.0.0.1:1143` so Thunderbird/mutt can browse them. Read-mostly in v1.

## PGP (opt-in)

```bash
nobox create-inbox --repo me/notes --name secure \
  --pgp --user-pgp-key ~/.gnupg/me.pub.asc
```

nobox generates the inbox's keypair, embeds the public half in the issue body,
and encrypts every outgoing comment with your public key. Incoming PGP-armored
replies are auto-decrypted. Your private key never touches the GitHub side.

## Critical one-time setup

GitHub suppresses email notifications for **your own actions** by default.
Since nobox uses your token, every comment it posts is a "self action," and
GitHub won't email you. **The whole loop silently breaks without this fix.**

1. Visit https://github.com/settings/notifications
2. Under **Email notification preferences**, enable **"Include your own updates"**
3. Run `nobox doctor --name <your-inbox>` — confirm the sentinel email arrives

If you'd rather not flip the global toggle, the alternative is to authenticate
nobox with a separate "bot" GitHub account; a different actor bypasses the
self-action suppression entirely.

## Known risks

1. **GitHub TOS.** Using issues as a personal mailbox is a gray area;
   "excessive bulk content" in issues is prohibited. Don't run this at scale.
2. **Email notification settings are user-controlled.** If you have GitHub
   email notifications disabled, the whole loop silently breaks. `nobox doctor`
   posts a sentinel comment so you can confirm receipt.
3. **Reply tokens invalidate on password reset.** Old notification emails stop
   working as reply targets. The agent side (`gh auth`) keeps working.
4. **Trim quoted text in your replies.** When you reply to a notification
   email, your client usually inlines the message you're answering ("On …
   wrote: …"). nobox tries to strip this on ingest, but the heuristic is
   best-effort and only knows a handful of locale variants — delete the
   quoted body in your email client before sending for the cleanest
   agent-side reads. The raw body is always retained; `--raw` reveals it.

## License

MIT
