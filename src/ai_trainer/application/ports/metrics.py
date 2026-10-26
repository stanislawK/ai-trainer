from typing import Literal, Protocol

from ai_trainer.domain.llm_calls import NewLlmCall

# Which ADR-0029 cap refused: the sign-in routes' per-address cap, the per-session cap, or the
# per-user model-call budget's hourly or in-flight limit.
type LimitKind = Literal["sign_in", "session", "model_calls_per_hour", "model_calls_in_flight"]


class MetricsPort(Protocol):
    """Counts and durations for the dashboard and alerts (ADR-0030). Every label is a bounded
    set: a route template, never a raw path, and never a user, message or request ID
    (invariant 4). Recording never raises."""

    def request(self, *, method: str, route: str, status: int, seconds: float) -> None: ...

    def model_call(self, call: NewLlmCall) -> None:
        """From the same values as the call's `llm_calls` row (ADR-0018)."""
        ...

    def refusal(self, kind: LimitKind) -> None: ...

    def log_turn(self, seconds: float) -> None:
        """From sending a log to its draft card (G14)."""
        ...


class NoMetrics:
    """Implements `MetricsPort` when telemetry is off (ADR-0030 invariant 5)."""

    def request(self, *, method: str, route: str, status: int, seconds: float) -> None:
        pass

    def model_call(self, call: NewLlmCall) -> None:
        pass

    def refusal(self, kind: LimitKind) -> None:
        pass

    def log_turn(self, seconds: float) -> None:
        pass


NO_METRICS: MetricsPort = NoMetrics()
