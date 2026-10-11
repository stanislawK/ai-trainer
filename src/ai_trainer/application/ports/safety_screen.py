from typing import Protocol
from uuid import UUID


class SafetyScreenPort(Protocol):
    """A second line of defence for G3 next to the router (ADR-0020): does the athlete's message
    mention pain or injury of their own?"""

    async def finds_pain(self, user_id: UUID, message: str) -> bool:
        """Never raises: a screen that timed out or failed finds no pain, which leaves the
        router's result as it was."""
        ...
