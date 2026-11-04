"""ADR-0007's single embeddings adapter: OpenRouter's `POST /api/v1/embeddings`, called directly.

Not through Pydantic AI's `Embedder`: its OpenAI-compatible model drops OpenRouter's `usage.cost`
and can only estimate one from genai-prices, which ADR-0018 invariant 6 forbids. Here every call
writes one `llm_calls` row with the cost the response bills, in OpenRouter credits (USD).

The body names the model, the inputs, `dimensions` and the provider pin, and nothing else: no
`user` field, so the acting admin never leaves the app (ADR-0011 invariant 2). Every vector's
length is checked, since `dimensions` isn't relied on to be honoured (ADR-0033).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from functools import cache
from pathlib import Path
from uuid import UUID

import httpx2
from pydantic import BaseModel, ValidationError

from ai_trainer.application.ports.llm_calls import LlmCallsRepositoryPort
from ai_trainer.domain.embeddings import (
    Embedded,
    EmbeddingFailed,
    EmbeddingFailure,
    EmbeddingOutcome,
    RateLimited,
)
from ai_trainer.domain.llm_calls import LlmCallOutcome, ProviderPreferences
from ai_trainer.llm.call_accounting import CallAccountant, CallKey, CallUsage
from ai_trainer.llm.prompts.registry import PromptRegistry

EMBEDDINGS_URL = "https://openrouter.ai/api/v1/embeddings"
# The `llm_calls` key of a document embedding. It has no prompt template: a document is
# embedded with no instruction (ADR-0033).
EMBEDDING_TEMPLATE_ID = "knowledge_embedding"
EMBEDDING_TEMPLATE_VERSION = 1
# A search query's instruction line, a prompt file (ADR-0033 *Query instruction*, ADR-0008):
# `Instruct: <task>\nQuery:` in the model card's form, the question appended as written (#424).
QUERY_TEMPLATE_ID = "knowledge.query"
QUERY_TEMPLATE_VERSION = 1
_PROMPTS_ROOT = Path(__file__).resolve().parent / "prompts"


@cache
def query_instruction() -> str:
    return PromptRegistry(root=_PROMPTS_ROOT).load_fixed_reply(
        QUERY_TEMPLATE_ID, QUERY_TEMPLATE_VERSION
    )


class _Item(BaseModel):
    index: int
    embedding: list[float]


class _Usage(BaseModel):
    prompt_tokens: int = 0
    cost: Decimal | None = None


class _Body(BaseModel):
    model: str
    data: list[_Item]
    usage: _Usage | None = None


@dataclass(frozen=True, slots=True)
class _Reply:
    outcome: EmbeddingOutcome
    usage: CallUsage


def _retry_after(response: httpx2.Response) -> float | None:
    try:
        return float(response.headers["retry-after"])
    except KeyError, ValueError:
        return None


class OpenRouterEmbeddingGateway:
    """Implements `EmbeddingGatewayPort`."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        dimensions: int,
        provider: ProviderPreferences | None,
        timeout_seconds: float,
        calls_repository: LlmCallsRepositoryPort,
        http_client: httpx2.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._dimensions = dimensions
        self._provider = provider
        self._accountant = CallAccountant(calls_repository, timeout_seconds=timeout_seconds)
        self._client = http_client or httpx2.AsyncClient(timeout=timeout_seconds)

    async def embed_documents(
        self, texts: Sequence[str], *, acting_user_id: UUID
    ) -> EmbeddingOutcome:
        return await self._embed(
            list(texts),
            CallKey(
                user_id=acting_user_id,
                template_id=EMBEDDING_TEMPLATE_ID,
                template_version=EMBEDDING_TEMPLATE_VERSION,
                model=self._model,
            ),
        )

    async def embed_query(self, text: str, *, acting_user_id: UUID) -> EmbeddingOutcome:
        """Implements `QueryEmbeddingPort`: the question behind the `knowledge.query` line."""
        return await self._embed(
            [f"{query_instruction()}{text}"],
            CallKey(
                user_id=acting_user_id,
                template_id=QUERY_TEMPLATE_ID,
                template_version=QUERY_TEMPLATE_VERSION,
                model=self._model,
            ),
        )

    async def _embed(self, texts: list[str], key: CallKey) -> EmbeddingOutcome:
        attempt = await self._accountant.attempt(
            key,
            self._post(texts),
            usage=lambda reply: reply.usage,
            outcome_of=lambda reply: (
                LlmCallOutcome.SUCCESS
                if isinstance(reply.outcome, Embedded)
                else LlmCallOutcome.ERROR
            ),
        )
        if attempt.value is not None:
            return attempt.value.outcome
        if attempt.outcome is LlmCallOutcome.TIMEOUT:
            return EmbeddingFailed(reason=EmbeddingFailure.TIMEOUT)
        return EmbeddingFailed(reason=EmbeddingFailure.ERROR)

    async def _post(self, texts: list[str]) -> _Reply:
        payload: dict[str, object] = {
            "model": self._model,
            "input": texts,
            "dimensions": self._dimensions,
            "encoding_format": "float",
        }
        if self._provider is not None:
            payload["provider"] = self._provider.model_dump(exclude_none=True)
        response = await self._client.post(
            EMBEDDINGS_URL, json=payload, headers={"Authorization": f"Bearer {self._api_key}"}
        )
        if response.status_code == httpx2.codes.TOO_MANY_REQUESTS:
            return _Reply(RateLimited(retry_after_seconds=_retry_after(response)), CallUsage())
        if response.is_error:
            return _Reply(EmbeddingFailed(reason=EmbeddingFailure.ERROR), CallUsage())
        return self._read(response, expected=len(texts))

    def _read(self, response: httpx2.Response, *, expected: int) -> _Reply:
        try:
            body = _Body.model_validate_json(response.content)
        except ValidationError:
            return _Reply(EmbeddingFailed(reason=EmbeddingFailure.WRONG_SHAPE), CallUsage())
        usage = body.usage or _Usage()
        billed = CallUsage(input_tokens=usage.prompt_tokens, cost=usage.cost, model=body.model)
        items = sorted(body.data, key=lambda item: item.index)
        if [item.index for item in items] != list(range(expected)) or any(
            len(item.embedding) != self._dimensions for item in items
        ):
            return _Reply(EmbeddingFailed(reason=EmbeddingFailure.WRONG_SHAPE), billed)
        vectors = tuple(tuple(item.embedding) for item in items)
        # Tagged with the configured model, which every search filters on (ADR-0011 invariant
        # 4), not the response's spelling of it; `llm_calls` keeps the billed name (#424).
        return _Reply(Embedded(vectors=vectors, model=self._model), billed)
