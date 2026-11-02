from typing import Protocol
from uuid import UUID


class BandOrderPort(Protocol):
    """The athlete's own scale of resistance bands, lightest first (PRD-0010 B39, ADR-0032),
    mapped by `sport_id` in the composition root so core code never branches on a sport. Every
    method is scoped to one `user_id` from the authenticated session; a band is never ranked
    from its colour, only from the athlete's answer."""

    async def ranked(self, user_id: UUID) -> list[str]:
        """The labels of the user's ranked bands, lightest first."""
        ...

    async def place(self, user_id: UUID, label: str, rank: int) -> None:
        """Gives the user's unranked band `label` the rank `rank`, 1 the lightest, and moves
        the bands from that rank on one up, in one transaction. A rank past the last place is
        the last place. A band that is already ranked, or gone, is left as it is."""
        ...

    async def next_unplaced(self, user_id: UUID) -> str | None:
        """The label of a band with no rank that was never asked about, or `None`."""
        ...
