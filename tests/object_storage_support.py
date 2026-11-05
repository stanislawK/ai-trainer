"""A small S3 server on 127.0.0.1 for the object storage tests (ADR-0033, ticket #420).

CI has no S3 service. This serves what the adapter sends, and refuses a presigned link, GET or PUT
(#460), once the time the test set passes its `X-Amz-Date` + `X-Amz-Expires`, so no test sleeps or
reads the wall clock.
It does not verify signatures: `make storage-check` against versitygw does that.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen

ACCESS_KEY_ID = "AKIATESTACCESSKEY"
SECRET_ACCESS_KEY = "test-secret-access-key"
BUCKET = "test-uploads"


@dataclass
class S3State:
    objects: dict[str, bytes] = field(default_factory=dict)
    content_types: dict[str, str] = field(default_factory=dict)
    authorizations: list[str] = field(default_factory=list)
    # Seconds past a link's own signing time that count as "now".
    seconds_after_signing: int = 0
    # Answer every request with this status, its body naming the access key as some services do.
    refuse_with: int | None = None
    # Answer a GET with bytes that aren't HTTP, as a broken proxy might.
    answer_garbage: bool = False
    # GETs served, so a test can tell `size` read no bytes.
    gets: int = 0
    endpoint: str = ""


def _refusal(code: str) -> bytes:
    return (
        f"<Error><Code>{code}</Code><Message>credential {ACCESS_KEY_ID} refused</Message></Error>"
    ).encode()


def _handler(state: S3State) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def _send(self, status: int, body: bytes = b"") -> None:
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _key(self) -> str:
            return urlsplit(self.path).path.removeprefix(f"/{BUCKET}/")

        def _refused(self) -> bool:
            if state.refuse_with is None:
                return False
            self._send(state.refuse_with, _refusal("Refused"))
            return True

        def _expired(self) -> bool:
            query = parse_qs(urlsplit(self.path).query)
            if "X-Amz-Expires" in query and state.seconds_after_signing > int(
                query["X-Amz-Expires"][0]
            ):
                self._send(403, _refusal("AccessDenied"))
                return True
            return False

        def do_PUT(self) -> None:
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            state.authorizations.append(self.headers.get("Authorization", ""))
            if self._refused() or self._expired():
                return
            state.objects[self._key()] = body
            state.content_types[self._key()] = self.headers.get("Content-Type", "")
            self.send_response(200)
            self.send_header("ETag", '"probe"')
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self) -> None:
            if state.answer_garbage:
                self.wfile.write(b"garbage not http\r\n\r\n")
                return
            if self._refused() or self._expired():
                return
            state.gets += 1
            body = state.objects.get(self._key())
            if body is None:
                self._send(404, _refusal("NoSuchKey"))
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("ETag", '"probe"')
            self.send_header("Last-Modified", "Mon, 02 Nov 2026 10:00:00 GMT")
            self.end_headers()
            self.wfile.write(body)

        def do_HEAD(self) -> None:
            # A HEAD answer has headers only, whatever its status.
            status = state.refuse_with
            body = state.objects.get(self._key())
            if status is None:
                status = 404 if body is None else 200
            self.send_response(status)
            self.send_header("Content-Length", str(len(body or b"")))
            self.send_header("ETag", '"probe"')
            self.send_header("Last-Modified", "Mon, 02 Nov 2026 10:00:00 GMT")
            self.end_headers()

        def do_DELETE(self) -> None:
            state.authorizations.append(self.headers.get("Authorization", ""))
            if self._refused():
                return
            state.objects.pop(self._key(), None)
            self._send(204)

    return Handler


@contextmanager
def s3_server() -> Iterator[S3State]:
    state = S3State()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(state))
    state.endpoint = f"http://127.0.0.1:{server.server_port}"
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def unused_endpoint() -> str:
    """An address nothing listens on: the server is closed before this returns."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    port = server.server_port
    server.server_close()
    return f"http://127.0.0.1:{port}"


def put_via_link(link: str, data: bytes) -> int:
    """PUTs `data` to a presigned link as `curl -T` would; the status it answers with."""
    request = Request(link, data=data, method="PUT")
    try:
        with urlopen(request, timeout=5) as response:
            return int(response.status)
    except HTTPError as exc:
        exc.close()
        return exc.code
