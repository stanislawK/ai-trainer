"""The image adapter (ADR-0031 *Picture generation*, #533): OpenRouter's `POST /api/v1/images`
over a mocked transport, one `llm_calls` row per call with the response's own cost and no
prompt text, and every failure a returned value, never an exception (ADR-0007, ADR-0018)."""

import asyncio
import base64
import json
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import httpx2
import pytest

from ai_trainer.domain.llm_calls import LlmCall, LlmCallOutcome, NewLlmCall
from ai_trainer.domain.media_generation import GeneratedImage, ImageGenerationFailed, ImageMediaType
from ai_trainer.llm.images import (
    IMAGES_URL,
    PICTURE_TEMPLATE_ID,
    PICTURE_TEMPLATE_VERSION,
    OpenRouterImageGenerator,
)

_ADMIN = UUID("00000000-0000-4000-8000-00000000a533")
_AT = datetime(2026, 11, 8, tzinfo=UTC)
_MODEL = "google/gemini-3.1-flash-lite-image"
_PROMPT = "One person hanging from a bar, then chest at the bar. Plain background. No text."
_JPEG = b"\xff\xd8\xff\xe0fake-jpeg"
_PNG = b"\x89PNG\r\n\x1a\nfake-png"
_WEBP = b"RIFF\x10\x00\x00\x00WEBPVP8 fake-webp"


class _Calls:
    def __init__(self) -> None:
        self.recorded: list[NewLlmCall] = []

    async def record(self, call: NewLlmCall) -> LlmCall:
        self.recorded.append(call)
        return LlmCall(id=uuid4(), created_at=_AT, **call.model_dump())

    async def list_for_user(self, user_id: UUID) -> list[LlmCall]:
        raise AssertionError("not read here")


def ok_response(
    *, data: bytes = _JPEG, media_type: str | None = "image/jpeg", cost: float | None = 0.0336
) -> httpx2.Response:
    item: dict[str, object] = {"b64_json": base64.b64encode(data).decode()}
    if media_type is not None:
        item["media_type"] = media_type
    usage: dict[str, object] = {
        "prompt_tokens": 40,
        "completion_tokens": 1290,
        "total_tokens": 1330,
    }
    if cost is not None:
        usage["cost"] = cost
    return httpx2.Response(200, json={"created": 1, "data": [item], "usage": usage})


def _generator(
    handler: Callable[[httpx2.Request], httpx2.Response],
    calls: _Calls,
    *,
    timeout_seconds: float = 5.0,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> OpenRouterImageGenerator:
    return OpenRouterImageGenerator(
        api_key="sk-or-v1-test",
        model=_MODEL,
        timeout_seconds=timeout_seconds,
        calls_repository=calls,
        http_client=httpx2.AsyncClient(transport=transport or httpx2.MockTransport(handler)),
    )


async def test_success_records_model_cost_and_template_without_prompt() -> None:
    """AC: the accounting row holds model, cost and template id, and no prompt text."""
    calls = _Calls()

    result = await _generator(lambda request: ok_response(), calls).generate(
        user_id=_ADMIN, prompt=_PROMPT
    )

    assert result == GeneratedImage(data=_JPEG, media_type="image/jpeg")
    [row] = calls.recorded
    assert row.user_id == _ADMIN
    assert row.model == _MODEL
    assert row.cost == Decimal("0.0336")
    assert row.template_id == PICTURE_TEMPLATE_ID == "media.picture"
    assert row.template_version == PICTURE_TEMPLATE_VERSION == 1
    assert (row.input_tokens, row.output_tokens) == (40, 1290)
    assert row.outcome is LlmCallOutcome.SUCCESS
    assert _PROMPT not in row.model_dump_json()
    assert "Plain background" not in row.model_dump_json()


async def test_the_request_names_the_model_and_prompt_and_nothing_about_the_admin() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return ok_response()

    await _generator(handler, _Calls()).generate(user_id=_ADMIN, prompt=_PROMPT)

    [request] = seen
    assert str(request.url) == IMAGES_URL == "https://openrouter.ai/api/v1/images"
    assert request.method == "POST"
    assert request.headers["authorization"] == "Bearer sk-or-v1-test"
    assert json.loads(request.content) == {"model": _MODEL, "prompt": _PROMPT}


async def test_a_png_is_returned_as_png() -> None:
    result = await _generator(
        lambda request: ok_response(media_type="image/png"), _Calls()
    ).generate(user_id=_ADMIN, prompt=_PROMPT)

    assert result == GeneratedImage(data=_JPEG, media_type="image/png")


async def test_a_webp_is_returned_as_webp() -> None:
    result = await _generator(
        lambda request: ok_response(data=_WEBP, media_type="image/webp"), _Calls()
    ).generate(user_id=_ADMIN, prompt=_PROMPT)

    assert result == GeneratedImage(data=_WEBP, media_type="image/webp")


@pytest.mark.parametrize(
    ("data", "media_type"),
    [(_JPEG, "image/jpeg"), (_PNG, "image/png"), (_WEBP, "image/webp")],
    ids=["jpeg", "png", "webp"],
)
async def test_a_missing_type_is_read_from_the_bytes(
    data: bytes, media_type: ImageMediaType
) -> None:
    """OpenRouter documents `media_type` as optional, so its absence isn't an error."""
    calls = _Calls()

    result = await _generator(
        lambda request: ok_response(data=data, media_type=None), calls
    ).generate(user_id=_ADMIN, prompt=_PROMPT)

    assert result == GeneratedImage(data=data, media_type=media_type)
    [row] = calls.recorded
    assert row.outcome is LlmCallOutcome.SUCCESS


async def test_an_undetectable_type_is_an_error_row_that_keeps_its_cost() -> None:
    calls = _Calls()

    result = await _generator(
        lambda request: ok_response(data=b"GIF89a-not-allowed", media_type=None), calls
    ).generate(user_id=_ADMIN, prompt=_PROMPT)

    assert result == ImageGenerationFailed(reason="error")
    [row] = calls.recorded
    assert row.outcome is LlmCallOutcome.ERROR
    assert row.cost == Decimal("0.0336")


async def test_a_missing_cost_is_recorded_as_null() -> None:
    calls = _Calls()

    await _generator(lambda request: ok_response(cost=None), calls).generate(
        user_id=_ADMIN, prompt=_PROMPT
    )

    [row] = calls.recorded
    assert row.cost is None
    assert row.outcome is LlmCallOutcome.SUCCESS


async def test_http_error_is_an_error_row_not_an_exception() -> None:
    calls = _Calls()

    result = await _generator(
        lambda request: httpx2.Response(502, json={"error": {"message": _PROMPT}}), calls
    ).generate(user_id=_ADMIN, prompt=_PROMPT)

    assert result == ImageGenerationFailed(reason="error")
    [row] = calls.recorded
    assert row.outcome is LlmCallOutcome.ERROR
    assert row.model == _MODEL
    assert row.cost is None


async def test_malformed_json_no_image_or_an_unknown_type_is_an_error_row() -> None:
    replies = [
        httpx2.Response(200, content=b"not json"),
        httpx2.Response(200, json={"data": [], "usage": {"cost": 0.0336}}),
        ok_response(media_type="image/svg+xml"),
        ok_response(data=b"<svg/>", media_type=None),
        httpx2.Response(200, json={"data": [{"b64_json": "%%%", "media_type": "image/jpeg"}]}),
    ]
    for reply in replies:
        calls = _Calls()

        def answer(request: httpx2.Request, reply: httpx2.Response = reply) -> httpx2.Response:
            return reply

        result = await _generator(answer, calls).generate(user_id=_ADMIN, prompt=_PROMPT)

        assert result == ImageGenerationFailed(reason="error")
        [row] = calls.recorded
        assert row.outcome is LlmCallOutcome.ERROR


async def test_a_billed_reply_with_no_image_keeps_its_cost() -> None:
    calls = _Calls()

    await _generator(
        lambda request: httpx2.Response(200, json={"data": [], "usage": {"cost": 0.0336}}), calls
    ).generate(user_id=_ADMIN, prompt=_PROMPT)

    [row] = calls.recorded
    assert row.cost == Decimal("0.0336")


async def test_a_raising_transport_is_an_error_row() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("no route", request=request)

    calls = _Calls()

    result = await _generator(handler, calls).generate(user_id=_ADMIN, prompt=_PROMPT)

    assert result == ImageGenerationFailed(reason="error")
    assert [row.outcome for row in calls.recorded] == [LlmCallOutcome.ERROR]


class _SlowTransport(httpx2.AsyncBaseTransport):
    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        await asyncio.sleep(10)
        return ok_response()  # pragma: no cover -- the adapter's timeout cancels the wait


async def test_timeout_is_a_timeout_row() -> None:
    calls = _Calls()

    result = await _generator(
        lambda request: ok_response(), calls, timeout_seconds=0.01, transport=_SlowTransport()
    ).generate(user_id=_ADMIN, prompt=_PROMPT)

    assert result == ImageGenerationFailed(reason="timeout")
    [row] = calls.recorded
    assert row.outcome is LlmCallOutcome.TIMEOUT
    assert row.cost is None
