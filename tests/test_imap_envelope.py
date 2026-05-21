"""Envelope synthesis for IMAP. Requires the [imap] extra."""


import pytest

pytest.importorskip("twisted")

from nobox.imap_server import NoboxMessage
from nobox.state import InboxRow, MessageRow


def _inbox() -> InboxRow:
    return InboxRow(
        name="daily",
        kind="issue",
        repo="me/test",
        issue_number=1,
        gist_id=None,
        user_login="me",
        created_at="2026-05-21T13:00:00Z",
        uid_validity=12345,
        pgp_enabled=False,
        imap_password="pw",
    )


def _msg() -> MessageRow:
    return MessageRow(
        comment_id=987,
        inbox_name="daily",
        direction="in",
        author_login="alice",
        created_at="2026-05-21T14:00:00Z",
        updated_at="2026-05-21T14:00:00Z",
        body_raw="hello\n",
        body_trimmed="hello\n",
        body_decrypted=None,
        subject="Re: daily",
        in_reply_to_id=100,
    )


def test_envelope_message_id_stable():
    m = NoboxMessage(_msg(), _inbox())
    headers = m.getHeaders(False)
    assert headers["Message-ID"] == "<comment-987@nobox.local>"


def test_in_reply_to_chain():
    m = NoboxMessage(_msg(), _inbox())
    headers = m.getHeaders(False)
    assert headers["In-Reply-To"] == "<comment-100@nobox.local>"
    assert headers["References"] == "<comment-100@nobox.local>"


def test_from_uses_github_noreply():
    m = NoboxMessage(_msg(), _inbox())
    headers = m.getHeaders(False)
    assert "alice via GitHub" in headers["From"]
    assert "noreply@github.com" in headers["From"]


def test_get_uid_is_comment_id():
    m = NoboxMessage(_msg(), _inbox())
    assert m.getUID() == 987


def test_body_file_returns_plaintext():
    m = NoboxMessage(_msg(), _inbox())
    body = m.getBodyFile().read()
    assert b"hello" in body
