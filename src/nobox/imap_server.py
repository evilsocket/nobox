"""Local IMAP server backed by GitHub.

Translates IMAP operations into GitHub REST calls (cached via SQLite) so
any mail client (Thunderbird, mutt) can point at `localhost:1143` and
browse / reply on a nobox inbox.

Scope (v2):
  - Bind to 127.0.0.1 only. PLAIN auth over loopback.
  - One IMAP user per inbox (username = inbox name).
  - Folders: INBOX (incoming), Sent (outgoing), Drafts (local), Trash.
  - Read: LOGIN, LIST, LSUB, SELECT, EXAMINE, STATUS, UID FETCH (BODY[],
    BODYSTRUCTURE, ENVELOPE, FLAGS, INTERNALDATE), UID SEARCH, NOOP, CHECK,
    CLOSE, LOGOUT, EXPUNGE.
  - Write:
      * UID STORE (flags) — notifies listeners on change so other connected
        clients see read/unread updates in real time.
      * APPEND to Drafts — local-only stash.
      * APPEND to Sent — parses the RFC822 body + In-Reply-To, then posts a
        real comment to the inbox issue via service.send_message. This is
        how "Send" works from a regular mail client.
  - IDLE — addListener spins up a background LoopingCall (60s) that polls
    GitHub; when the message count changes, newMessages is fired so IDLE
    clients receive an untagged EXISTS response.

Requires the `[imap]` extra (Twisted).
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import email
import email.message
import email.parser
import email.policy
import email.utils
import io
import logging
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass


def _offline() -> bool:
    """Test hook: NOBOX_OFFLINE=1 skips the per-call GitHub sync.

    Useful for e2e tests that drive the IMAP protocol over real TCP but want
    to serve from the pre-seeded local SQLite mirror instead of hitting the
    real GitHub API on every FETCH.
    """
    return os.environ.get("NOBOX_OFFLINE") == "1"


def _iso_to_datetime(s: str) -> _dt.datetime | None:
    """Parse an ISO-8601 timestamp (possibly Z-suffixed) to an aware datetime."""
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return _dt.datetime.fromisoformat(s)
    except ValueError:
        return None


try:
    from twisted.cred import checkers, credentials, error, portal
    from twisted.cred.portal import IRealm
    from twisted.internet import defer, endpoints, reactor
    from twisted.internet.task import LoopingCall
    from twisted.internet.threads import deferToThread
    from twisted.mail import imap4
    from zope.interface import implementer
except ImportError as e:
    raise ImportError(
        "IMAP support requires the [imap] extra: pip install 'nobox[imap]'"
    ) from e

from nobox import inbox as inbox_mod  # noqa: E402
from nobox import poller, service, state  # noqa: E402
from nobox.auth import resolve_token  # noqa: E402
from nobox.github_client import GitHubClient  # noqa: E402
from nobox.state import DraftRow, InboxRow, MessageRow  # noqa: E402

log = logging.getLogger(__name__)

FOLDERS = ("INBOX", "Sent", "Drafts", "Trash")
IDLE_POLL_INTERVAL = 60.0  # seconds between background polls while IDLE
COMMENT_ID_RE = re.compile(r"<comment-(\d+)@nobox\.local>", re.IGNORECASE)
_HTML_TAG_RE = re.compile(r"<[^>]+>")


# ---------- credentials ---------------------------------------------------


@implementer(checkers.ICredentialsChecker)
class InboxPasswordChecker:
    credentialInterfaces = (credentials.IUsernamePassword,)

    def requestAvatarId(self, creds):
        username = creds.username.decode() if isinstance(creds.username, bytes) else creds.username
        password = creds.password.decode() if isinstance(creds.password, bytes) else creds.password
        try:
            row = inbox_mod.load_inbox(username)
        except Exception:  # noqa: BLE001
            return defer.fail(error.UnauthorizedLogin())
        if password != row.imap_password:
            return defer.fail(error.UnauthorizedLogin())
        return defer.succeed(username)


# ---------- message -------------------------------------------------------


@implementer(imap4.IMessage)
class NoboxMessage:
    def __init__(self, row: MessageRow, inbox: InboxRow):
        self.row = row
        self.inbox = inbox
        self._mime: email.message.EmailMessage | None = None

    def _build_mime(self) -> email.message.EmailMessage:
        if self._mime is not None:
            return self._mime
        m = email.message.EmailMessage()
        m["Message-ID"] = f"<comment-{self.row.comment_id}@nobox.local>"
        if self.row.in_reply_to_id:
            m["In-Reply-To"] = f"<comment-{self.row.in_reply_to_id}@nobox.local>"
            m["References"] = f"<comment-{self.row.in_reply_to_id}@nobox.local>"
        dt = _iso_to_datetime(self.row.created_at)
        if dt:
            m["Date"] = email.utils.format_datetime(dt)
        author = self.row.author_login or "unknown"
        m["From"] = f'"{author} via GitHub" <noreply@github.com>'
        m["To"] = f'"{self.inbox.user_login}" <{self.inbox.user_login}@nobox.local>'
        m["Subject"] = self.row.subject or f"Re: {self.inbox.name}"
        body = (
            self.row.body_decrypted
            or self.row.body_trimmed
            or self.row.body_raw
            or ""
        )
        m.set_content(body)
        self._mime = m
        return m

    # IMessage --------

    def getUID(self) -> int:
        return self.row.comment_id

    def getFlags(self) -> Iterable[str]:
        return self.row.flags

    def getInternalDate(self) -> bytes:
        dt = _iso_to_datetime(self.row.created_at)
        if dt is None:
            return b""
        return email.utils.format_datetime(dt).encode("ascii")

    def getHeaders(self, negate: bool, *names):
        mime = self._build_mime()
        names_set = {n.lower() for n in names} if names else None
        out = {}
        for k, v in mime.items():
            if names_set is None:
                out[k] = v
                continue
            in_set = k.lower() in names_set
            if (in_set and not negate) or (not in_set and negate):
                out[k] = v
        return out

    def getBodyFile(self):
        mime = self._build_mime()
        content = mime.get_content()
        return io.BytesIO(content.encode("utf-8") if isinstance(content, str) else content)

    def getSize(self) -> int:
        return len(bytes(self._build_mime()))

    def isMultipart(self) -> bool:
        return False

    def getSubPart(self, part: int):
        raise IndexError(part)


# ---------- folder spec ---------------------------------------------------


@dataclass
class FolderSpec:
    name: str
    direction: str | None  # "in" | "out" | None (Drafts)
    flags_required: list[str]
    flags_excluded: list[str]


_FOLDER_SPECS = {
    "INBOX": FolderSpec("INBOX", "in", [], ["\\Deleted"]),
    "Sent": FolderSpec("Sent", "out", [], ["\\Deleted"]),
    "Drafts": FolderSpec("Drafts", None, ["\\Draft"], []),
    "Trash": FolderSpec("Trash", None, ["\\Deleted"], []),
}


# ---------- mailbox -------------------------------------------------------


@implementer(imap4.IMailbox)
class NoboxMailbox:
    def __init__(self, inbox: InboxRow, folder: str):
        self.inbox = inbox
        self.folder = folder
        self.spec = _FOLDER_SPECS[folder]
        self.listeners: list = []
        self._idle_loop: LoopingCall | None = None
        self._last_count: int | None = None

    # ---- internal helpers -------------------------------------------------

    def _fetch_from_github(self) -> int:
        """Sync GitHub → SQLite. Returns count of new messages persisted."""
        if _offline():
            return 0
        token = resolve_token()
        with GitHubClient(token) as c:
            since = poller.latest_watermark(self.inbox.name)
            return poller.sync_inbox(c, self.inbox, since=since)

    def _messages(self) -> list[MessageRow]:
        with state.connect() as conn:
            rows = state.list_messages(
                conn,
                self.inbox.name,
                direction=self.spec.direction,
            )
        out = []
        for r in rows:
            if any(f in r.flags for f in self.spec.flags_excluded):
                continue
            if any(f not in r.flags for f in self.spec.flags_required):
                continue
            out.append(r)
        return out

    def _notify_new_messages(self) -> None:
        count = self.getMessageCount()
        if self._last_count is None:
            self._last_count = count
            return
        if count != self._last_count:
            for listener in list(self.listeners):
                try:
                    listener.newMessages(count, 0)
                except Exception:  # noqa: BLE001
                    log.exception("listener.newMessages failed")
            self._last_count = count

    def _notify_flags_changed(self, changes: dict[int, list[str]]) -> None:
        """Per Twisted's IMessageListener: flagsChanged takes one dict arg."""
        if not changes:
            return
        for listener in list(self.listeners):
            try:
                listener.flagsChanged(dict(changes))
            except Exception:  # noqa: BLE001
                log.exception("listener.flagsChanged failed")

    def _idle_tick(self):
        """LoopingCall body. Polls GitHub off-thread, then notifies listeners."""
        d = deferToThread(self._fetch_from_github)

        def _ok(_n):
            self._notify_new_messages()

        def _err(failure):
            log.warning("nobox IMAP idle poll failed: %s", failure.value)

        d.addCallbacks(_ok, _err)
        return d

    # ---- IMailbox ---------------------------------------------------------

    def getUIDValidity(self) -> int:
        return self.inbox.uid_validity

    def getUIDNext(self) -> int:
        msgs = self._messages()
        if not msgs:
            return 1
        return max(m.comment_id for m in msgs) + 1

    def getUID(self, msg_index: int) -> int:
        msgs = self._messages()
        return msgs[msg_index - 1].comment_id

    def getMessageCount(self) -> int:
        return len(self._messages())

    def getRecentCount(self) -> int:
        return 0

    def getUnseenCount(self) -> int:
        return sum(1 for m in self._messages() if "\\Seen" not in m.flags)

    def isWriteable(self) -> bool:
        return True

    def getHierarchicalDelimiter(self) -> str:
        return "/"

    def requestStatus(self, names):
        names = [n.decode() if isinstance(n, bytes) else n for n in names]
        result = {}
        for n in names:
            n_upper = n.upper()
            if n_upper == "MESSAGES":
                result[n] = self.getMessageCount()
            elif n_upper == "RECENT":
                result[n] = self.getRecentCount()
            elif n_upper == "UNSEEN":
                result[n] = self.getUnseenCount()
            elif n_upper == "UIDNEXT":
                result[n] = self.getUIDNext()
            elif n_upper == "UIDVALIDITY":
                result[n] = self.getUIDValidity()
        return defer.succeed(result)

    def getFlags(self):
        return [r"\Seen", r"\Answered", r"\Flagged", r"\Deleted", r"\Draft"]

    def getPermanentFlags(self):
        return [r"\Seen", r"\Answered", r"\Flagged", r"\Deleted", r"\Draft"]

    # ---- listener management (IDLE) --------------------------------------

    def addListener(self, listener):
        self.listeners.append(listener)
        # Start the background poll if this is the first listener.
        if self._idle_loop is None:
            self._last_count = self.getMessageCount()
            self._idle_loop = LoopingCall(self._idle_tick)
            try:
                self._idle_loop.start(IDLE_POLL_INTERVAL, now=False)
            except Exception:  # noqa: BLE001
                # Reactor not running (e.g. inside a unit test); fine.
                self._idle_loop = None

    def removeListener(self, listener):
        if listener in self.listeners:
            self.listeners.remove(listener)
        if not self.listeners and self._idle_loop is not None:
            if self._idle_loop.running:
                self._idle_loop.stop()
            self._idle_loop = None

    # ---- read -------------------------------------------------------------

    def fetch(self, messages, uid):
        # Sync once before serving so the client sees fresh data on demand
        # FETCH outside an IDLE window.
        try:
            self._fetch_from_github()
        except Exception:  # noqa: BLE001
            log.exception("nobox IMAP fetch sync failed; serving cached")
        msgs = self._messages()

        # Defensively bind the upper bound of the MessageSet. Twisted's
        # IMAP4Server is supposed to set messages.last before handing us the
        # set, but in 26.x it sometimes leaves it None when the client uses
        # the "*" wildcard, which makes iteration raise "Can't iterate; last
        # value not set". Setting it from our own count is harmless when it
        # was already set.
        upper = (max((m.comment_id for m in msgs), default=0) if uid else len(msgs))
        if upper < 1:
            upper = 1
        with contextlib.suppress(Exception):
            messages.last = upper

        # Twisted expects results keyed by *sequence number* (1-based), even
        # for UID FETCH — the IMAP response formatter then appends the UID
        # field separately via msg.getUID(). So we always emit seq numbers.
        results = []
        if uid:
            uid_to_seq = {m.comment_id: i + 1 for i, m in enumerate(msgs)}
            for uid_v in messages:
                seq = uid_to_seq.get(uid_v)
                if seq is not None:
                    results.append((seq, NoboxMessage(msgs[seq - 1], self.inbox)))
        else:
            for seq, m in enumerate(msgs, start=1):
                if seq in messages:
                    results.append((seq, NoboxMessage(m, self.inbox)))
        return results

    # ---- store (flags) ----------------------------------------------------

    def store(self, messages, flags, mode, uid):
        flags = [f.decode() if isinstance(f, bytes) else f for f in flags]
        msgs = self._messages()
        # Bind upper bound (see fetch() for the same defensive fix).
        upper = (max((m.comment_id for m in msgs), default=0) if uid else len(msgs))
        if upper < 1:
            upper = 1
        with contextlib.suppress(Exception):
            messages.last = upper

        # Build (seq, message) target list. Twisted treats store() result
        # keys as *sequence numbers* and calls getUID(seq) on them to produce
        # the " UID <n>" suffix for UID STORE responses — so we MUST key by
        # seq, not by comment_id.
        targets: list[tuple[int, MessageRow]] = []
        if uid:
            uid_to_seq = {m.comment_id: i + 1 for i, m in enumerate(msgs)}
            for u in messages:
                seq = uid_to_seq.get(u)
                if seq is not None:
                    targets.append((seq, msgs[seq - 1]))
        else:
            for i in messages:
                if 1 <= i <= len(msgs):
                    targets.append((i, msgs[i - 1]))

        result: dict[int, list[str]] = {}
        with state.connect() as conn:
            for seq, m in targets:
                current = set(m.flags)
                if mode == 1:  # add
                    new = list(current | set(flags))
                elif mode == -1:  # remove
                    new = list(current - set(flags))
                else:  # 0: replace
                    new = list(flags)
                state.set_flags(conn, m.comment_id, new)
                result[seq] = new
        # Tell any IDLE clients that flags changed.
        self._notify_flags_changed(result)
        return defer.succeed(result)

    # ---- append (Drafts + Sent) ------------------------------------------

    def addMessage(self, message, flags=(), date=None):
        raw = message.read() if hasattr(message, "read") else bytes(message)
        raw_bytes = raw.encode("utf-8", errors="replace") if isinstance(raw, str) else raw

        if self.folder == "Drafts":
            return self._append_draft(raw_bytes)
        if self.folder == "Sent":
            return self._append_sent(raw_bytes)
        return defer.fail(
            imap4.MailboxException(
                f"APPEND to {self.folder} not supported — send via Sent or stash via Drafts"
            )
        )

    def _append_draft(self, raw_bytes: bytes):
        import uuid as _uuid

        body = raw_bytes.decode("utf-8", errors="replace")
        d = DraftRow(
            draft_id=_uuid.uuid4().hex,
            inbox_name=self.inbox.name,
            body=body,
            in_reply_to_id=None,
            created_at=state._now_iso(),
        )
        with state.connect() as conn:
            state.insert_draft(conn, d)
        return defer.succeed(None)

    def _append_sent(self, raw_bytes: bytes):
        """Treat an APPEND to Sent as 'send this' — post via GitHub API.

        The mail client just delivered an RFC822 message it wants archived
        in Sent. We interpret that as "the user is sending this from their
        client" and post a real GitHub comment.
        """
        parser = email.parser.BytesParser(policy=email.policy.default)
        msg = parser.parsebytes(raw_bytes)
        body = _extract_text_body(msg)
        in_reply_to = _extract_in_reply_to(msg)

        # Run the GitHub POST off-thread so we don't block the reactor.
        d = deferToThread(
            service.send_message, self.inbox, body, in_reply_to=in_reply_to
        )

        def _ok(_payload):
            # Refresh count + notify IDLE clients that there's a new Sent entry.
            self._notify_new_messages()

        def _err(failure):
            log.warning("nobox IMAP APPEND-to-Sent post failed: %s", failure.value)
            return failure

        d.addCallbacks(_ok, _err)
        return d

    # ---- expunge / close --------------------------------------------------

    def expunge(self):
        # \Deleted messages are already hidden from INBOX/Sent listings via
        # the folder spec. EXPUNGE here returns the UIDs that *would* have
        # been removed so the IMAP layer can emit untagged EXPUNGE responses;
        # nothing is destroyed in SQLite.
        with state.connect() as conn:
            rows = state.list_messages(conn, self.inbox.name)
        expunged = [r.comment_id for r in rows if "\\Deleted" in r.flags]
        return defer.succeed(expunged)

    def close(self):
        # Called on CLOSE or LOGOUT — stop the IDLE loop if any client is
        # still attached when the connection drops.
        if self._idle_loop is not None and self._idle_loop.running:
            self._idle_loop.stop()
        self._idle_loop = None
        self.listeners.clear()
        return defer.succeed(None)


# ---------- body extraction helpers --------------------------------------


def _extract_text_body(msg: email.message.EmailMessage) -> str:
    """Pull the plaintext body out of an RFC822 message.

    Prefers text/plain. Falls back to a crude HTML strip for text/html only.
    """
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                try:
                    return _ensure_str(part.get_content()).strip()
                except Exception:  # noqa: BLE001
                    pass
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                try:
                    return _HTML_TAG_RE.sub("", _ensure_str(part.get_content())).strip()
                except Exception:  # noqa: BLE001
                    pass
        return ""
    try:
        body = _ensure_str(msg.get_content())
    except Exception:  # noqa: BLE001
        body = msg.as_string()
    if msg.get_content_type() == "text/html":
        body = _HTML_TAG_RE.sub("", body)
    return body.strip()


def _extract_in_reply_to(msg: email.message.EmailMessage) -> int | None:
    """Map the In-Reply-To header back to a nobox comment_id, if possible."""
    for header in ("In-Reply-To", "References"):
        v = msg.get(header) or ""
        m = COMMENT_ID_RE.search(v)
        if m:
            return int(m.group(1))
    return None


def _ensure_str(content) -> str:
    if isinstance(content, bytes):
        return content.decode("utf-8", errors="replace")
    return str(content)


# ---------- account / realm ----------------------------------------------


@implementer(imap4.IAccount)
class NoboxAccount:
    def __init__(self, inbox_name: str):
        self.inbox = inbox_mod.load_inbox(inbox_name)

    def listMailboxes(self, ref, wildcard):
        return [(name, NoboxMailbox(self.inbox, name)) for name in FOLDERS]

    def select(self, name, readwrite=1):
        name = name.decode() if isinstance(name, bytes) else name
        if name not in FOLDERS:
            return None
        return NoboxMailbox(self.inbox, name)

    def create(self, pathspec):
        return defer.fail(imap4.MailboxCollision("nobox folders are fixed"))

    def delete(self, name):
        return defer.fail(imap4.MailboxException("delete not supported"))

    def rename(self, oldname, newname):
        return defer.fail(imap4.MailboxException("rename not supported"))

    def isSubscribed(self, name):
        return name in FOLDERS

    def subscribe(self, name):
        return defer.succeed(None)

    def unsubscribe(self, name):
        return defer.succeed(None)


@implementer(IRealm)
class NoboxRealm:
    def requestAvatar(self, avatarId, mind, *interfaces):
        if imap4.IAccount in interfaces:
            avatar = NoboxAccount(avatarId)
            return (imap4.IAccount, avatar, lambda: None)
        raise NotImplementedError("only IAccount supported")


# ---------- entry ---------------------------------------------------------


def serve(*, host: str = "127.0.0.1", port: int = 1143) -> None:
    from twisted.internet.protocol import ServerFactory

    class _Factory(ServerFactory):
        def __init__(self, p):
            self._portal = p

        def buildProtocol(self, addr):
            proto = imap4.IMAP4Server()
            proto.portal = self._portal
            return proto

    realm = NoboxRealm()
    p = portal.Portal(realm)
    p.registerChecker(InboxPasswordChecker())

    factory = _Factory(p)
    endpoint = endpoints.TCP4ServerEndpoint(reactor, port, interface=host)
    endpoint.listen(factory)
    print(f"nobox IMAP server listening on {host}:{port}")
    print("  username = inbox name")
    print("  password = cat ~/.local/state/nobox/inboxes/<name>/imap.password")
    reactor.run()
