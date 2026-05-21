---
title: PGP encryption
description: Opt-in per-inbox PGP keypair. nobox encrypts outgoing comments to the user's public key and decrypts incoming replies transparently. Inbox private key never touches GitHub.
---

# PGP

PGP support is opt-in per inbox. It requires the `[pgp]` install extra
(see [Installation](install.md)). When enabled, nobox:

- Generates a fresh **4096-bit RSA keypair for the inbox** at create time.
- Embeds the **public half** in the inbox issue body (collapsible `<details>`
  block, so it doesn't clutter the email subject thread).
- Stores the **private half** under
  `$XDG_STATE_HOME/nobox/inboxes/<name>/pgp.priv.asc`, mode `0600`. It
  never goes near the GitHub side.
- Copies your user public key into the same directory as `user.pub.asc`.

After that, encryption is transparent on both sides.

## Enabling on a new inbox

```bash
nobox create-inbox \
  --repo OWNER/REPO \
  --name secure \
  --pgp \
  --user-pgp-key ~/.gnupg/me.pub.asc
```

`--user-pgp-key` points at your armored PGP public key — that's the key
nobox encrypts outgoing comments to. Export it once with `gpg --armor
--export <your-fingerprint> > ~/.gnupg/me.pub.asc` if you don't have it
already.

## What happens on send

When the agent calls `send_message`:

1. The body is plaintext markdown.
2. nobox encrypts it to **your public key** and signs with the **inbox
   private key**.
3. The ASCII-armored ciphertext goes into a fenced `text` code block.
4. nobox appends its usual `<!-- nobox-meta {…, "pgp":true} -->` tag.
5. The whole thing is posted as a GitHub issue comment.

GitHub emails you a notification containing the armored block. You
decrypt it locally with **your** PGP private key.

## What happens on read

When the agent calls `read_message` (or any `read_inbox` with `decrypt=True`):

1. nobox fetches the comment body.
2. It looks for an ASCII-armored `-----BEGIN PGP MESSAGE-----` block.
3. If present, it decrypts with the **inbox private key**.
4. If a signer key is configured, it verifies the signature (failure is
   non-fatal; the plaintext is still returned with a warning).
5. The returned `body` field is plaintext.

`raw=true` always returns the untouched original (still encrypted), useful
for debugging.

## Your reply path

For incoming replies, the user is expected to encrypt to the inbox's
public key in their own email client (Enigmail, GPGSuite, Mailvelope,
etc.). The inbox public key is in the issue body for convenience — your
email client may auto-discover it via `gpg --import` from the page, or you
can paste it manually.

If you reply without encrypting, nobox still ingests the comment but
won't be able to decrypt anything — the body field will be plaintext as
posted.

## Key rotation

There's no built-in rotation command yet. To rotate:

1. `nobox delete-inbox --name X` (full wipe).
2. `nobox create-inbox --name X --pgp --user-pgp-key …` again.

A new inbox keypair is generated; the old private key file under
`$XDG_STATE_HOME/nobox/inboxes/X/` is removed as part of the delete.

## Threat model notes

- The inbox private key sits on your local filesystem with mode `0600`.
  Filesystem permissions are the trust boundary (no passphrase).
- GitHub sees only ciphertext for outgoing messages once PGP is on, but
  it still sees comment metadata: author login, timestamps, edit history,
  and the fact that comments exist.
- Token-level forgery on the GitHub side (someone with your
  `reply+<token>@reply.github.com` address) lets the attacker post
  comments as you, but they can't read the encrypted ciphertext you
  received because they don't have your PGP private key.
- The MAC keying material in GitHub reply tokens rotates on **GitHub
  password reset**. The PGP keypair is **independent** of that — rotating
  your GitHub password does not invalidate the PGP keys, and vice versa.

## CLI behaviour with PGP

| Subcommand | Effect with PGP enabled |
|------------|-------------------------|
| `read-inbox` | Previews show the decrypted plaintext (first 500 chars). |
| `read-message` | Returns decrypted plaintext in `body`. |
| `send` / `reply` | Encrypts to user pubkey + signs with inbox key. |
| `read-message --raw` | Returns the still-encrypted original. |
