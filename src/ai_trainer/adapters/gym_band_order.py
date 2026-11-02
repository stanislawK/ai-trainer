"""The gym's band scale (PRD-0010 B39, ADR-0032, ticket #431): which of the athlete's bands still
need a place, the places they take, and the question that asks for one. Every statement filters
by `user_id`. A band is asked about once: a `band` question about its label, open, answered or
skipped, settles that."""

from collections.abc import Callable
from uuid import UUID

from sqlalchemy import ColumnElement, exists, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.orm import (
    AthleteBandOrm,
    ClarificationOrm,
    GymExerciseEntryOrm,
    GymSetOrm,
)
from ai_trainer.domain.clarification import BAND_PURPOSE, Clarification
from ai_trainer.domain.sports.gym.bands import band_question

_OPEN = "open"


def _asked(user_id: UUID) -> ColumnElement[bool]:
    """The band's label has a `band` question of the user's, in any status."""
    return exists().where(
        ClarificationOrm.user_id == user_id,
        ClarificationOrm.purpose == BAND_PURPOSE,
        ClarificationOrm.clarification["subject"].astext == AthleteBandOrm.label,
    )


async def ranked_labels(tx: AsyncSession, user_id: UUID) -> list[str]:
    """The user's ranked bands, lightest first."""
    rows = await tx.scalars(
        select(AthleteBandOrm.label)
        .where(AthleteBandOrm.user_id == user_id, AthleteBandOrm.rank.is_not(None))
        .order_by(AthleteBandOrm.rank, AthleteBandOrm.label)
    )
    return list(rows)


async def _asking(tx: AsyncSession, user_id: UUID) -> bool:
    """Whether a band question of the user's is open: one is asked at a time (ADR-0015)."""
    return bool(
        await tx.scalar(
            select(
                exists().where(
                    ClarificationOrm.user_id == user_id,
                    ClarificationOrm.purpose == BAND_PURPOSE,
                    ClarificationOrm.status == _OPEN,
                )
            )
        )
    )


async def ask_for_log(
    tx: AsyncSession, user_id: UUID, source_id: UUID, *, max_options: int
) -> Clarification | None:
    """The question the log just confirmed asks, in the confirm's transaction: about the first
    band of the log, in the order it names them, that has no rank and was never asked about.
    The athlete's first band takes rank 1 here without a question. `None` when nothing is left
    to ask, or another band question is still open."""
    if await _asking(tx, user_id):
        return None
    labels = await tx.scalars(
        select(AthleteBandOrm.label)
        .join(
            GymSetOrm,
            (GymSetOrm.band_id == AthleteBandOrm.id)
            & (GymSetOrm.user_id == AthleteBandOrm.user_id),
        )
        .join(
            GymExerciseEntryOrm,
            (GymExerciseEntryOrm.id == GymSetOrm.entry_id)
            & (GymExerciseEntryOrm.user_id == GymSetOrm.user_id),
        )
        .where(
            AthleteBandOrm.user_id == user_id,
            AthleteBandOrm.rank.is_(None),
            GymExerciseEntryOrm.source_id == source_id,
            ~_asked(user_id),
        )
        .order_by(GymExerciseEntryOrm.position, GymSetOrm.set_no)
    )
    for label in dict.fromkeys(labels):
        ranked = await ranked_labels(tx, user_id)
        if ranked:
            return band_question(label, ranked, max_options=max_options)
        await tx.execute(
            update(AthleteBandOrm)
            .where(AthleteBandOrm.user_id == user_id, AthleteBandOrm.label == label)
            .values(rank=1)
        )
    return None


class SqlAlchemyBandOrder:
    """Implements `BandOrderPort` over `athlete_bands` (ADR-0004, ADR-0032)."""

    def __init__(self, session_factory: Callable[[], AsyncSession]) -> None:
        self._session_factory = session_factory

    async def ranked(self, user_id: UUID) -> list[str]:
        async with self._session_factory() as tx:
            return await ranked_labels(tx, user_id)

    async def place(self, user_id: UUID, label: str, rank: int) -> None:
        async with self._session_factory() as tx:
            # The athlete's bands are locked together, so two placements renumber one after the
            # other.
            await tx.execute(
                select(AthleteBandOrm.id).where(AthleteBandOrm.user_id == user_id).with_for_update()
            )
            band = await tx.scalar(
                select(AthleteBandOrm.id).where(
                    AthleteBandOrm.user_id == user_id,
                    AthleteBandOrm.label == label,
                    AthleteBandOrm.rank.is_(None),
                )
            )
            if band is None:
                return
            ranked = await tx.scalar(
                select(func.count()).where(
                    AthleteBandOrm.user_id == user_id, AthleteBandOrm.rank.is_not(None)
                )
            )
            taken = min(rank, (ranked or 0) + 1)
            await tx.execute(
                update(AthleteBandOrm)
                .where(AthleteBandOrm.user_id == user_id, AthleteBandOrm.rank >= taken)
                .values(rank=AthleteBandOrm.rank + 1)
            )
            await tx.execute(
                update(AthleteBandOrm)
                .where(AthleteBandOrm.user_id == user_id, AthleteBandOrm.id == band)
                .values(rank=taken)
            )
            await tx.commit()

    async def next_unplaced(self, user_id: UUID) -> str | None:
        async with self._session_factory() as tx:
            labels = await tx.scalars(
                select(AthleteBandOrm.label)
                .where(
                    AthleteBandOrm.user_id == user_id,
                    AthleteBandOrm.rank.is_(None),
                    ~_asked(user_id),
                )
                .order_by(AthleteBandOrm.label_norm)
                .limit(1)
            )
            return labels.first()
