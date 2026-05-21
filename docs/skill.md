---
title: Claude Code Skill
description: Install the bundled SKILL.md so Claude Code automatically uses nobox tools when the user asks for async email-style messaging.
---

# Claude Code Skill

nobox ships a [Claude Code Skill](https://code.claude.com/docs/en/skills)
that tells Claude Code (and any other client that consumes skill files)
what nobox is, when to use it, what the tools mean, and how to behave
around polling and destructive operations.

## Install

```bash
nobox install-skill
```

That copies the bundled `SKILL.md` to `~/.claude/skills/nobox/SKILL.md`.

For a project-scoped install (only active in one repo):

```bash
nobox install-skill --project /path/to/your/project
```

Drops it into `/path/to/your/project/.claude/skills/nobox/SKILL.md`.

After install, restart any Claude Code session and the skill becomes
discoverable. You can also invoke it explicitly with `/nobox`.

## What the skill tells Claude

The skill description triggers auto-invocation when the user asks for
async / leave-a-note / email-style follow-up patterns ("ping me when
done", "leave me a message", "check my inbox", "email me a summary"...).

The body covers:

- **When to reach for nobox** vs. normal interactive chat.
- **The tool table** with one-line guidance per tool.
- **Mental model**: `direction="in"` vs `"out"`, POP3-style retention,
  that doctor sentinels are auto-filtered, PGP transparency, the
  best-effort quoted-text trim.
- **Typical workflow**: `list_inboxes` → `read_inbox` (previews) →
  `read_message` for actionable items → `send_message` with `in_reply_to`.
- **Polling pattern**: if the runtime supports recurring tasks
  (`/schedule`, `/loop`, cron, etc.), Claude should propose a 10-minute
  recurring check **and only set it up after the user agrees** - never
  silently.
- **Safety**: `create_inbox` and `delete_inbox` are destructive; never
  call them without explicit user instruction.

## Updating

The skill ships with the package, so to refresh after a `nobox` upgrade:

```bash
nobox install-skill
```

(It overwrites the installed file.)

## Uninstalling

```bash
rm -rf ~/.claude/skills/nobox
```

## MCP + skill together

The recommended setup is to wire both:

```bash
# MCP gives Claude the actual tools
claude mcp add --transport stdio nobox -- nobox mcp

# Skill gives Claude the methodology
nobox install-skill
```

The MCP server provides connectivity (the actual `read_inbox` /
`send_message` calls); the skill provides the operating manual for using
them. They're complementary, not redundant.
