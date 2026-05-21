"""Tests for inbox.delete_inbox.

Default = full wipe (GitHub + local). Pass local_only=True to keep remote.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from nobox import inbox as inbox_mod
from nobox import state
from nobox.github_client import GitHubClient
from nobox.state import InboxRow


def _insert(*, name="daily") -> InboxRow:
    row = InboxRow(
        name=name,
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
    d = state.inbox_dir(name)
    (d / "imap.password").write_text("pw")
    return row


def test_local_only_removes_row_and_dir_without_touching_github():
    """local_only=True clears local state and does NOT call any GitHub API."""
    row = _insert()
    d = state.inbox_dir(row.name)
    assert d.exists()

    actions = inbox_mod.delete_inbox(row.name, local_only=True)
    assert actions["local"] is True
    assert actions["remote"] is None  # never called the API

    with state.connect() as conn:
        assert state.get_inbox(conn, row.name) is None
    assert not d.exists()


def test_local_only_delete_then_recreate_succeeds():
    _insert(name="foo")
    inbox_mod.delete_inbox("foo", local_only=True)
    with pytest.raises(inbox_mod.InboxError):
        inbox_mod.load_inbox("foo")


@respx.mock
def test_default_delete_drops_comments_then_graphql_delete():
    """Default (no flags): list comments → delete each → GraphQL deleteIssue."""
    row = _insert(name="itest")
    respx.get(
        "https://api.github.com/repos/me/test/issues/42/comments"
    ).mock(
        return_value=httpx.Response(200, json=[{"id": 1}, {"id": 2}, {"id": 3}])
    )
    del1 = respx.delete(
        "https://api.github.com/repos/me/test/issues/comments/1"
    ).mock(return_value=httpx.Response(204))
    del2 = respx.delete(
        "https://api.github.com/repos/me/test/issues/comments/2"
    ).mock(return_value=httpx.Response(204))
    del3 = respx.delete(
        "https://api.github.com/repos/me/test/issues/comments/3"
    ).mock(return_value=httpx.Response(204))
    respx.get("https://api.github.com/repos/me/test/issues/42").mock(
        return_value=httpx.Response(200, json={"node_id": "I_abc", "number": 42})
    )
    gql = respx.post("https://api.github.com/graphql").mock(
        return_value=httpx.Response(
            200, json={"data": {"deleteIssue": {"repository": {"id": "R_xyz"}}}}
        )
    )

    with GitHubClient("tok") as c:
        actions = inbox_mod.delete_inbox(row.name, client=c)

    assert del1.called and del2.called and del3.called
    assert gql.called
    assert "deleted issue me/test#42" in actions["remote"]
    assert "3 comments" in actions["remote"]
    with state.connect() as conn:
        assert state.get_inbox(conn, row.name) is None


@respx.mock
def test_default_delete_falls_back_to_wipe_close_on_graphql_403():
    """If GraphQL refuses (not repo admin), wipe title+body and close."""
    row = _insert(name="noperm")
    respx.get(
        "https://api.github.com/repos/me/test/issues/42/comments"
    ).mock(return_value=httpx.Response(200, json=[{"id": 7}]))
    respx.delete(
        "https://api.github.com/repos/me/test/issues/comments/7"
    ).mock(return_value=httpx.Response(204))
    respx.get("https://api.github.com/repos/me/test/issues/42").mock(
        return_value=httpx.Response(200, json={"node_id": "I_xyz", "number": 42})
    )
    respx.post("https://api.github.com/graphql").mock(
        return_value=httpx.Response(
            200,
            json={"errors": [{"type": "FORBIDDEN", "message": "no admin"}]},
        )
    )
    patch_route = respx.patch(
        "https://api.github.com/repos/me/test/issues/42"
    ).mock(
        return_value=httpx.Response(200, json={"state": "closed", "number": 42})
    )

    with GitHubClient("tok") as c:
        actions = inbox_mod.delete_inbox(row.name, client=c)

    assert patch_route.called
    body = patch_route.calls[0].request.read().decode()
    assert '"title":"(deleted)"' in body
    assert '"body":""' in body
    assert '"state":"closed"' in body
    assert "wiped+closed" in actions["remote"]
    assert "1 comments" in actions["remote"]


@respx.mock
def test_default_delete_issue_404_is_swallowed():
    """If the issue is already gone, list-comments returns 404 and we bail cleanly."""
    row = _insert(name="gone")
    respx.get(
        "https://api.github.com/repos/me/test/issues/42/comments"
    ).mock(return_value=httpx.Response(404, json={"message": "Not Found"}))
    with GitHubClient("tok") as c:
        actions = inbox_mod.delete_inbox(row.name, client=c)
    assert "already gone" in actions["remote"]
    with state.connect() as conn:
        assert state.get_inbox(conn, row.name) is None


def test_create_existing_error_message_points_at_delete():
    """The improved error tells the user how to recover."""
    _insert(name="dup")
    class _NoClient:
        def get_authenticated_user(self):  # pragma: no cover - unreached
            raise AssertionError("should not be called")

    with pytest.raises(inbox_mod.InboxError) as ei:
        inbox_mod.create_inbox(_NoClient(), name="dup", repo="me/test")
    assert "delete-inbox" in str(ei.value)
