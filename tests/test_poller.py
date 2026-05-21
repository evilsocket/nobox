"""Tests for poller.sync_inbox and pop_mode behaviour."""

from __future__ import annotations

import httpx
import respx

from nobox import poller, state
from nobox.github_client import GitHubClient
from nobox.state import InboxRow


def _insert_inbox(*, pop_mode: bool) -> InboxRow:
    row = InboxRow(
        name="daily",
        kind="issue",
        repo="me/test",
        issue_number=1,
        gist_id=None,
        user_login="me",
        created_at="2026-05-21T13:00:00Z",
        uid_validity=42,
        pgp_enabled=False,
        imap_password="pw",
        pop_mode=pop_mode,
    )
    with state.connect() as conn:
        state.insert_inbox(conn, row)
    return row


def _comment(comment_id: int, *, author: str = "alice", body: str = "hello") -> dict:
    return {
        "id": comment_id,
        "user": {"login": author},
        "body": body,
        "created_at": "2026-05-21T14:00:00Z",
        "updated_at": "2026-05-21T14:00:00Z",
    }


@respx.mock
def test_pop_mode_deletes_after_persist():
    row = _insert_inbox(pop_mode=True)

    respx.get("https://api.github.com/repos/me/test/issues/1/comments").mock(
        return_value=httpx.Response(200, json=[_comment(111), _comment(222)])
    )
    delete_111 = respx.delete(
        "https://api.github.com/repos/me/test/issues/comments/111"
    ).mock(return_value=httpx.Response(204))
    delete_222 = respx.delete(
        "https://api.github.com/repos/me/test/issues/comments/222"
    ).mock(return_value=httpx.Response(204))

    with GitHubClient("tok") as c:
        n = poller.sync_inbox(c, row)

    assert n == 2
    # Both comments must end up in SQLite
    with state.connect() as conn:
        assert state.get_message(conn, 111) is not None
        assert state.get_message(conn, 222) is not None
    # And both must have been DELETEd from GitHub
    assert delete_111.called
    assert delete_222.called


@respx.mock
def test_pop_mode_off_does_not_delete():
    row = _insert_inbox(pop_mode=False)

    respx.get("https://api.github.com/repos/me/test/issues/1/comments").mock(
        return_value=httpx.Response(200, json=[_comment(333)])
    )
    delete_route = respx.delete(
        "https://api.github.com/repos/me/test/issues/comments/333"
    ).mock(return_value=httpx.Response(204))

    with GitHubClient("tok") as c:
        n = poller.sync_inbox(c, row)

    assert n == 1
    with state.connect() as conn:
        assert state.get_message(conn, 333) is not None
    assert not delete_route.called


@respx.mock
def test_pop_swallows_404():
    """A 404 on delete (already gone) should not raise."""
    row = _insert_inbox(pop_mode=True)

    respx.get("https://api.github.com/repos/me/test/issues/1/comments").mock(
        return_value=httpx.Response(200, json=[_comment(444)])
    )
    respx.delete(
        "https://api.github.com/repos/me/test/issues/comments/444"
    ).mock(return_value=httpx.Response(404, json={"message": "Not Found"}))

    with GitHubClient("tok") as c:
        n = poller.sync_inbox(c, row)

    assert n == 1
    with state.connect() as conn:
        assert state.get_message(conn, 444) is not None


@respx.mock
def test_sentinel_comments_are_skipped_but_still_popped():
    """Doctor sentinels carry kind=sentinel meta. Poller drops them on ingest,
    but pop_mode still deletes them from GitHub."""
    row = _insert_inbox(pop_mode=True)

    sentinel_body = (
        "**nobox doctor sentinel** — if you received an email …\n\n"
        "<!-- nobox-meta\n"
        '{"v":1,"id":"abc","kind":"sentinel","direction":"system","ts":"2026-05-21T15:00:00Z"}\n'
        "-->\n"
    )
    respx.get("https://api.github.com/repos/me/test/issues/1/comments").mock(
        return_value=httpx.Response(
            200,
            json=[
                _comment(700, author="me", body=sentinel_body),
                _comment(701, author="alice", body="real message"),
            ],
        )
    )
    delete_sentinel = respx.delete(
        "https://api.github.com/repos/me/test/issues/comments/700"
    ).mock(return_value=httpx.Response(204))
    delete_real = respx.delete(
        "https://api.github.com/repos/me/test/issues/comments/701"
    ).mock(return_value=httpx.Response(204))

    with GitHubClient("tok") as c:
        n = poller.sync_inbox(c, row)

    assert n == 1  # only the real message counted
    with state.connect() as conn:
        assert state.get_message(conn, 700) is None  # sentinel skipped
        assert state.get_message(conn, 701) is not None  # real message kept
    assert delete_sentinel.called  # but pop still ran on both
    assert delete_real.called


@respx.mock
def test_pop_keeps_local_copy_when_delete_fails():
    """A 403 on delete should leave the local copy intact, not raise."""
    row = _insert_inbox(pop_mode=True)

    respx.get("https://api.github.com/repos/me/test/issues/1/comments").mock(
        return_value=httpx.Response(200, json=[_comment(555)])
    )
    respx.delete(
        "https://api.github.com/repos/me/test/issues/comments/555"
    ).mock(return_value=httpx.Response(403, json={"message": "Forbidden"}))

    with GitHubClient("tok") as c:
        n = poller.sync_inbox(c, row)

    assert n == 1
    with state.connect() as conn:
        assert state.get_message(conn, 555) is not None


