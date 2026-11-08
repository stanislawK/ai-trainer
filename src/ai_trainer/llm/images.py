"""The image adapter (ADR-0031 *Picture generation*, #533): OpenRouter's `POST /api/v1/images`,
called directly, as `embeddings.py` calls its endpoint, since it doesn't go through the text
gateway. It has its own timeout, error translation and accounting: every call writes one
`llm_calls` row with the cost the response bills (ADR-0018 invariant 6), under the admin who
runs the generation.

The body names the model and the prompt, and nothing else: no `user` field, so the admin never
leaves the app. The prompt is the brief's image prompt; it never reaches the row, which has no
text field. A non-2xx answer, malformed JSON, no image, or a type other than JPEG or PNG is an
`error` row and a returned `ImageGenerationFailed`, never an exception (ADR-0007)."""

import base64
import binascii
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal
from uuid import UUID

import httpx2
from pydantic import BaseModel, ValidationError

from ai_trainer.application.ports.llm_calls import LlmCallsRepositoryPort
from ai_trainer.domain.llm_calls import LlmCallOutcome
from ai_trainer.domain.media_generation import GeneratedImage, ImageGenerationFailed
from ai_trainer.llm.call_accounting import CallAccountant, CallKey, CallUsage

IMAGES_URL = "https://openrouter.ai/api/v1/images"
# The `llm_calls` key of a picture. It has no prompt template: the prompt is the brief's
# `image_prompt`, written by `media.picture_brief` (#532).
PICTURE_TEMPLATE_ID = "media.picture"
PICTURE_TEMPLATE_VERSION = 1


class _Image(BaseModel):
    b64_json: str
    media_type: Literal["image/jpeg", "image/png"]


class _Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost: Decimal | None = None


class _Body(BaseModel):
    data: list[_Image]
    usage: _Usage | None = None


class _Billed(BaseModel):
    """Only the bill, read even when the image can't be: a billed call keeps its cost."""

    usage: _Usage | None = None


@dataclass(frozen=True, slots=True)
class _Reply:
    outcome: GeneratedImage | ImageGenerationFailed
    usage: CallUsage


_FAILED = ImageGenerationFailed(reason="error")


def _usage(usage: _Usage | None) -> CallUsage:
    usage = usage or _Usage()
    return CallUsage(
        input_tokens=usage.prompt_tokens, output_tokens=usage.completion_tokens, cost=usage.cost
    )


class OpenRouterImageGenerator:
    """Implements `ImageGenerationPort`."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        timeout_seconds: float,
        calls_repository: LlmCallsRepositoryPort,
        http_client: httpx2.AsyncClient,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._accountant = CallAccountant(calls_repository, timeout_seconds=timeout_seconds)
        self._client = http_client

    async def generate(
        self, *, user_id: UUID, prompt: str
    ) -> GeneratedImage | ImageGenerationFailed:
        attempt = await self._accountant.attempt(
            CallKey(
                user_id=user_id,
                template_id=PICTURE_TEMPLATE_ID,
                template_version=PICTURE_TEMPLATE_VERSION,
                model=self._model,
            ),
            self._post(prompt),
            usage=lambda reply: reply.usage,
            outcome_of=lambda reply: (
                LlmCallOutcome.SUCCESS
                if isinstance(reply.outcome, GeneratedImage)
                else LlmCallOutcome.ERROR
            ),
        )
        if attempt.value is not None:
            return attempt.value.outcome
        if attempt.outcome is LlmCallOutcome.TIMEOUT:
            return ImageGenerationFailed(reason="timeout")
        return _FAILED

    async def _post(self, prompt: str) -> _Reply:
        response = await self._client.post(
            IMAGES_URL,
            json={"model": self._model, "prompt": prompt},
            headers={"Authorization": f"Bearer {self._api_key}"},
        )
        if response.is_error:
            return _Reply(_FAILED, CallUsage())
        return _read(response.content)


def _read(content: bytes) -> _Reply:
    try:
        body = _Body.model_validate_json(content)
    except ValidationError:
        return _Reply(_FAILED, _billed(content))
    billed = _usage(body.usage)
    if not body.data:
        return _Reply(_FAILED, billed)
    image = body.data[0]
    try:
        data = base64.b64decode(image.b64_json, validate=True)
    except binascii.Error:
        return _Reply(_FAILED, billed)
    return _Reply(GeneratedImage(data=data, media_type=image.media_type), billed)


def _billed(content: bytes) -> CallUsage:
    try:
        return _usage(_Billed.model_validate_json(content).usage)
    except ValidationError:
        return CallUsage()
