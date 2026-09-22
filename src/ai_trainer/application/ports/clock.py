from datetime import datetime
from typing import Protocol


class ClockPort(Protocol):
    """The only permitted source of the current time (ADR-0014). No adapter but the one
    backing this port may read the wall clock directly."""

    def now(self) -> datetime: ...
