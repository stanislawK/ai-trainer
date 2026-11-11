"""A fake wger.de replaying the responses recorded on 2026-11-11 in `tests/fixtures/wger/` (#552).

The pages are real, trimmed to four images over two pages: image 3 has no author, image 15
("Bench Press Narrow Grip") and image 724 ("Stomach Vacuum", flagged AI-generated) have one,
and image 19 ("Biceps Curl With Cable") is one a test leaves unmatched. The two image files are
tiny stand-ins under wger's own file names, since only their bytes matter."""

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import httpx2

FIXTURES = Path(__file__).parent / "fixtures" / "wger"
WGER = "https://wger.de"
BENCH_PRESS_FILE = FIXTURES / "Narrow-grip-bench-press-1.png"
VACUUM_FILE = FIXTURES / "e947b431-e4c4-48fa-bb21-de293734cce8.jpg"

type WgerFailure = Literal["server_error", "timeout", "malformed", "transport"]

_IMAGES = {
    "/media/exercise-images/88/Narrow-grip-bench-press-1.png": (BENCH_PRESS_FILE, "image/png"),
    "/media/exercise-images/2673/e947b431-e4c4-48fa-bb21-de293734cce8.jpg": (
        VACUUM_FILE,
        "image/jpeg",
    ),
}


@dataclass
class FakeWgerSite:
    """`failing` maps a request path (the image listing's second page is
    `/api/v2/exerciseimage/?offset=2`) to how it fails."""

    failing: dict[str, WgerFailure] = field(default_factory=dict)
    requests: list[httpx2.Request] = field(default_factory=list)
    timeout_delay: float = 5.0

    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self._handle)

    async def _handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        key = _key(request)
        failure = self.failing.get(key)
        if failure == "timeout":
            await asyncio.sleep(self.timeout_delay)
        if failure == "transport":
            raise httpx2.ConnectError("refused", request=request)
        if failure == "server_error":
            return httpx2.Response(503, request=request)
        if failure == "malformed":
            return httpx2.Response(200, content=b"<html>not json</html>", request=request)
        return _answer(request, key)


def _key(request: httpx2.Request) -> str:
    path = request.url.path
    offset = request.url.params.get("offset")
    return f"{path}?offset={offset}" if offset else path


def _json(name: str, request: httpx2.Request) -> httpx2.Response:
    return httpx2.Response(
        200,
        content=(FIXTURES / name).read_bytes(),
        headers={"content-type": "application/json"},
        request=request,
    )


def _answer(request: httpx2.Request, key: str) -> httpx2.Response:
    pages = {
        "/api/v2/license/": "license.json",
        "/api/v2/exerciseimage/": "exerciseimage-1.json",
        "/api/v2/exerciseimage/?offset=2": "exerciseimage-2.json",
        "/api/v2/exercise-translation/": "exercise-translation-1.json",
        "/api/v2/exercise-translation/?offset=2": "exercise-translation-2.json",
    }
    if key in pages:
        return _json(pages[key], request)
    if key in _IMAGES:
        path, media_type = _IMAGES[key]
        return httpx2.Response(
            200, content=path.read_bytes(), headers={"content-type": media_type}, request=request
        )
    return httpx2.Response(404, request=request)
