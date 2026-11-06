"""The token client for the knowledge API (ADR-0036, tickets #459 and #464).

- `list` prints, as one JSON array, what production's knowledge base already holds: title,
  status and the identifiers `/research-knowledge` matches candidates on.
- `stage-pmc <manifest.json>` stages papers by PMC ID as `proposed` sources, 20 to a request, and
  prints each ID's result (`staged`, `covered`, `refused`, `not_found`, `unavailable`).
- `stage-file <template.json> <file>` stages one file's metadata, sends the file's bytes from disk
  to the presigned link the app issued (never through the app, a model or an argument), and
  registers the upload. It prints the register outcome.

It reads `KNOWLEDGE_API_URL` and `KNOWLEDGE_API_TOKEN` from the environment and sends the token
only in the `Authorization` header of calls to the app, never to the upload link. The client has
no command beyond listing and staging, which is all a token may do (ADR-0036 invariant 2).

Exit codes: 0 done, 1 failed, 2 unknown command, 3 no token set (the skill skips the check).
Every message is built from a status code or an exception class, never from a request, a header,
an upload link or an exception's text, so neither the token nor a link can reach stdout or stderr.
"""

import argparse
import json
import os
import sys
from collections.abc import Sequence
from http.client import HTTPException
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

SOURCES_PATH = "/api/admin/knowledge/sources"
STAGE_PMC_PATH = "/api/admin/knowledge/stage/pmc"
STAGE_FILE_PATH = "/api/admin/knowledge/stage/file"
REGISTER_PATH = "/api/admin/knowledge/register"
PAGE_SIZE = 500
PMC_BATCH = 20  # the route's cap on articles in one request
TIMEOUT_SECONDS = 10
PUT_TIMEOUT_SECONDS = 300  # a book over a slow uplink; the link lives for an hour
DETAIL_LIMIT = 500
DEV_STORAGE_HOST = "storage"  # what `make start`'s links name; the host reaches it on loopback
EXIT_FAILED = 1
EXIT_NO_TOKEN = 3
KEPT_FIELDS = ("title", "status", "pmcid", "doi", "pmid", "isbn", "source_url")
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
HTTP_HINTS = {
    401: " (the token is refused or expired)",
    403: " (the token is refused or expired)",
    429: " (rate limited: try again later)",
}
COMMAND_ARITY = {"list": 0, "stage-pmc": 1, "stage-file": 2}
UNFINISHED = frozenset({"missing", "expired"})


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


def _error_detail(error: HTTPError) -> str:
    """The app's own words for a 422, so a refused template names its field."""
    try:
        detail = json.loads(error.read()).get("detail")
    except ValueError, AttributeError, OSError, HTTPException:
        return ""
    if detail is None:
        return ""
    text = detail if isinstance(detail, str) else json.dumps(detail)
    return f": {text[:DETAIL_LIMIT]}"


def _open(request: Request, *, noun: str, timeout: float = TIMEOUT_SECONDS) -> bytes:
    try:
        with urlopen(request, timeout=timeout) as response:
            body: bytes = response.read()
            return body
    except HTTPError as error:
        detail = _error_detail(error) if error.code == 422 else ""
        error.close()
        hint = HTTP_HINTS.get(error.code, "")
        raise ClientError(f"{noun} answered HTTP {error.code}{hint}{detail}") from None
    except (URLError, OSError, ValueError, HTTPException) as error:
        raise ClientError(f"{noun} was unreachable ({type(error).__name__})") from None


def fetch_json(request: Request) -> dict[str, Any]:
    body = _open(request, noun="the knowledge API")
    try:
        page = json.loads(body)
    except ValueError:
        raise ClientError("the knowledge API did not answer with JSON") from None
    if not isinstance(page, dict):
        raise ClientError("the knowledge API answered with an unexpected shape")
    return page


def fetch_page(request: Request) -> dict[str, Any]:
    page = fetch_json(request)
    if not isinstance(page.get("sources"), list):
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


def _read_json(path: str, what: str) -> object:
    name = Path(path).name
    try:
        text = Path(path).read_text()
    except OSError:
        raise ClientError(f"cannot read {name}") from None
    try:
        return json.loads(text)
    except ValueError:
        raise ClientError(f"the {what} {name} is not valid JSON") from None


def load_manifest(path: str) -> list[dict[str, Any]]:
    data = _read_json(path, "manifest")
    articles = data.get("articles") if isinstance(data, dict) else None
    if not isinstance(articles, list) or not articles:
        raise ClientError('the manifest must be {"articles": [ ... ]} with at least one article')
    if not all(isinstance(a, dict) and isinstance(a.get("pmcid"), str) for a in articles):
        raise ClientError("every article in the manifest needs a pmcid")
    return articles


def post_json(base_url: str, token: str, path: str, body: dict[str, Any]) -> dict[str, Any]:
    request = Request(
        f"{base_url.rstrip('/')}{path}",
        data=json.dumps(body).encode(),
        method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    return fetch_json(request)


def stage_pmc(base_url: str, token: str, articles: list[dict[str, Any]]) -> dict[str, Any]:
    results: list[Any] = []
    for start in range(0, len(articles), PMC_BATCH):
        page = post_json(
            base_url, token, STAGE_PMC_PATH, {"articles": articles[start : start + PMC_BATCH]}
        )
        if not isinstance(page.get("results"), list):
            raise ClientError("the knowledge API answered with an unexpected shape")
        results.extend(page["results"])
    return {"results": results}


def put_target(put_url: str, base_url: str) -> tuple[str, str | None]:
    """The link to send to and the Host header to keep. Only https, except that a local app's
    `make start` link names its `storage` container, which the host reaches on loopback."""
    parts = urlsplit(put_url)
    if parts.scheme == "https" and parts.hostname:
        return put_url, None
    local = urlsplit(base_url).hostname in LOCAL_HOSTS
    if parts.scheme == "http" and parts.hostname == DEV_STORAGE_HOST and local:
        port = f":{parts.port}" if parts.port else ""
        return urlunsplit(parts._replace(netloc=f"127.0.0.1{port}")), parts.netloc
    raise ClientError("the upload link must be https")


def put_file(base_url: str, put_url: str, data: bytes) -> None:
    """Send the bytes to the presigned link. The link carries its own signature, so no
    `Authorization` header goes with it."""
    url, host = put_target(put_url, base_url)
    headers = {"Content-Type": "application/octet-stream"} | ({"Host": host} if host else {})
    request = Request(url, data=data, method="PUT", headers=headers)
    _open(request, noun="the upload link", timeout=PUT_TIMEOUT_SECONDS)


def _put_and_register(
    base_url: str, token: str, staged: dict[str, Any], data: bytes
) -> dict[str, Any]:
    """PUT the bytes, register; one more PUT when register found nothing yet."""
    put_url, upload_id = staged.get("put_url"), staged.get("upload_id")
    if not isinstance(put_url, str) or not isinstance(upload_id, str):
        raise ClientError("the knowledge API answered with an unexpected shape")
    report: dict[str, Any] = {}
    for _ in range(2):
        put_file(base_url, put_url, data)
        report = post_json(base_url, token, REGISTER_PATH, {"upload_id": upload_id})
        if report.get("outcome") != "missing":
            break
    return report


def stage_file(base_url: str, token: str, template: str, file: str) -> dict[str, Any]:
    source = _read_json(template, "template")
    if not isinstance(source, dict):
        raise ClientError("the template must be a JSON object")
    path = Path(file)
    try:
        data = path.read_bytes()
    except OSError:
        raise ClientError(f"cannot read {path.name}") from None
    report: dict[str, Any] = {}
    for _ in range(2):  # a second stage only when the first link expired unused
        body = {"file_name": path.name, "source": source}
        staged = post_json(base_url, token, STAGE_FILE_PATH, body)
        if staged.get("outcome") != "ready":
            return staged
        report = _put_and_register(base_url, token, staged, data)
        if report.get("outcome") != "expired":
            break
    return report


def run(command: str, paths: list[str], base_url: str, token: str) -> int:
    check_base_url(base_url)
    if command == "list":
        print(json.dumps(list_covered(base_url, token), indent=2))
        return 0
    if command == "stage-pmc":
        print(json.dumps(stage_pmc(base_url, token, load_manifest(paths[0])), indent=2))
        return 0
    report = stage_file(base_url, token, paths[0], paths[1])
    print(json.dumps(report, indent=2))
    return EXIT_FAILED if report.get("outcome") in UNFINISHED else 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Token client for the knowledge API.")
    parser.add_argument("command", choices=list(COMMAND_ARITY))
    parser.add_argument(
        "paths", nargs="*", help="stage-pmc <manifest>; stage-file <template> <file>"
    )
    args = parser.parse_args(argv)
    if len(args.paths) != COMMAND_ARITY[args.command]:
        parser.error(f"{args.command} takes {COMMAND_ARITY[args.command]} path(s)")
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
        return run(args.command, args.paths, base_url, token)
    except ClientError as error:
        print(f"knowledge_client: {error}", file=sys.stderr)
        return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
