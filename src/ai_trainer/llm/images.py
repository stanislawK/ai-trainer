"""The image adapter (ADR-0031 *Picture generation*, #533): OpenRouter's `POST /api/v1/images`,
called directly, as `embeddings.py` calls its endpoint, since it doesn't go through the text
gateway. It has its own timeout, error translation and accounting: every call writes one
`llm_calls` row with the cost the response bills (ADR-0018 invariant 6), under the admin who
runs the generation.

The body names the model and the prompt, and nothing else: no `user` field, so the admin never
leaves the app. The prompt is the brief's image prompt; it never reaches the row, which has no
text field. OpenRouter documents an image's `media_type` as optional, so when it is absent the
type is read from the bytes' magic numbers. A non-2xx answer, a transport error, malformed JSON,
no image, or a type other than JPEG, PNG or WebP (declared or detected) is an `error` row, which
keeps the billed cost, and a returned `ImageGenerationFailed`, never an exception
(ADR-0007). The failure names its cause, the HTTP status and the provider's error code and
message (#543); the message is stripped of the prompt and the key, and nothing is logged."""

import base64
import binascii
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

import httpx2
from pydantic import BaseModel, ValidationError

from ai_trainer.application.ports.llm_calls import LlmCallsRepositoryPort
from ai_trainer.domain.llm_calls import LlmCallOutcome
from ai_trainer.domain.media_generation import (
    GeneratedImage,
    ImageFailureCause,
    ImageGenerationFailed,
    ImageMediaType,
)
from ai_trainer.llm.call_accounting import CallAccountant, CallKey, CallUsage

IMAGES_URL = "https://openrouter.ai/api/v1/images"
# The `llm_calls` key of a picture. It has no prompt template: the prompt is the brief's
# `image_prompt`, written by `media.picture_brief` (#532).
PICTURE_TEMPLATE_ID = "media.picture"
PICTURE_TEMPLATE_VERSION = 1


class _Image(BaseModel):
    b64_json: str
    media_type: str | None = None


_MEDIA_TYPES: dict[str, ImageMediaType] = {
    "image/jpeg": "image/jpeg",
    "image/png": "image/png",
    "image/webp": "image/webp",
}


def _sniffed(data: bytes) -> ImageMediaType | None:
    """The type the bytes' magic numbers name, if it is one the command writes."""
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


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


class _ErrorDetail(BaseModel):
    code: int | str | None = None
    message: str | None = None


class _ErrorBody(BaseModel):
    """OpenRouter's `{"error": {"code": ..., "message": ...}}`, read leniently."""

    error: _ErrorDetail


_MAX_MESSAGE = 200
_MAX_CODE = 64
# A provider message holding any run of this many of the prompt's characters is dropped whole:
# it may be echoing a fragment the exact-match redaction can't see.
_ECHO_WINDOW = 20
_REDACTED = "[redacted]"


def _failed(
    cause: ImageFailureCause,
    *,
    status: int | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> ImageGenerationFailed:
    return ImageGenerationFailed(
        reason="error",
        cause=cause,
        status=status,
        error_code=error_code,
        error_message=error_message,
    )


def _echoes(text: str, prompt: str) -> bool:
    window = min(_ECHO_WINDOW, len(prompt))
    return window > 0 and any(
        prompt[start : start + window] in text for start in range(len(prompt) - window + 1)
    )


def _safe_text(text: str, *, prompt: str, api_key: str, limit: int) -> str:
    """Provider text without the prompt or the key, on one line, at most `limit` chars; text
    that still echoes part of the prompt is dropped whole."""
    for secret in (api_key, prompt):
        if secret:
            text = text.replace(secret, _REDACTED)
    if _echoes(text, prompt):
        return _REDACTED
    return " ".join(text.split())[:limit]


def _http_failure(response: httpx2.Response, *, prompt: str, api_key: str) -> ImageGenerationFailed:
    try:
        error = _ErrorBody.model_validate_json(response.content).error
    except ValidationError:
        return _failed("http_status", status=response.status_code)
    return _failed(
        "http_status",
        status=response.status_code,
        error_code=(
            None
            if error.code is None
            else _safe_text(str(error.code), prompt=prompt, api_key=api_key, limit=_MAX_CODE)
        ),
        error_message=(
            None
            if error.message is None
            else _safe_text(error.message, prompt=prompt, api_key=api_key, limit=_MAX_MESSAGE)
        ),
    )


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
        # `_post` turns every HTTP error but a timeout into a reply, so this is anything else.
        return _failed("unexpected")

    async def _post(self, prompt: str) -> _Reply:
        try:
            response = await self._client.post(
                IMAGES_URL,
                json={"model": self._model, "prompt": prompt},
                headers={"Authorization": f"Bearer {self._api_key}"},
            )
        except httpx2.HTTPError as error:
            # The class name only: a message can carry the URL or a header.
            return _Reply(_failed("transport", error_code=type(error).__name__), CallUsage())
        if response.is_error:
            return _Reply(
                _http_failure(response, prompt=prompt, api_key=self._api_key), CallUsage()
            )
        return _read(response.content, status=response.status_code)


def _read(content: bytes, *, status: int) -> _Reply:
    try:
        body = _Body.model_validate_json(content)
    except ValidationError:
        return _Reply(_failed("malformed_body", status=status), _billed(content))
    billed = _usage(body.usage)
    if not body.data:
        return _Reply(_failed("empty_data", status=status), billed)
    image = body.data[0]
    try:
        data = base64.b64decode(image.b64_json, validate=True)
    except binascii.Error:
        return _Reply(_failed("bad_base64", status=status), billed)
    media_type = _sniffed(data) if image.media_type is None else _MEDIA_TYPES.get(image.media_type)
    if media_type is None:
        return _Reply(_failed("unsupported_type", status=status), billed)
    return _Reply(GeneratedImage(data=data, media_type=media_type), billed)


def _billed(content: bytes) -> CallUsage:
    try:
        return _usage(_Billed.model_validate_json(content).usage)
    except ValidationError:
        return CallUsage()
