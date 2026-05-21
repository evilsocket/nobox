---
title: Installation
description: Install nobox via uv tool or pipx straight from the GitHub repo. Optional [imap] and [pgp] extras. GitHub auth via gh CLI or GITHUB_TOKEN.
---

# Installation

nobox is not on PyPI. Install it straight from the GitHub repo with `uv` or
`pipx`. Python 3.11+ is required.

## Core install

=== "uv (recommended)"

    ```bash
    uv tool install 'git+https://github.com/evilsocket/nobox'
    ```

=== "pipx"

    ```bash
    pipx install 'git+https://github.com/evilsocket/nobox'
    ```

This puts a `nobox` executable in `~/.local/bin/` (uv) or your pipx bin
directory. Make sure that's on your `$PATH` - `uv tool update-shell` will
patch your shell rc if it isn't.

## With extras

Two optional extras:

| Extra | Adds | Why |
|-------|------|-----|
| `imap` | `twisted[tls]` | Required for `nobox serve-imap` (the local IMAP bridge). |
| `pgp` | `pgpy` | Required for PGP-enabled inboxes (`--pgp` flag on `create-inbox`). |

Install one or both:

```bash
# Just IMAP support
uv tool install 'nobox[imap] @ git+https://github.com/evilsocket/nobox'

# Just PGP support
uv tool install 'nobox[pgp] @ git+https://github.com/evilsocket/nobox'

# Everything
uv tool install 'nobox[all] @ git+https://github.com/evilsocket/nobox'
```

`[all]` is an alias for `[imap,pgp]`.

## GitHub authentication

nobox needs a GitHub token with `repo` scope. Order of precedence:

1. `GH_TOKEN` environment variable
2. `GITHUB_TOKEN` environment variable
3. `gh auth token` subprocess (the **recommended** path)

The easiest setup is the `gh` CLI:

```bash
gh auth login --scopes 'repo'
```

After this, nobox can pick up your token automatically - no environment
variables needed.

## Verifying the install

```bash
nobox --version
nobox --help
```

If the CLI starts but you see `IMAP support requires the [imap] extra`,
re-run the install with `[imap]` or `[all]`. Same for PGP.

## Critical one-time GitHub setting

GitHub suppresses email notifications for **your own actions** by default,
and nobox uses your token, so every comment it posts counts as a
self-action. Without this fix the loop silently breaks:

1. Visit <https://github.com/settings/notifications>
2. Under **Email notification preferences**, enable
 **"Include your own updates"**.
3. Run `nobox doctor --name <your-inbox>` to confirm the sentinel email
 arrives.

If you'd rather not flip the global toggle, authenticate nobox with a
separate "bot" GitHub account instead - a different actor bypasses the
self-action suppression entirely.

## Uninstall

```bash
uv tool uninstall nobox
# or
pipx uninstall nobox
```

Local state (per-inbox keys, the SQLite archive, etc.) lives under
`$XDG_STATE_HOME/nobox/`, `$XDG_DATA_HOME/nobox/`, and
`$XDG_CONFIG_HOME/nobox/`. Wipe those manually if you want a clean slate.
