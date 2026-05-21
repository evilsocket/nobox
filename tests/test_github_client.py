
import httpx
import respx

from nobox.github_client import GitHubClient, _next_link


def test_next_link_parses_rel_next():
    r = httpx.Response(
        200,
        headers={
            "Link": '<https://api.github.com/x?page=2>; rel="next", <https://api.github.com/x?page=5>; rel="last"'
        },
    )
    assert _next_link(r) == "https://api.github.com/x?page=2"


def test_next_link_returns_none_when_absent():
    r = httpx.Response(200)
    assert _next_link(r) is None


@respx.mock
def test_get_authenticated_user():
    respx.get("https://api.github.com/user").mock(
        return_value=httpx.Response(200, json={"login": "evilsocket"})
    )
    with GitHubClient("token123") as c:
        u = c.get_authenticated_user()
    assert u["login"] == "evilsocket"


@respx.mock
def test_etag_304_is_cache_hit():
    """First GET returns 200 + ETag. Second GET sends If-None-Match,
    server returns 304; client returns Page(status=304, data=None)."""
    route = respx.get(
        "https://api.github.com/repos/me/test/issues/1/comments"
    ).mock(
        side_effect=[
            httpx.Response(
                200,
                headers={"ETag": '"abc"'},
                json=[{"id": 1, "user": {"login": "x"}, "body": "hi",
                       "created_at": "2026-05-21T00:00:00Z",
                       "updated_at": "2026-05-21T00:00:00Z"}],
            ),
            httpx.Response(304, headers={"ETag": '"abc"'}),
        ]
    )
    with GitHubClient("tok") as c:
        pages = list(c.list_issue_comments("me", "test", 1))
        assert len(pages) == 1
        assert pages[0].status == 200
        assert pages[0].etag == '"abc"'
        pages2 = list(c.list_issue_comments("me", "test", 1))
        assert pages2[0].status == 304
        # Second call's request should include If-None-Match
        assert route.calls[1].request.headers.get("If-None-Match") == '"abc"'


@respx.mock
def test_create_issue_post():
    respx.post(
        "https://api.github.com/repos/me/test/issues",
    ).mock(
        return_value=httpx.Response(
            201,
            json={"number": 42, "id": 9001},
        )
    )
    with GitHubClient("tok") as c:
        issue = c.create_issue("me", "test", title="t", body="b")
    assert issue["number"] == 42
