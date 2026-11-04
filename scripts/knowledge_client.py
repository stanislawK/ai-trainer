"""The token client for the knowledge API (ADR-0036, ticket #459).

`uv run python scripts/knowledge_client.py list` prints, as one JSON array, what production's
knowledge base already holds: title, status and the identifiers `/research-knowledge` matches
candidates on. It reads `KNOWLEDGE_API_URL` and `KNOWLEDGE_API_TOKEN` from the environment and
sends the token only in the `Authorization` header.

Exit codes: 0 done, 1 failed, 2 unknown command, 3 no token set (the skill skips the check).
Every message is built from a status code or an exception class, never from a request, a header
or an exception's text, so the token cannot reach stdout or stderr.
"""

import argparse
import json
import os
import sys
from collections.abc import Sequence
from http.client import HTTPException
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

SOURCES_PATH = "/api/admin/knowledge/sources"
PAGE_SIZE = 500
TIMEOUT_SECONDS = 10
EXIT_FAILED = 1
EXIT_NO_TOKEN = 3
KEPT_FIELDS = ("title", "status", "pmcid", "doi", "pmid", "isbn", "source_url")
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
HTTP_HINTS = {
    401: " (the token is refused or expired)",
    403: " (the token is refused or expired)",
    429: " (rate limited: try again later)",
}


class ClientError(Exception):
    """A failure whose message is safe to print."""


class _NoRedirect(HTTPRedirectHandler):
    """A redirect would carry the `Authorization` header to wherever it points, so none is
    followed: the 3xx surfaces as an `HTTPError`."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> Request | None:  # noqa: ANN401 -- the stdlib handler's own signature
        return None


_OPENER = build_opener(_NoRedirect)


def urlopen(request: Request, timeout: float) -> Any:  # noqa: ANN401 -- the stdlib response object
    return _OPENER.open(request, timeout=timeout)


def check_base_url(base_url: str) -> None:
    """The token travels in clear text over `http`, so only a local server may use it."""
    parts = urlsplit(base_url)
    if parts.scheme == "https" and parts.hostname:
        return
    if parts.scheme == "http" and parts.hostname in LOCAL_HOSTS:
        return
    raise ClientError("KNOWLEDGE_API_URL must be an https URL (http only for localhost)")


def sources_request(base_url: str, token: str, after: str | None) -> Request:
    query = {"limit": PAGE_SIZE} | ({"after": after} if after else {})
    url = f"{base_url.rstrip('/')}{SOURCES_PATH}?{urlencode(query)}"
    return Request(url, headers={"Authorization": f"Bearer {token}"})


def fetch_page(request: Request) -> dict[str, Any]:
    try:
        with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            body = response.read()
    except HTTPError as error:
        error.close()
        hint = HTTP_HINTS.get(error.code, "")
        raise ClientError(f"the knowledge API answered HTTP {error.code}{hint}") from None
    except (URLError, OSError, ValueError, HTTPException) as error:
        raise ClientError(f"the knowledge API was unreachable ({type(error).__name__})") from None
    try:
        page = json.loads(body)
    except ValueError:
        raise ClientError("the knowledge API did not answer with JSON") from None
    if not isinstance(page, dict) or not isinstance(page.get("sources"), list):
        raise ClientError("the knowledge API answered with an unexpected shape")
    return page


def list_covered(base_url: str, token: str) -> list[dict[str, Any]]:
    covered: list[dict[str, Any]] = []
    after: str | None = None
    cursors: set[str] = set()
    while True:
        page = fetch_page(sources_request(base_url, token, after))
        if not all(isinstance(source, dict) for source in page["sources"]):
            raise ClientError("the knowledge API answered with an unexpected shape")
        covered.extend(
            {field: source.get(field) for field in KEPT_FIELDS} for source in page["sources"]
        )
        after = page.get("next")
        if not after:
            return covered
        if after in cursors:
            raise ClientError("the knowledge API repeated a page cursor")
        cursors.add(after)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Token client for the knowledge API.")
    parser.add_argument("command", choices=["list"])
    parser.parse_args(argv)
    # A client script, not app code: the owner's shell is the source of both values.
    token = os.environ.get("KNOWLEDGE_API_TOKEN", "").strip()
    if not token:
        print("KNOWLEDGE_API_TOKEN is not set", file=sys.stderr)
        return EXIT_NO_TOKEN
    base_url = os.environ.get("KNOWLEDGE_API_URL", "").strip()
    if not base_url:
        print("KNOWLEDGE_API_URL is not set", file=sys.stderr)
        return EXIT_FAILED
    try:
        check_base_url(base_url)
        covered = list_covered(base_url, token)
    except ClientError as error:
        print(f"knowledge_client: {error}", file=sys.stderr)
        return EXIT_FAILED
    print(json.dumps(covered, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
