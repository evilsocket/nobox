"""End-to-end IMAP test driving the real server over TCP with imaplib.

Spawns `python -m nobox.cli serve-imap` as a subprocess with the test's
XDG dirs pointed at a tmp path that already contains a pre-populated
inbox + message. The server cannot reach GitHub (GH_TOKEN is faked) but
the fetch path catches API failures and falls back to the local SQLite
mirror, so LOGIN / SELECT / FETCH / STORE / SEARCH / APPEND-Drafts work
against the cached data.
"""

from __future__ import annotations

import contextlib
import imaplib
import os
import socket
import subprocess
import sys
import time

import pytest

pytest.importorskip("twisted")

from nobox import state  # noqa: E402
from nobox.state import InboxRow, MessageRow  # noqa: E402


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_port(port: int, timeout_s: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"IMAP server did not bind to 127.0.0.1:{port} within {timeout_s}s")


def _seed_state() -> InboxRow:
    inbox = InboxRow(
        name="daily",
        kind="issue",
        repo="me/test",
        issue_number=42,
        gist_id=None,
        user_login="me",
        created_at="2026-05-21T13:00:00Z",
        uid_validity=12345,
        pgp_enabled=False,
        imap_password="hunter2",
        pop_mode=True,
    )
    with state.connect() as conn:
        state.insert_inbox(conn, inbox)
        state.upsert_message(
            conn,
            MessageRow(
                comment_id=901,
                inbox_name=inbox.name,
                direction="in",
                author_login="alice",
                created_at="2026-05-21T14:00:00Z",
                updated_at="2026-05-21T14:00:00Z",
                body_raw="hello over IMAP",
                body_trimmed="hello over IMAP",
                body_decrypted=None,
                subject="Re: daily",
                in_reply_to_id=None,
            ),
        )
    return inbox


@pytest.fixture
def running_imap_server(tmp_path, monkeypatch):
    """Boot `nobox serve-imap` in a subprocess, yield (port, inbox)."""
    # Seed SQLite *before* spawning so the server reads the inbox on connect.
    # The conftest fixture already points XDG_* at tmp_path, so seeding here
    # writes into the same DB the subprocess will read.
    inbox = _seed_state()
    port = _free_port()

    env = {
        **os.environ,
        # Force the subprocess into the same isolated XDG dirs the test uses.
        "XDG_CONFIG_HOME": os.environ["XDG_CONFIG_HOME"],
        "XDG_DATA_HOME": os.environ["XDG_DATA_HOME"],
        "XDG_STATE_HOME": os.environ["XDG_STATE_HOME"],
        "XDG_CACHE_HOME": os.environ["XDG_CACHE_HOME"],
        # Fake token so resolve_token doesn't fall back to `gh auth token`.
        "GH_TOKEN": "fake-token-for-tests",
        # Don't hit real GitHub during the test — serve from local SQLite.
        "NOBOX_OFFLINE": "1",
    }

    proc = subprocess.Popen(
        [sys.executable, "-m", "nobox", "serve-imap", "--port", str(port)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        _wait_for_port(port)
        yield port, inbox
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_login_select_fetch_over_real_tcp(running_imap_server):
    port, inbox = running_imap_server

    m = imaplib.IMAP4("127.0.0.1", port)
    try:
        typ, _ = m.login(inbox.name, inbox.imap_password)
        assert typ == "OK", "LOGIN should succeed with the inbox password"

        typ, data = m.list()
        assert typ == "OK"
        names = b" ".join(data).decode()
        for folder in ("INBOX", "Sent", "Drafts", "Trash"):
            assert folder in names, f"missing folder {folder!r} in LIST output"

        typ, _ = m.select("INBOX")
        assert typ == "OK"

        typ, data = m.uid("FETCH", "1:*", "(BODY[TEXT])")
        assert typ == "OK"
        # imaplib returns a list of tuples / bytes. Flatten and look for our body.
        flat = b"".join(part if isinstance(part, bytes) else b"".join(part) for part in data)
        assert b"hello over IMAP" in flat
    finally:
        m.logout()


def test_login_with_bad_password_is_rejected(running_imap_server):
    port, inbox = running_imap_server
    m = imaplib.IMAP4("127.0.0.1", port)
    try:
        with pytest.raises(imaplib.IMAP4.error):
            m.login(inbox.name, "wrong-password")
    finally:
        with contextlib.suppress(imaplib.IMAP4.error):
            m.logout()


def test_uid_store_persists_seen_flag(running_imap_server):
    port, inbox = running_imap_server

    m = imaplib.IMAP4("127.0.0.1", port)
    try:
        m.login(inbox.name, inbox.imap_password)
        m.select("INBOX")
        typ, _ = m.uid("STORE", "901", "+FLAGS", r"(\Seen)")
        assert typ == "OK"
    finally:
        m.logout()

    # Confirm the flag was persisted in SQLite — outside the IMAP session.
    with state.connect() as conn:
        row = state.get_message(conn, 901)
    assert row is not None
    assert "\\Seen" in row.flags


def test_append_to_drafts_persists_locally(running_imap_server):
    port, inbox = running_imap_server

    raw = (
        b"From: me@nobox.local\r\n"
        b"To: agent@nobox.local\r\n"
        b"Subject: a draft\r\n\r\n"
        b"this is a draft body\r\n"
    )

    m = imaplib.IMAP4("127.0.0.1", port)
    try:
        m.login(inbox.name, inbox.imap_password)
        # Twisted IMAP4Server crashes on a None flag list; always pass one.
        typ, _ = m.append(
            "Drafts", r"(\Draft)", imaplib.Time2Internaldate(time.time()), raw
        )
        assert typ == "OK"
    finally:
        m.logout()

    with state.connect() as conn:
        drafts = state.list_drafts(conn, inbox.name)
    assert len(drafts) == 1
    assert "this is a draft body" in drafts[0].body
