"""The value types, defaults and checks `Settings` is built from (ADR-0002), kept apart so the
class stays within the size rule (ADR-0003). Nothing here reads the environment."""

import json
import re
from decimal import Decimal
from typing import Annotated, Any, Self
from urllib.parse import urlsplit

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic_settings import NoDecode

from ai_trainer.domain.llm_calls import ProviderPreferences, ReasoningEffort


class ExerciseSearchSettings(BaseModel):
    """The catalogue's search by meaning as the exercise shortlist's third input (ADR-0031,
    #429): one embedding call per name history doesn't know. On since #508, owner-directed after
    the smoke set and the latency scenario measured it; the search is cut off after the timeout,
    and with no catalogue chunks it finds nothing, so today's shortlist stands."""

    exercise_search_enabled: bool = True
    exercise_search_timeout_seconds: float = Field(default=2.5, gt=0)
    # When no trigram candidate scores this high, the typed name matches no catalogue name and is
    # read as a paraphrase: the search's order leads and the trigram list follows (#508). Measured
    # 2026-11-07: paraphrases top out at 0.32 to 0.34, real names start at 0.59 ("Triceps
    # pushdown"); fitted to those few names (ADR-0031's #508 amendment).
    exercise_name_match_floor: float = Field(default=0.5, ge=0, le=1)


def split_comma_separated(value: Any) -> Any:  # noqa: ANN401 -- raw env input of any type
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


CommaSeparated = Annotated[list[str], NoDecode, BeforeValidator(split_comma_separated)]


def json_object(value: Any) -> Any:  # noqa: ANN401 -- raw env input of any type
    # Decoded here, not by pydantic-settings, so a blank value copied from `.env.example` is
    # unset rather than invalid JSON; the object is then checked field by field.
    if isinstance(value, str):
        return json.loads(value) if value.strip() else None
    return value


# A template's OpenRouter provider preferences as one JSON object, e.g.
# `{"sort": "latency", "preferred_max_latency": {"p90": 2}}` (ADR-0026 step 4).
ProviderPreferencesSetting = Annotated[
    ProviderPreferences | None, NoDecode, BeforeValidator(json_object)
]
# A template's own call timeout; unset falls back to `llm_call_timeout_seconds`.
CallTimeout = Annotated[float | None, Field(gt=0)]

# Picked on `onboarding.about_you`'s first eval baseline (ADR-0028, #254): 98% of its
# assertions at `low` effort on fp8/nvfp4 upstreams sorted by latency. OpenRouter's default
# price sort lands on an upstream with no structured outputs, which fails the read.
DEFAULT_ONBOARDING_MODEL = "z-ai/glm-5.3-flash"
DEFAULT_ONBOARDING_PROVIDER = ProviderPreferences(sort="latency", quantizations=["fp8", "nvfp4"])
DEFAULT_ONBOARDING_REASONING_EFFORT: ReasoningEffort = "low"

# The starting point for `<sport>.answer` (#421), confirmed by its smoke set: what the other
# templates run on, at `low` effort so a tool loop stays quick.
DEFAULT_ANSWER_MODEL = "z-ai/glm-5.3-flash"
DEFAULT_ANSWER_PROVIDER = ProviderPreferences(sort="latency", quantizations=["fp8", "nvfp4"])
DEFAULT_ANSWER_REASONING_EFFORT: ReasoningEffort = "low"

# `media.picture_brief` (ADR-0031, #532): what the other read templates run on; its smoke set
# confirms it.
DEFAULT_PICTURE_BRIEF_MODEL = "z-ai/glm-5.3-flash"
# The fallback media source's model and its tool-printed price per picture (ADR-0031 #524,
# 2026-11-08). The catalogue's `image_output` line reads lower; the price only gates the call
# against the run's cap, so the higher figure is the safe side (#533).
DEFAULT_IMAGE_GENERATION_MODEL = "google/gemini-3.1-flash-lite-image"
# Dated `canonical_slug`s (`get-model`, 2026-11-11) of the image models we know, by the model ID
# that is called: what a stored picture's `source_ref` records (ADR-0031 *Fallback media
# source*). A model not listed here is recorded under its own ID.
IMAGE_MODEL_DATED_SLUGS = {
    DEFAULT_IMAGE_GENERATION_MODEL: "google/gemini-3.1-flash-lite-image-20260630",
}


def image_model_source_ref(model: str) -> str:
    return IMAGE_MODEL_DATED_SLUGS.get(model, model)


DEFAULT_IMAGE_GENERATION_PRICE_USD = Decimal("0.0336")

# Chosen by the owner at #417 (ADR-0033): one space for English and Polish, on DeepInfra only.
DEFAULT_EMBEDDING_MODEL = "qwen/qwen3-embedding-8b"
DEFAULT_EMBEDDING_PROVIDER = ProviderPreferences(only=["deepinfra"])

# A Jev ID carries a version number; aliases such as `~typesafe/jev-latest` move under a
# calibrated threshold, so they are refused (ADR-0020).
VERSIONED_DECISION_MODEL = re.compile(r"^[a-z0-9-]+/jev-\d+\.\d+(\.\d+)?(-\d{8})?$")
# The only hosts `LLM_DEBUG_LOG` may run behind (#278); `localhost.example.com` is public.
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1"})
# Set together or not at all (ADR-0033). `object_storage_region` has a default, so it isn't one.
OBJECT_STORAGE_KEYS = (
    "object_storage_endpoint",
    "object_storage_access_key_id",
    "object_storage_secret_access_key",
    "object_storage_uploads_bucket",
)


def http_origin(value: str) -> str | None:
    """`value` as a lower-case http(s) origin, or None when it is anything else."""
    try:
        parts = urlsplit(value.strip())
        host = parts.hostname or ""
        port = parts.port
    except ValueError:
        return None
    # An IPv6 literal or a trailing-dot host would never match the Host header Starlette's
    # trusted-host check compares, locking the app out of every request.
    has_host = bool(host) and ":" not in host and not host.endswith(".") and port != 0
    if (
        parts.scheme not in ("http", "https")
        or not has_host
        or parts.path not in ("", "/")
        or parts.query
        or parts.fragment
        or parts.username is not None
        or parts.password is not None
    ):
        return None
    return f"{parts.scheme}://{parts.netloc.lower()}"


def _public_origin(value: str) -> str:
    origin = http_origin(value)
    if origin is None:
        raise ValueError("must be an http(s) origin without a path, such as https://wger.de")
    return origin


# A public service's base URL (PMC's bucket, wger.de), kept as its lower-case origin.
PublicOrigin = Annotated[str, AfterValidator(_public_origin)]


class ObjectStorageSettings(BaseModel):
    """The `uploads` and `media` buckets on an S3-compatible service (ADR-0033): R2 in
    production, versitygw under `make start`. R2's region is `auto`; the timeout bounds one
    request with its retries. `Settings` reads a blank key as unset."""

    object_storage_endpoint: str | None = None
    object_storage_region: str = "auto"
    object_storage_access_key_id: SecretStr | None = None
    object_storage_secret_access_key: SecretStr | None = None
    object_storage_uploads_bucket: str | None = None
    object_storage_timeout_seconds: float = Field(default=60.0, gt=0)
    # Catalogue pictures (ADR-0031), on the same service and keys. Optional, so a release never
    # waits on it; set without the keys it refuses to start (#530).
    object_storage_media_bucket: str | None = None

    @field_validator("object_storage_endpoint")
    @classmethod
    def _object_storage_endpoint_is_an_origin(cls, value: str | None) -> str | None:
        if value is None:
            return None
        origin = http_origin(value)
        if origin is None:
            # Never echoes the value: credentials pasted into the URL would reach the log.
            raise ValueError(
                "OBJECT_STORAGE_ENDPOINT must be an http(s) origin without a path or credentials, "
                "such as http://localhost:7070"
            )
        return origin

    @model_validator(mode="after")
    def _object_storage_all_or_nothing(self) -> Self:
        # Unset is fine until a feature needs it, so a release never waits on new keys. Half set
        # is a mistake that should stop the app at start, not at an admin's upload.
        missing = [key.upper() for key in OBJECT_STORAGE_KEYS if getattr(self, key) is None]
        if 0 < len(missing) < len(OBJECT_STORAGE_KEYS):
            raise ValueError(f"object storage is only partly set; missing {', '.join(missing)}")
        if self.object_storage_media_bucket is not None and missing:
            raise ValueError(
                f"OBJECT_STORAGE_MEDIA_BUCKET needs every OBJECT_STORAGE_* key, the uploads bucket "
                f"included; missing "
                f"{', '.join(missing)}"
            )
        return self
