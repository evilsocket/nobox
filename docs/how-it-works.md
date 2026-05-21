---
title: How it works — reverse-engineering GitHub's reply-by-email token
description: Field-by-field breakdown of GitHub's reply+<TOKEN>@reply.github.com address. Base32-encoded uid + MAC + thread_id + msgpack tail; the legacy hex variant; the SMTP no-validation behavior; the security implications.
---

# How it works

When somebody comments on a GitHub issue you're subscribed to (or
@mentioned in), GitHub emails you from `reply+<TOKEN>@reply.github.com`.
Replies to that address are posted back to the same thread as a comment.
**nobox is just glue around that mechanism**: the human uses their normal
email client; the agent uses the GitHub REST API; nobox stays on the
agent side.

## The token isn't opaque

> **⚠ Never share a `reply+<TOKEN>@reply.github.com` address with anyone.
> Each token is effectively a scoped bearer credential — it lets whoever
> holds it post a comment _as you_, on that specific issue, simply by
> SMTPing a reply. The MAC stops forgery, but if the token itself leaks
> (forwarded email, screenshot, paste into a chat), the holder can
> impersonate you on that thread. The only way to revoke an outstanding
> reply token is to change your GitHub password — that rotates the keying
> material and invalidates every still-live token at once.**

`<TOKEN>` is a structured binary blob — base32-encoded without padding,
26–30 bytes on the wire. (The old hex variant was twice as long and was
compressed to base32 around 2022.)

```
uid(4) || mac(10) || thread_id(4) || msgpack([kind, subject_id])
```

### `uid` — 4 bytes, big-endian uint32, **in the clear**

Recipient's GitHub user ID. The first 7 base32 chars of any token
directly identify the recipient — no API call needed. Leaked notification
emails are therefore trivially attributable to a specific GitHub user via
`github.com/<username>.png` reverse-mapping.

### `mac` — 10 bytes / 80-bit authenticator

Empirically depends on `uid`, `thread_id`, and almost certainly the
`(kind, subject_id)` tuple as well. Different recipients of the same
notification get different MACs; different notifications for the same
subject get different MACs. No field can be mutated without invalidating
it.

**The keying material rotates on GitHub password reset.** Old reply
tokens stop working once you change your password — that's the only
externally-visible revocation path.

### `thread_id` — 4 bytes, big-endian uint32

A global notification-delivery counter. Shared across all recipients of
one notification event, unique per delivery (same user receiving two
notifications about the same subject gets two different `thread_id`s).

Currently in the 1.78B range and growing ~1B every six months. GitHub
will need to migrate the field before it hits 2³² in roughly two years.

### `msgpack` tail — 2-element array `[kind, subject_id]`

`kind` is a single ASCII char identifying the subject type:

| Char | Subject type | Confirmed in the wild? |
|------|--------------|------------------------|
| `'i'` | Issue | Yes |
| `'g'` | Gist | Yes |
| `'p'` | Pull request | Inferred |
| `'c'` | Commit comment | Inferred |
| `'d'` | Discussion | Inferred |
| `'r'` | Release | Inferred |

`subject_id` is GitHub's global database ID for the subject (not the
per-repo `#N`) — encoded with msgpack's int-tag autosizing:
`ce` + 4 bytes for `uint32` (below 2³²), `cf` + 8 bytes for `uint64`
above.

**The token has no repo field.** GitHub recovers the repo server-side by
joining on `subject_id`. Externally you can do the same via GraphQL
`node(id: "MDU6SXNzdWU<base64-encoded-N>")`.

## Legacy hex variant

A pre-base32 hex-encoded variant predates the cutover around 2022:

```
uid(4) || mac(20) || msgpack([thread_id_u64, [kind, subject_id]])
```

Same fields, but **longer MAC** (160 bits), `thread_id` as a `uint64`
inside the msgpack instead of in its own slot, wrapped in hex on the wire
(60+ chars). GitHub's `metroplex` daemon still accepts both formats, so
archived notification emails from before the cutover remain valid until
the recipient's next password reset.

## SMTP-layer behaviour

At the SMTP layer there's effectively **no validation**. The six
round-robin MX hosts (`in-{5..10}.smtp.github.com`, on a shared cert with
`*.smtp.ghe.com` covering Enterprise Cloud) accept anything syntactically
valid:

- Any recipient (`reply+<anything>@reply.github.com`).
- Any sender.
- Any token mutation.
- Even arbitrary local-part prefixes (`notifications+`, `noreply+`,
  `postmaster`, …).

All real token validation happens **post-DATA inside metroplex**, which
means:

- **No liveness oracle** via SMTP probing — you cannot distinguish a
  valid token from an invalid one at the protocol level.
- **The MAC is the only thing stopping forgery.**
- `uid` being plaintext in the prefix means leaked notification emails
  are directly attributable to specific users without any GitHub API
  call.

## Why nobox doesn't decode any of this

nobox itself does not decode the token. It only needs the GitHub REST
API on the agent side and the user's regular reply-to-email behaviour on
the human side. The token research is *why we know this works* — it
proves the design isn't fragile and isn't relying on undocumented
internals being friendly. The reply pipeline has been stable since 2011
(the year the feature shipped), with the only externally visible change
being the hex→base32 cutover.
