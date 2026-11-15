"""The exercise card's pictures (F23, F24, ADR-0031, #558): every candidate's first approved
picture in one read, with its public URL and credit; with no public media base, nothing."""

from collections.abc import Collection, Mapping
from uuid import UUID, uuid4

from ai_trainer.application.exercise_card_media import ExerciseCardMedia
from ai_trainer.application.media_credit import WgerCredit
from ai_trainer.domain.media import MediaAsset, MediaDelivery, MediaKind, MediaRights

_BASE = "https://media.example.test"


def _wger(*, ai: bool = False) -> MediaAsset:
    return MediaAsset(
        kind=MediaKind.IMAGE,
        rights=MediaRights.LICENSED,
        delivery=MediaDelivery.STORED,
        storage_key="wger/squat.png",
        source="wger",
        source_ref="https://wger.de/api/v2/exerciseimage/7/" + ("#ai-generated" if ai else ""),
        license="CC BY-SA 4.0",
        attribution=(
            "K. Brandt · CC BY-SA 4.0 (https://creativecommons.org/licenses/by-sa/4.0/)"
            " — via wger.de"
        ),
        sha256="a" * 64,
    )


class _Media:
    def __init__(self, pictures: Mapping[UUID, MediaAsset]) -> None:
        self.pictures = pictures
        self.calls: list[set[UUID]] = []

    async def first_approved_pictures(
        self, exercise_ids: Collection[UUID]
    ) -> Mapping[UUID, MediaAsset]:
        self.calls.append(set(exercise_ids))
        return {key: value for key, value in self.pictures.items() if key in exercise_ids}


async def test_all_candidates_read_in_one_call() -> None:
    first, second, bare = uuid4(), uuid4(), uuid4()
    media = _Media({first: _wger(), second: _wger(ai=True)})

    pictures = await ExerciseCardMedia(media=media, public_base_url=_BASE).pictures(
        [first, second, bare]
    )

    assert media.calls == [{first, second, bare}]
    assert set(pictures) == {first, second}
    picture = pictures[first]
    assert picture.public_url == f"{_BASE}/wger/squat.png"
    assert picture.origin == "wger"
    assert picture.credit == WgerCredit(
        author="K. Brandt",
        licence="CC BY-SA 4.0",
        licence_url="https://creativecommons.org/licenses/by-sa/4.0/",
    )
    assert picture.page_url == "https://wger.de/api/v2/exerciseimage/7/"
    assert not picture.ai_flagged
    assert pictures[second].ai_flagged


async def test_no_public_base_url_reads_nothing_and_shows_text_only() -> None:
    exercise = uuid4()
    media = _Media({exercise: _wger()})

    pictures = await ExerciseCardMedia(media=media, public_base_url=None).pictures([exercise])

    assert pictures == {}
    assert media.calls == []


async def test_no_candidates_read_nothing() -> None:
    media = _Media({})

    assert await ExerciseCardMedia(media=media, public_base_url=_BASE).pictures([]) == {}
    assert media.calls == []
