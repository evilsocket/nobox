"""Local IMAP server backed by GitHub.

Translates IMAP read operations into GitHub REST calls (cached via SQLite)
so any mail client (Thunderbird, mutt) can point at `localhost:1143` and
browse a nobox inbox.

Scope (v1):
  - Bind to 127.0.0.1 only. PLAIN auth over loopback.
  - One IMAP user per inbox (username = inbox name).
  - Folders: INBOX (incoming), Sent (outgoing), Drafts (local), Trash.
  - Supported: LOGIN, LIST, LSUB, SELECT, EXAMINE, STATUS, UID FETCH,
    UID STORE, UID SEARCH, NOOP, CHECK, CLOSE, LOGOUT, EXPUNGE (no-op).
  - Not supported in v1: IDLE, APPEND-to-Sent (use CLI/MCP `send` instead),
    COPY, MOVE.

Requires the `[imap]` extra (Twisted).
"""

from __future__ import annotations

import datetime as _dt
import email
import email.message
import email.utils
import io
from collections.abc import Iterable
from dataclasses import dataclass


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
    from twisted.mail import imap4
    from zope.interface import implementer
except ImportError as e:
    raise ImportError(
        "IMAP support requires the [imap] extra: pip install 'nobox[imap]'"
    ) from e

from nobox import inbox as inbox_mod  # noqa: E402
from nobox import poller, state  # noqa: E402
from nobox.auth import resolve_token  # noqa: E402
from nobox.github_client import GitHubClient  # noqa: E402
from nobox.state import InboxRow, MessageRow  # noqa: E402

FOLDERS = ("INBOX", "Sent", "Drafts", "Trash")


# ---------- credentials ---------------------------------------------------


@implementer(checkers.ICredentialsChecker)
class InboxPasswordChecker:
    credentialInterfaces = (credentials.IUsernamePassword,)

    def requestAvatarId(self, creds):
        username = creds.username.decode() if isinstance(creds.username, bytes) else creds.username
        password = creds.password.decode() if isinstance(creds.password, bytes) else creds.password
        try:
            row = inbox_mod.load_inbox(username)
        except Exception:
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
        return io.BytesIO(mime.get_content().encode("utf-8") if isinstance(mime.get_content(), str) else mime.get_content())

    def getSize(self) -> int:
        return len(bytes(self._build_mime()))

    def isMultipart(self) -> bool:
        return False

    def getSubPart(self, part: int):
        raise IndexError(part)


# ---------- mailbox -------------------------------------------------------


@dataclass
class FolderSpec:
    name: str
    direction: str | None  # "in" | "out" | None (drafts)
    flags_required: list[str]
    flags_excluded: list[str]


_FOLDER_SPECS = {
    "INBOX": FolderSpec("INBOX", "in", [], ["\\Deleted"]),
    "Sent": FolderSpec("Sent", "out", [], ["\\Deleted"]),
    "Drafts": FolderSpec("Drafts", None, ["\\Draft"], []),
    "Trash": FolderSpec("Trash", None, ["\\Deleted"], []),
}


@implementer(imap4.IMailbox)
class NoboxMailbox:
    def __init__(self, inbox: InboxRow, folder: str):
        self.inbox = inbox
        self.folder = folder
        self.spec = _FOLDER_SPECS[folder]
        self.listeners: list = []

    # ---- listing helpers

    def _fetch_from_github(self) -> None:
        """Pull new comments before listing."""
        token = resolve_token()
        with GitHubClient(token) as c:
            since = poller.latest_watermark(self.inbox.name)
            poller.sync_inbox(c, self.inbox, since=since)

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

    # ---- IMailbox

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
        # v1: we accept STORE for flags only. APPEND/COPY/MOVE are rejected.
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

    def addListener(self, listener):
        self.listeners.append(listener)

    def removeListener(self, listener):
        if listener in self.listeners:
            self.listeners.remove(listener)

    def fetch(self, messages, uid):
        self._fetch_from_github()
        msgs = self._messages()
        if uid:
            id_to_msg = {m.comment_id: m for m in msgs}
            for uid_v in messages:
                m = id_to_msg.get(uid_v)
                if m is None:
                    continue
                yield (uid_v, NoboxMessage(m, self.inbox))
        else:
            for seq, m in enumerate(msgs, start=1):
                if seq in messages:
                    yield (seq, NoboxMessage(m, self.inbox))

    def store(self, messages, flags, mode, uid):
        flags = [f.decode() if isinstance(f, bytes) else f for f in flags]
        msgs = self._messages()
        if uid:
            id_to_msg = {m.comment_id: m for m in msgs}
            targets = [id_to_msg[u] for u in messages if u in id_to_msg]
        else:
            targets = [msgs[i - 1] for i in messages if 1 <= i <= len(msgs)]
        result = {}
        with state.connect() as conn:
            for m in targets:
                current = set(m.flags)
                if mode == 1:  # add
                    new = list(current | set(flags))
                elif mode == -1:  # remove
                    new = list(current - set(flags))
                else:  # 0: replace
                    new = list(flags)
                state.set_flags(conn, m.comment_id, new)
                result[m.comment_id] = new
        return defer.succeed(result)

    def addMessage(self, message, flags=(), date=None):
        if self.folder != "Drafts":
            return defer.fail(imap4.MailboxException("APPEND only allowed to Drafts in v1"))
        # Read the RFC822 stream and save a draft locally.
        raw = message.read() if hasattr(message, "read") else bytes(message)
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        import uuid as _uuid

        from nobox.state import DraftRow

        d = DraftRow(
            draft_id=_uuid.uuid4().hex,
            inbox_name=self.inbox.name,
            body=raw,
            in_reply_to_id=None,
            created_at=state._now_iso(),
        )
        with state.connect() as conn:
            state.insert_draft(conn, d)
        return defer.succeed(None)

    def expunge(self):
        # Permanently apply \Deleted flag locally (we don't delete GitHub-side).
        # Returns the message numbers that were expunged.
        msgs = self._messages()
        expunged = [m.comment_id for m in msgs if "\\Deleted" in m.flags]
        return defer.succeed(expunged)

    def close(self):
        return defer.succeed(None)


# ---------- account / realm ----------------------------------------------


@implementer(imap4.IAccount)
class NoboxAccount:
    def __init__(self, inbox_name: str):
        self.inbox = inbox_mod.load_inbox(inbox_name)

    def listMailboxes(self, ref, wildcard):
        # Return (name, mailbox) pairs that match the reference + wildcard.
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


class NoboxIMAPFactory:
    def buildProtocol(self, addr):

        proto = imap4.IMAP4Server()
        proto.portal = self._portal
        return proto

    def __init__(self):
        realm = NoboxRealm()
        p = portal.Portal(realm)
        p.registerChecker(InboxPasswordChecker())
        self._portal = p
        self.protocol = imap4.IMAP4Server


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
