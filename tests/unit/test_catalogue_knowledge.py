"""Which catalogue texts the worker embeds, and how each becomes a chunk (#427)."""

from uuid import UUID

from ai_trainer.domain.catalogue_knowledge import (
    CatalogueChunkState,
    CatalogueText,
    catalogue_chunk,
    catalogue_content_hash,
    has_changed_text,
    kept_exercises,
    stale_chunks,
)
from ai_trainer.domain.knowledge import SourceLanguage
from ai_trainer.domain.knowledge_document import SectionKind, SectionLocator

_MODEL = "qwen/qwen3-embedding-8b"
_RAISE = CatalogueText(UUID(int=1), "Dumbbell Lateral Raise", "Raise your arms to the sides.")
_CURL = CatalogueText(UUID(int=2), "Dumbbell Curl", "Curl the dumbbells up.")


def _stored(text: CatalogueText, *, model: str = _MODEL) -> CatalogueChunkState:
    return CatalogueChunkState(text.exercise_id, text.instructions, (text.name,), model)


def test_a_chunk_is_the_instructions_under_the_exercise_name() -> None:
    made = catalogue_chunk(CatalogueText(UUID(int=1), "Dumbbell Curl", "  Curl them up.\n"))

    assert made.exercise_id == UUID(int=1)
    assert made.chunk.text == "Curl them up."
    assert made.chunk.heading_path == ("Dumbbell Curl",)
    assert made.chunk.section_kind is SectionKind.BODY
    assert made.chunk.locator == SectionLocator(section="Dumbbell Curl")


def test_catalogue_chunk_carries_target_and_equipment() -> None:
    # #508: a paraphrase names the movement, so what it works and with what is embedded too.
    made = catalogue_chunk(
        CatalogueText(
            UUID(int=1),
            "Dumbbell Lateral Raise",
            " Raise your arms to the sides.\n",
            target="delts",
            equipment="dumbbell",
        )
    )

    assert made.chunk.text == "Raise your arms to the sides.\n\nTarget: delts. Equipment: dumbbell."
    assert made.chunk.heading_path == ("Dumbbell Lateral Raise",)


def test_a_chunk_names_only_the_metadata_it_has() -> None:
    target_only = CatalogueText(UUID(int=1), "Plank", "Hold it.", target="abs")
    equipment_only = CatalogueText(UUID(int=2), "Band Pull Apart", "Pull.", equipment="band")

    assert catalogue_chunk(target_only).chunk.text == "Hold it.\n\nTarget: abs."
    assert catalogue_chunk(equipment_only).chunk.text == "Pull.\n\nEquipment: band."


def test_changed_metadata_makes_the_chunk_stale() -> None:
    before = CatalogueText(UUID(int=1), "Dumbbell Curl", "Curl up.", target="biceps")
    after = CatalogueText(UUID(int=1), "Dumbbell Curl", "Curl up.", target="forearms")
    stored = CatalogueChunkState(
        before.exercise_id, catalogue_chunk(before).chunk.text, (before.name,), _MODEL
    )

    assert stale_chunks([before], [stored], model=_MODEL) == []
    assert stale_chunks([after], [stored], model=_MODEL) == [catalogue_chunk(after)]


def test_the_import_sees_a_text_change_as_the_worker_does() -> None:
    # One rule for "stale": the import queues a source only when the worker would embed in it.
    # A model change is not a text change: it is a re-embed job (ADR-0033 invariant 4).
    raise_ = CatalogueText(UUID(int=1), "Dumbbell Lateral Raise", "Raise.", target="delts")
    stored = CatalogueChunkState(
        raise_.exercise_id, catalogue_chunk(raise_).chunk.text, (raise_.name,), "old/model"
    )
    moved = CatalogueText(raise_.exercise_id, raise_.name, raise_.instructions, target="traps")
    blank = CatalogueText(UUID(int=9), "Plank", " ")

    assert not has_changed_text([raise_, blank], [stored])
    assert has_changed_text([moved], [stored])
    assert has_changed_text([raise_, _CURL], [stored])


def test_metadata_alone_is_nothing_to_embed() -> None:
    blank = CatalogueText(UUID(int=9), "Plank", "  ", target="abs", equipment="body weight")

    assert stale_chunks([blank], [], model=_MODEL) == []
    assert kept_exercises([blank]) == frozenset()


def test_unchanged_texts_embed_nothing() -> None:
    assert stale_chunks([_RAISE, _CURL], [_stored(_RAISE), _stored(_CURL)], model=_MODEL) == []


def test_a_missing_changed_renamed_or_other_model_chunk_is_stale() -> None:
    renamed = CatalogueText(_CURL.exercise_id, "Biceps Curl", _CURL.instructions)
    changed = CatalogueText(_RAISE.exercise_id, _RAISE.name, "Lift the arms sideways.")
    third = CatalogueText(UUID(int=3), "Squat", "Sit back and stand up.")
    fourth = CatalogueText(UUID(int=4), "Lunge", "Step forward and lower.")

    stale = stale_chunks(
        [changed, renamed, third, fourth],
        [_stored(_RAISE), _stored(_CURL), _stored(third, model="old/model"), _stored(fourth)],
        model=_MODEL,
    )

    assert [chunk.exercise_id for chunk in stale] == [UUID(int=1), UUID(int=2), UUID(int=3)]


def test_a_blank_text_is_neither_embedded_nor_kept() -> None:
    blank = CatalogueText(UUID(int=9), "Plank", "   ")

    assert stale_chunks([blank, _RAISE], [], model=_MODEL) == [catalogue_chunk(_RAISE)]
    assert kept_exercises([blank, _RAISE]) == frozenset({_RAISE.exercise_id})


def test_each_language_has_its_own_source_key() -> None:
    english = catalogue_content_hash(SourceLanguage.EN)

    assert english != catalogue_content_hash(SourceLanguage.PL)
    assert len(english) == 64
