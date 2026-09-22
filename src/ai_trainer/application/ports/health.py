from typing import Protocol


class DatabaseHealthPort(Protocol):
    """Reports whether the database is reachable."""

    async def ping(self) -> bool: ...
