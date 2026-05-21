"""Thin httpx-based GitHub REST client.

Implements only the endpoints nobox needs, with explicit control over
ETag conditional requests and X-RateLimit-Remaining backoff. Sync —
the volume is low and the rest of the codebase is sync.
"""

from __future__ import annotations

import random
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import httpx

from nobox import state

API_BASE = "https://api.github.com"
GRAPHQL_URL = "https://api.github.com/graphql"
ACCEPT = "application/vnd.github+json"
API_VERSION = "2022-11-28"


@dataclass
class Page:
    status: int
    data: Any
    etag: str | None
    last_modified: str | None
    next_url: str | None
    rate_remaining: int | None


class GitHubError(RuntimeError):
    def __init__(self, status: int, body: Any):
        super().__init__(f"GitHub API error {status}: {body}")
        self.status = status
        self.body = body


class GitHubClient:
    def __init__(self, token: str, *, timeout: float = 30.0):
        self._token = token
        self._client = httpx.Client(
            base_url=API_BASE,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": ACCEPT,
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": "nobox/0.1",
            },
            timeout=timeout,
            follow_redirects=True,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ---------- low-level -------------------------------------------------

    def _request(
        self,
        method: str,
        url: str,
        *,
        json: Any = None,
        params: dict | None = None,
        extra_headers: dict | None = None,
    ) -> httpx.Response:
        # Rate-limit backoff: if we're close to zero, wait until reset.
        headers = dict(extra_headers or {})
        for _ in range(5):
            r = self._client.request(method, url, json=json, params=params, headers=headers)
            remaining = _int_header(r, "X-RateLimit-Remaining")
            reset = _int_header(r, "X-RateLimit-Reset")
            if r.status_code in (403, 429) and remaining == 0 and reset:
                sleep_for = max(1, reset - int(time.time())) + random.uniform(0.5, 1.5)
                time.sleep(sleep_for)
                continue
            return r
        return r  # last attempt

    def _get_with_etag(
        self,
        endpoint_key: str,
        url: str,
        *,
        params: dict | None = None,
    ) -> Page:
        """GET with ETag conditional. 304 returns Page(status=304, data=None).

        endpoint_key is used as the cache key for the etag store; pass a stable
        string per logical endpoint (e.g. "issue-comments:owner/repo:42:page1").
        """
        with state.connect() as conn:
            etag, last_modified = state.get_etag(conn, endpoint_key)
        headers: dict[str, str] = {}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified

        r = self._request("GET", url, params=params, extra_headers=headers)

        if r.status_code == 304:
            return Page(
                status=304,
                data=None,
                etag=etag,
                last_modified=last_modified,
                next_url=_next_link(r),
                rate_remaining=_int_header(r, "X-RateLimit-Remaining"),
            )
        if 200 <= r.status_code < 300:
            new_etag = r.headers.get("ETag")
            new_last_modified = r.headers.get("Last-Modified")
            if new_etag or new_last_modified:
                with state.connect() as conn:
                    state.set_etag(conn, endpoint_key, new_etag, new_last_modified)
            return Page(
                status=r.status_code,
                data=r.json(),
                etag=new_etag,
                last_modified=new_last_modified,
                next_url=_next_link(r),
                rate_remaining=_int_header(r, "X-RateLimit-Remaining"),
            )
        raise GitHubError(r.status_code, _safe_json(r))

    def _post(self, url: str, json: Any) -> Any:
        r = self._request("POST", url, json=json)
        if 200 <= r.status_code < 300:
            return r.json()
        raise GitHubError(r.status_code, _safe_json(r))

    def _patch(self, url: str, json: Any) -> Any:
        r = self._request("PATCH", url, json=json)
        if 200 <= r.status_code < 300:
            return r.json()
        raise GitHubError(r.status_code, _safe_json(r))

    def _delete(self, url: str) -> None:
        r = self._request("DELETE", url)
        if r.status_code not in (200, 204):
            raise GitHubError(r.status_code, _safe_json(r))

    def _get(self, url: str, params: dict | None = None) -> Any:
        r = self._request("GET", url, params=params)
        if 200 <= r.status_code < 300:
            return r.json()
        raise GitHubError(r.status_code, _safe_json(r))

    # ---------- public endpoints -----------------------------------------

    def get_authenticated_user(self) -> dict:
        return self._get("/user")

    def get_repo(self, owner: str, repo: str) -> dict:
        return self._get(f"/repos/{owner}/{repo}")

    def get_rate_limit(self) -> dict:
        return self._get("/rate_limit")

    def create_issue(self, owner: str, repo: str, *, title: str, body: str) -> dict:
        return self._post(f"/repos/{owner}/{repo}/issues", {"title": title, "body": body})

    def update_issue(self, owner: str, repo: str, number: int, **fields) -> dict:
        return self._patch(f"/repos/{owner}/{repo}/issues/{number}", fields)

    def get_issue(self, owner: str, repo: str, number: int) -> dict:
        return self._get(f"/repos/{owner}/{repo}/issues/{number}")

    def list_issue_comments(
        self,
        owner: str,
        repo: str,
        number: int,
        *,
        since: str | None = None,
        per_page: int = 100,
    ) -> Iterator[Page]:
        """Yield pages of comments, following Link rel=next."""
        url = f"/repos/{owner}/{repo}/issues/{number}/comments"
        params: dict[str, Any] = {"per_page": per_page}
        if since:
            params["since"] = since
        page_idx = 0
        next_url: str | None = url
        while next_url is not None:
            key = f"issue-comments:{owner}/{repo}:{number}:p{page_idx}:{since or ''}"
            page = self._get_with_etag(
                key, next_url, params=params if page_idx == 0 else None
            )
            yield page
            next_url = page.next_url
            page_idx += 1

    def post_issue_comment(
        self, owner: str, repo: str, number: int, body: str
    ) -> dict:
        return self._post(
            f"/repos/{owner}/{repo}/issues/{number}/comments", {"body": body}
        )

    def edit_issue_comment(
        self, owner: str, repo: str, comment_id: int, body: str
    ) -> dict:
        return self._patch(
            f"/repos/{owner}/{repo}/issues/comments/{comment_id}", {"body": body}
        )

    def delete_issue_comment(self, owner: str, repo: str, comment_id: int) -> None:
        self._delete(f"/repos/{owner}/{repo}/issues/comments/{comment_id}")

    def list_all_issue_comments(
        self, owner: str, repo: str, number: int, *, per_page: int = 100
    ) -> list[dict]:
        """Fully paginate comments, bypassing the ETag cache (we need fresh state)."""
        out: list[dict] = []
        url: str | None = f"/repos/{owner}/{repo}/issues/{number}/comments"
        params: dict[str, Any] | None = {"per_page": per_page}
        while url is not None:
            r = self._request("GET", url, params=params)
            if r.status_code != 200:
                raise GitHubError(r.status_code, _safe_json(r))
            out.extend(r.json())
            url = _next_link(r)
            params = None  # subsequent URLs from Link already carry params
        return out

    def delete_issue_via_graphql(self, issue_node_id: str) -> None:
        """Try the GraphQL deleteIssue mutation (repo-admin only).

        Raises GitHubError on permission errors so callers can fall back to
        wipe-and-close.
        """
        query = (
            "mutation($id: ID!) {"
            "  deleteIssue(input: {issueId: $id}) {"
            "    repository { id }"
            "  }"
            "}"
        )
        self.graphql(query, {"id": issue_node_id})

    def graphql(self, query: str, variables: dict | None = None) -> dict:
        r = self._client.request(
            "POST",
            GRAPHQL_URL,
            json={"query": query, "variables": variables or {}},
        )
        body = _safe_json(r)
        if r.status_code >= 300 or (isinstance(body, dict) and body.get("errors")):
            raise GitHubError(
                r.status_code if r.status_code >= 300 else 422,
                body if isinstance(body, dict) else {"text": body},
            )
        return body.get("data", {}) if isinstance(body, dict) else {}

# ---------- header helpers ------------------------------------------------


def _next_link(r: httpx.Response) -> str | None:
    """Parse RFC-5988 Link header for rel=next, return the URL or None."""
    link = r.headers.get("Link")
    if not link:
        return None
    for part in link.split(","):
        section = part.strip().split(";")
        if len(section) < 2:
            continue
        url = section[0].strip().lstrip("<").rstrip(">")
        rel = next(
            (s.strip() for s in section[1:] if s.strip().startswith("rel=")), ""
        )
        if rel.endswith('"next"') or rel == "rel=next":
            return url
    return None


def _int_header(r: httpx.Response, name: str) -> int | None:
    v = r.headers.get(name)
    if v is None:
        return None
    try:
        return int(v)
    except ValueError:
        return None


def _safe_json(r: httpx.Response) -> Any:
    try:
        return r.json()
    except Exception:
        return r.text
