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

See the documentation at <https://nobox.evilsocket.net/> — the
[How it works](https://nobox.evilsocket.net/how-it-works/) page covers
the GitHub `reply+<TOKEN>@reply.github.com` token format we
reverse-engineered, the security implications, and why none of it has
to be fragile.

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
