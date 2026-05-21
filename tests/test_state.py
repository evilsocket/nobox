
from nobox import state
from nobox.state import InboxRow, MessageRow


def _sample_inbox(name="daily") -> InboxRow:
    return InboxRow(
        name=name,
        kind="issue",
        repo="me/test",
        issue_number=1,
        gist_id=None,
        user_login="me",
        created_at="2026-05-21T13:00:00Z",
        uid_validity=1234,
        pgp_enabled=False,
        imap_password="hunter2",
    )


def _sample_message(comment_id=100, inbox="daily") -> MessageRow:
    return MessageRow(
        comment_id=comment_id,
        inbox_name=inbox,
        direction="in",
        author_login="someone",
        created_at="2026-05-21T14:00:00Z",
        updated_at="2026-05-21T14:00:00Z",
        body_raw="hi",
        body_trimmed="hi",
        body_decrypted=None,
        subject="Re: daily",
        in_reply_to_id=None,
    )


def test_inbox_crud():
    with state.connect() as conn:
        state.insert_inbox(conn, _sample_inbox())
        rows = state.list_inboxes(conn)
        assert len(rows) == 1
        assert rows[0].name == "daily"
        row = state.get_inbox(conn, "daily")
        assert row is not None
        assert row.repo == "me/test"
        state.delete_inbox(conn, "daily")
        assert state.get_inbox(conn, "daily") is None


def test_message_upsert_and_flags():
    with state.connect() as conn:
        state.insert_inbox(conn, _sample_inbox())
        state.upsert_message(conn, _sample_message())
        m = state.get_message(conn, 100)
        assert m is not None
        assert m.flags == []
        state.add_flag(conn, 100, "\\Seen")
        m = state.get_message(conn, 100)
        assert "\\Seen" in m.flags
        state.remove_flag(conn, 100, "\\Seen")
        m = state.get_message(conn, 100)
        assert "\\Seen" not in m.flags


def test_etag_cache():
    with state.connect() as conn:
        assert state.get_etag(conn, "foo") == (None, None)
        state.set_etag(conn, "foo", "abc", "Mon, 01 Jan 2026 00:00:00 GMT")
        e, lm = state.get_etag(conn, "foo")
        assert e == "abc"
        assert lm == "Mon, 01 Jan 2026 00:00:00 GMT"


def test_list_messages_filters():
    with state.connect() as conn:
        state.insert_inbox(conn, _sample_inbox())
        m1 = _sample_message(comment_id=1)
        m1.flags = ["\\Seen"]
        m2 = _sample_message(comment_id=2)
        state.upsert_message(conn, m1)
        state.upsert_message(conn, m2)
        all_msgs = state.list_messages(conn, "daily")
        assert len(all_msgs) == 2
        unread = state.list_messages(conn, "daily", unread_only=True)
        assert len(unread) == 1
        assert unread[0].comment_id == 2
