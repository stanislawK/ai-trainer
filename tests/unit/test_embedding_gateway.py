"""ADR-0007's single embeddings adapter: `POST /api/v1/embeddings` in batches, one `llm_calls`
row per call with OpenRouter's own cost, a 429 as `RateLimited`, every vector's length checked,
and nothing about the acting user sent (ADR-0018, ADR-0033, #423)."""

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import httpx2
import pytest

from ai_trainer.domain.embeddings import (
    Embedded,
    EmbeddingFailed,
    EmbeddingFailure,
    RateLimited,
)
from ai_trainer.domain.llm_calls import LlmCall, LlmCallOutcome, NewLlmCall, ProviderPreferences
from ai_trainer.llm.embeddings import EMBEDDING_TEMPLATE_ID, OpenRouterEmbeddingGateway

_ADMIN = UUID("00000000-0000-4000-8000-00000000a423")
_AT = datetime(2026, 11, 4, tzinfo=UTC)


class _Calls:
    def __init__(self) -> None:
        self.recorded: list[NewLlmCall] = []

    async def record(self, call: NewLlmCall) -> LlmCall:
        self.recorded.append(call)
        return LlmCall(id=uuid4(), created_at=_AT, **call.model_dump())

    async def list_for_user(self, user_id: UUID) -> list[LlmCall]:
        raise AssertionError("not read here")


def _vectors(count: int, dimensions: int = 4) -> list[dict[str, object]]:
    return [
        {"object": "embedding", "index": i, "embedding": [i / 10] * dimensions}
        for i in range(count)
    ]


def _ok(count: int, *, dimensions: int = 4, cost: float | None = 0.00002) -> httpx2.Response:
    usage: dict[str, object] = {"prompt_tokens": 17, "total_tokens": 17}
    if cost is not None:
        usage["cost"] = cost
    return httpx2.Response(
        200,
        json={
            "object": "list",
            "model": "qwen/qwen3-embedding-8b",
            "data": _vectors(count, dimensions),
            "usage": usage,
        },
    )


def _gateway(
    handler: Callable[[httpx2.Request], httpx2.Response],
    calls: _Calls,
    *,
    timeout_seconds: float = 5.0,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> OpenRouterEmbeddingGateway:
    return OpenRouterEmbeddingGateway(
        api_key="sk-or-v1-test",
        model="qwen/qwen3-embedding-8b",
        dimensions=4,
        provider=ProviderPreferences(only=["deepinfra"]),
        timeout_seconds=timeout_seconds,
        calls_repository=calls,
        http_client=httpx2.AsyncClient(transport=transport or httpx2.MockTransport(handler)),
    )


async def test_the_request_carries_only_model_inputs_dimensions_and_provider() -> None:
    sent: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        sent.append(request)
        return _ok(2)

    await _gateway(handler, _Calls()).embed_documents(["a", "b"], acting_user_id=_ADMIN)

    (request,) = sent
    assert request.method == "POST"
    assert str(request.url) == "https://openrouter.ai/api/v1/embeddings"
    assert request.headers["authorization"] == "Bearer sk-or-v1-test"
    assert json.loads(request.content) == {
        "model": "qwen/qwen3-embedding-8b",
        "input": ["a", "b"],
        "dimensions": 4,
        "encoding_format": "float",
        "provider": {"only": ["deepinfra"]},
    }
    assert str(_ADMIN) not in request.content.decode()


async def test_vectors_come_back_in_input_order_with_one_row_and_the_billed_cost() -> None:
    calls = _Calls()

    def handler(_request: httpx2.Request) -> httpx2.Response:
        body = _ok(3).json()
        body["data"].reverse()
        return httpx2.Response(200, json=body)

    outcome = await _gateway(handler, calls).embed_documents(["a", "b", "c"], acting_user_id=_ADMIN)

    assert outcome == Embedded(
        vectors=((0.0,) * 4, (0.1,) * 4, (0.2,) * 4), model="qwen/qwen3-embedding-8b"
    )
    (row,) = calls.recorded
    assert (row.user_id, row.template_id, row.template_version) == (
        _ADMIN,
        EMBEDDING_TEMPLATE_ID,
        1,
    )
    assert (row.model, row.input_tokens, row.output_tokens) == ("qwen/qwen3-embedding-8b", 17, 0)
    assert row.cost == Decimal("0.00002")
    assert row.outcome is LlmCallOutcome.SUCCESS


async def test_a_response_without_a_cost_records_none() -> None:
    calls = _Calls()

    await _gateway(lambda _r: _ok(1, cost=None), calls).embed_documents(
        ["a"], acting_user_id=_ADMIN
    )

    assert calls.recorded[0].cost is None


@pytest.mark.parametrize(
    ("headers", "expected"),
    [({"retry-after": "7"}, 7.0), ({}, None), ({"retry-after": "soon"}, None)],
    ids=["seconds", "none", "not_a_number"],
)
async def test_a_429_is_rate_limited_with_its_wait_and_one_error_row(
    headers: dict[str, str], expected: float | None
) -> None:
    calls = _Calls()

    def handler(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            429, headers=headers, json={"error": {"code": 429, "message": "Rate limit exceeded"}}
        )

    outcome = await _gateway(handler, calls).embed_documents(["a"], acting_user_id=_ADMIN)

    assert outcome == RateLimited(retry_after_seconds=expected)
    assert [row.outcome for row in calls.recorded] == [LlmCallOutcome.ERROR]


@pytest.mark.parametrize("status", [400, 402, 502], ids=["bad_request", "no_credit", "upstream"])
async def test_another_error_status_fails_with_one_error_row(status: int) -> None:
    calls = _Calls()

    outcome = await _gateway(
        lambda _r: httpx2.Response(status, json={"error": {"code": status, "message": "no"}}),
        calls,
    ).embed_documents(["a"], acting_user_id=_ADMIN)

    assert outcome == EmbeddingFailed(reason=EmbeddingFailure.ERROR)
    assert [row.outcome for row in calls.recorded] == [LlmCallOutcome.ERROR]


async def test_a_network_failure_fails_the_call() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused", request=request)

    calls = _Calls()
    outcome = await _gateway(handler, calls).embed_documents(["a"], acting_user_id=_ADMIN)

    assert outcome == EmbeddingFailed(reason=EmbeddingFailure.ERROR)
    assert [row.outcome for row in calls.recorded] == [LlmCallOutcome.ERROR]


async def test_a_slow_upstream_times_out_with_a_timeout_row() -> None:
    class _Slow(httpx2.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
            await asyncio.sleep(5)
            return _ok(1)

    calls = _Calls()
    outcome = await _gateway(
        lambda _r: _ok(1), calls, timeout_seconds=0.01, transport=_Slow()
    ).embed_documents(["a"], acting_user_id=_ADMIN)

    assert outcome == EmbeddingFailed(reason=EmbeddingFailure.TIMEOUT)
    assert [row.outcome for row in calls.recorded] == [LlmCallOutcome.TIMEOUT]


@pytest.mark.parametrize(
    "response",
    [
        _ok(1, dimensions=3),
        _ok(2),
        httpx2.Response(200, json={"data": "nope"}),
        httpx2.Response(200, content=b"not json"),
    ],
    ids=["wrong_length", "wrong_count", "malformed", "not_json"],
)
async def test_a_response_of_the_wrong_shape_fails_the_batch(response: httpx2.Response) -> None:
    calls = _Calls()

    outcome = await _gateway(lambda _r: response, calls).embed_documents(
        ["a"], acting_user_id=_ADMIN
    )

    assert outcome == EmbeddingFailed(reason=EmbeddingFailure.WRONG_SHAPE)
    assert [row.outcome for row in calls.recorded] == [LlmCallOutcome.ERROR]
