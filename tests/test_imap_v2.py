"""Integration tests for IMAP server v2 features.

Covers:
  - APPEND to Sent parses the RFC822 message, extracts plaintext body and
    In-Reply-To, and posts via service.send_message.
  - STORE on a mailbox notifies registered listeners of flag changes (the
    push half of IDLE).
  - addListener / removeListener track listener lifecycle correctly.
  - The IDLE tick body fires newMessages on listeners when the count grows.
  - Body extraction helpers handle plain, multipart, and HTML inputs.
"""

from __future__ import annotations

from email.message import EmailMessage

import pytest

twisted = pytest.importorskip("twisted")

from nobox import imap_server, state  # noqa: E402, I001
from nobox.imap_server import NoboxMailbox, _extract_in_reply_to, _extract_text_body  # noqa: E402
from nobox.state import InboxRow, MessageRow  # noqa: E402

# ---------- shared fixtures ----------------------------------------------


@pytest.fixture
def inbox_row() -> InboxRow:
    row = InboxRow(
        name="daily",
        kind="issue",
        repo="me/test",
        issue_number=42,
        gist_id=None,
        user_login="me",
        created_at="2026-05-21T13:00:00Z",
        uid_validity=1,
        pgp_enabled=False,
        imap_password="pw",
        pop_mode=True,
    )
    with state.connect() as conn:
        state.insert_inbox(conn, row)
    return row


def _insert_msg(inbox: InboxRow, comment_id: int, direction: str = "in") -> None:
    with state.connect() as conn:
        state.upsert_message(
            conn,
            MessageRow(
                comment_id=comment_id,
                inbox_name=inbox.name,
                direction=direction,
                author_login="alice" if direction == "in" else inbox.user_login,
                created_at="2026-05-21T14:00:00Z",
                updated_at="2026-05-21T14:00:00Z",
                body_raw="hi",
                body_trimmed="hi",
                body_decrypted=None,
                subject=f"Re: {inbox.name}",
                in_reply_to_id=None,
            ),
        )


class _RecordingListener:
    """Stand-in for IMAP4Server connected via addListener; records callbacks."""

    def __init__(self):
        self.new_messages_calls: list[tuple[int, int]] = []
        self.flags_changed_calls: list[dict[int, list[str]]] = []

    def newMessages(self, exists, recent):
        self.new_messages_calls.append((exists, recent))

    def flagsChanged(self, changes):
        # Match Twisted's IMessageListener: a single {msg_id: [flags]} dict.
        self.flags_changed_calls.append({k: list(v) for k, v in changes.items()})


def _identity_deferToThread(monkeypatch):
    """Patch deferToThread to execute synchronously and wrap in a fired Deferred."""
    from twisted.internet import defer

    def _sync(fn, *args, **kwargs):
        d = defer.Deferred()
        try:
            d.callback(fn(*args, **kwargs))
        except Exception as e:  # noqa: BLE001
            d.errback(e)
        return d

    monkeypatch.setattr(imap_server, "deferToThread", _sync)


# ---------- body extraction helpers --------------------------------------


def test_extract_text_body_plaintext():
    msg = EmailMessage()
    msg["Subject"] = "Re: eddy"
    msg["From"] = "me@example.com"
    msg["To"] = "me@nobox.local"
    msg.set_content("Hello agent, please reply.\n\n--\nMe")
    body = _extract_text_body(msg)
    assert "Hello agent" in body


def test_extract_text_body_multipart_prefers_plain():
    msg = EmailMessage()
    msg["Subject"] = "x"
    msg.set_content("PLAIN TEXT")
    msg.add_alternative("<p>HTML</p>", subtype="html")
    body = _extract_text_body(msg)
    assert "PLAIN" in body
    assert "<p>" not in body


def test_extract_in_reply_to_maps_comment_id():
    msg = EmailMessage()
    msg["In-Reply-To"] = "<comment-123456789@nobox.local>"
    assert _extract_in_reply_to(msg) == 123456789


def test_extract_in_reply_to_falls_back_to_references():
    msg = EmailMessage()
    msg["References"] = "<unrelated@x.com> <comment-987@nobox.local>"
    assert _extract_in_reply_to(msg) == 987


def test_extract_in_reply_to_returns_none_when_unrelated():
    msg = EmailMessage()
    msg["In-Reply-To"] = "<random@elsewhere>"
    assert _extract_in_reply_to(msg) is None


# ---------- APPEND to Sent -----------------------------------------------


def test_append_sent_posts_via_service_send_message(inbox_row, monkeypatch):
    _identity_deferToThread(monkeypatch)
    captured: dict = {}

    def _fake_send(inbox, body, in_reply_to=None):
        captured["inbox"] = inbox
        captured["body"] = body
        captured["in_reply_to"] = in_reply_to
        return {"comment_id": 1, "body": body}

    monkeypatch.setattr(imap_server.service, "send_message", _fake_send)

    mb = NoboxMailbox(inbox_row, "Sent")
    raw = (
        b"From: me@nobox.local\r\n"
        b"To: agent@nobox.local\r\n"
        b"Subject: Re: eddy\r\n"
        b"In-Reply-To: <comment-555@nobox.local>\r\n"
        b"\r\n"
        b"Replying to the agent -- please proceed.\r\n"
    )
    d = mb.addMessage(raw)
    # Identity deferToThread => synchronous; the Deferred has fired.
    assert captured["body"].startswith("Replying to the agent")
    assert captured["in_reply_to"] == 555
    assert captured["inbox"].name == "daily"
    assert d.called


def test_append_sent_without_in_reply_to(inbox_row, monkeypatch):
    _identity_deferToThread(monkeypatch)
    captured: dict = {}
    monkeypatch.setattr(
        imap_server.service,
        "send_message",
        lambda inbox, body, in_reply_to=None: captured.update(
            body=body, in_reply_to=in_reply_to
        )
        or {"comment_id": 99},
    )

    mb = NoboxMailbox(inbox_row, "Sent")
    raw = (
        b"From: me@nobox.local\r\n"
        b"Subject: hello\r\n\r\n"
        b"Just a new message.\r\n"
    )
    mb.addMessage(raw)
    assert captured["in_reply_to"] is None
    assert "Just a new message" in captured["body"]


def test_append_to_inbox_is_rejected(inbox_row):
    mb = NoboxMailbox(inbox_row, "INBOX")
    d = mb.addMessage(b"From: x\r\n\r\nbody\r\n")
    # Should fail; the deferred carries an exception.
    failures = []
    d.addErrback(lambda f: failures.append(f))
    assert failures, "expected APPEND to INBOX to fail"


def test_append_to_drafts_stores_locally(inbox_row):
    mb = NoboxMailbox(inbox_row, "Drafts")
    d = mb.addMessage(b"From: me\r\nSubject: scratch\r\n\r\nDraft body.\r\n")
    assert d.called
    with state.connect() as conn:
        drafts = state.list_drafts(conn, inbox_row.name)
    assert len(drafts) == 1
    assert "Draft body" in drafts[0].body


# ---------- STORE notifies listeners (flagsChanged) ----------------------


def test_store_fires_flags_changed_on_listeners(inbox_row):
    _insert_msg(inbox_row, comment_id=101)
    mb = NoboxMailbox(inbox_row, "INBOX")
    listener = _RecordingListener()
    mb.listeners.append(listener)  # bypass addListener's reactor loop

    mb.store([101], [b"\\Seen"], mode=1, uid=True)

    # store() returns {seq_num: [flags]}; the only message has seq=1.
    assert listener.flags_changed_calls == [{1: ["\\Seen"]}]


def test_store_handles_multiple_messages(inbox_row):
    _insert_msg(inbox_row, 1)
    _insert_msg(inbox_row, 2)
    mb = NoboxMailbox(inbox_row, "INBOX")
    listener = _RecordingListener()
    mb.listeners.append(listener)

    mb.store([1, 2], [b"\\Flagged"], mode=1, uid=True)

    # One flagsChanged call carrying both seq numbers.
    assert len(listener.flags_changed_calls) == 1
    changes = listener.flags_changed_calls[0]
    assert sorted(changes.keys()) == [1, 2]
    assert all("\\Flagged" in v for v in changes.values())


# ---------- IDLE listener management -------------------------------------


def test_addremovelistener_tracks_listeners(inbox_row):
    mb = NoboxMailbox(inbox_row, "INBOX")
    l1 = _RecordingListener()
    l2 = _RecordingListener()
    mb.addListener(l1)
    mb.addListener(l2)
    assert l1 in mb.listeners and l2 in mb.listeners
    mb.removeListener(l1)
    assert l1 not in mb.listeners
    assert l2 in mb.listeners
    mb.removeListener(l2)
    assert mb.listeners == []


def test_idle_tick_notifies_listeners_when_count_grows(inbox_row, monkeypatch):
    _identity_deferToThread(monkeypatch)
    mb = NoboxMailbox(inbox_row, "INBOX")
    listener = _RecordingListener()
    mb.listeners.append(listener)
    mb._last_count = 0  # pretend we knew there were 0 messages

    # Make the GitHub fetch a no-op that just reports "1 new" — the actual
    # count is what NoboxMailbox.getMessageCount() reads from SQLite.
    monkeypatch.setattr(mb, "_fetch_from_github", lambda: 1)
    _insert_msg(inbox_row, comment_id=42)

    mb._idle_tick()

    assert listener.new_messages_calls == [(1, 0)]
    assert mb._last_count == 1


def test_idle_tick_silent_when_count_unchanged(inbox_row, monkeypatch):
    _identity_deferToThread(monkeypatch)
    mb = NoboxMailbox(inbox_row, "INBOX")
    listener = _RecordingListener()
    mb.listeners.append(listener)
    _insert_msg(inbox_row, comment_id=42)
    mb._last_count = 1  # already in sync
    monkeypatch.setattr(mb, "_fetch_from_github", lambda: 0)

    mb._idle_tick()

    assert listener.new_messages_calls == []


def test_close_clears_listeners_and_stops_loop(inbox_row):
    mb = NoboxMailbox(inbox_row, "INBOX")
    listener = _RecordingListener()
    mb.listeners.append(listener)
    # Pretend a loop exists (without actually starting it inside the reactor).
    class _StubLoop:
        running = True

        def stop(self):
            self.running = False

    stub = _StubLoop()
    mb._idle_loop = stub
    mb.close()
    assert not stub.running
    assert mb._idle_loop is None
    assert mb.listeners == []
