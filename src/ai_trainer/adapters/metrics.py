"""App and model-call metrics over OpenTelemetry (ADR-0030).

Every label is a bounded set (invariant 4): a method, a route template, a status class, a
template ID and version, a model ID, an outcome or a limit kind. Nothing here can raise into a
request: a failed recording is logged and dropped, and the periodic reader exports on its own
thread, swallowing a failed export (invariant 5).
"""

import logging
from collections.abc import Callable

from opentelemetry.metrics import Meter

from ai_trainer.application.ports.metrics import LimitKind
from ai_trainer.domain.llm_calls import NewLlmCall

logger = logging.getLogger(__name__)

# OpenTelemetry's advisory buckets for HTTP server durations, in seconds.
_REQUEST_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.25, 0.5, 0.75, 1, 2.5, 5, 7.5, 10)
# Model calls run from a fraction of a second (Jev) to the extraction's timeout.
_MODEL_CALL_BUCKETS = (0.1, 0.25, 0.5, 1, 2, 3, 5, 7.5, 10, 15, 20, 30, 60)
# Dense around G14's bounds: 5 s at the median, 10 s at the 95th percentile.
_LOG_TURN_BUCKETS = (1, 2, 3, 4, 5, 6, 8, 10, 15, 20, 30, 60)


def status_class(status: int) -> str:
    return f"{status // 100}xx"


class OtelMetrics:
    """Implements `MetricsPort`. Constructed only in the composition root."""

    def __init__(self, meter: Meter) -> None:
        self._requests = meter.create_histogram(
            "http.server.request.duration",
            unit="s",
            description="Duration of HTTP server requests.",
            explicit_bucket_boundaries_advisory=_REQUEST_BUCKETS,
        )
        self._model_calls = meter.create_histogram(
            "ai_trainer.llm.call.duration",
            unit="s",
            description="Model calls, one per llm_calls row, by how they ended.",
            explicit_bucket_boundaries_advisory=_MODEL_CALL_BUCKETS,
        )
        self._model_cost = meter.create_counter(
            "ai_trainer.llm.call.cost",
            unit="USD",
            description="OpenRouter's accounted cost of model calls.",
        )
        self._refusals = meter.create_counter(
            "ai_trainer.rate_limit.refusals",
            unit="{refusal}",
            description="Requests and model calls refused by an ADR-0029 cap.",
        )
        self._log_turns = meter.create_histogram(
            "ai_trainer.log_turn.duration",
            unit="s",
            description="From sending a log to its draft card (G14).",
            explicit_bucket_boundaries_advisory=_LOG_TURN_BUCKETS,
        )

    def request(self, *, method: str, route: str, status: int, seconds: float) -> None:
        _guarded(
            lambda: self._requests.record(
                seconds,
                {
                    "http.request.method": method,
                    "http.route": route,
                    "http.response.status_class": status_class(status),
                },
            )
        )

    def model_call(self, call: NewLlmCall) -> None:
        labels: dict[str, str | int] = {
            "template_id": call.template_id,
            "template_version": call.template_version,
            "model": call.model,
            "outcome": call.outcome.value,
        }

        def record() -> None:
            self._model_calls.record(call.latency_ms / 1000, labels)
            if call.cost is not None:
                self._model_cost.add(float(call.cost), labels)

        _guarded(record)

    def refusal(self, kind: LimitKind) -> None:
        _guarded(lambda: self._refusals.add(1, {"limit_kind": kind}))

    def log_turn(self, seconds: float) -> None:
        _guarded(lambda: self._log_turns.record(seconds))


def _guarded(record: Callable[[], None]) -> None:
    try:
        record()
    except Exception:
        logger.warning("metric not recorded", exc_info=True)
