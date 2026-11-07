"""The exercise catalogue's texts as knowledge (ADR-0033 *Catalogue texts*, ADR-0036's #427
amendment, PRD B7, G5).

Each language is one source, "Exercise catalogue", with one chunk per exercise: the exercise's
`instructions`, under its name, followed by its target and equipment (#508). The worker embeds
only a text whose chunk is missing, differs, or was embedded by another model, so a run with
nothing changed embeds nothing.
"""

import hashlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from uuid import UUID

from ai_trainer.domain.knowledge import SourceLanguage
from ai_trainer.domain.knowledge_document import NewKnowledgeChunk, SectionKind, SectionLocator

CATALOGUE_TITLE = "Exercise catalogue"
# The dataset's licence (ADR-0031 G10); `data/catalogue/LICENSE.exercises-dataset` keeps its
# notice. `other`, since MIT isn't one of ADR-0036's CC codes.
CATALOGUE_LICENSE_NOTE = "MIT licence (data/catalogue/LICENSE.exercises-dataset)"


def catalogue_content_hash(language: SourceLanguage) -> str:
    """The catalogue source's key: unique per language, and never a file's hash in practice."""
    return hashlib.sha256(f"catalogue:{language.value}".encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class CatalogueText:
    """One exercise's imported text in the source's language, with the catalogue row's target
    and equipment, which are the same in every language."""

    exercise_id: UUID
    name: str
    instructions: str
    target: str | None = None
    equipment: str | None = None


@dataclass(frozen=True, slots=True)
class CatalogueChunkState:
    """What a stored catalogue chunk holds, without its vector."""

    exercise_id: UUID
    text: str
    heading_path: tuple[str, ...]
    embedding_model: str


@dataclass(frozen=True, slots=True)
class CatalogueChunk:
    """A chunk to embed, with the exercise it belongs to."""

    exercise_id: UUID
    chunk: NewKnowledgeChunk


@dataclass(frozen=True, slots=True)
class EmbeddedCatalogueChunk:
    item: CatalogueChunk
    embedding: tuple[float, ...]


def catalogue_chunk(text: CatalogueText) -> CatalogueChunk:
    """The instructions, then a line with the exercise's target and equipment where it has them:
    a paraphrase names the movement, and these place it among its look-alikes (#508). The
    ordinal is a placeholder: the repository keeps a replaced chunk's own and gives a new one
    the next free number."""
    facts = " ".join(
        f"{label}: {value}."
        for label, value in (("Target", text.target), ("Equipment", text.equipment))
        if value
    )
    body = text.instructions.strip()
    return CatalogueChunk(
        exercise_id=text.exercise_id,
        chunk=NewKnowledgeChunk(
            ordinal=0,
            text=f"{body}\n\n{facts}" if facts else body,
            heading_path=(text.name,),
            section_kind=SectionKind.BODY,
            locator=SectionLocator(section=text.name),
        ),
    )


def stale_chunks(
    texts: Iterable[CatalogueText], stored: Iterable[CatalogueChunkState], *, model: str
) -> list[CatalogueChunk]:
    """The chunks to embed, in the texts' order: an exercise with no chunk, a chunk whose text or
    name changed, or one another model embedded. A blank text has nothing to embed."""
    current = {state.exercise_id: state for state in stored}
    wanted = [catalogue_chunk(text) for text in texts if text.instructions.strip()]
    return [
        wanted_chunk
        for wanted_chunk in wanted
        if not _matches(current.get(wanted_chunk.exercise_id), wanted_chunk, model)
    ]


def has_changed_text(texts: Iterable[CatalogueText], stored: Iterable[CatalogueChunkState]) -> bool:
    """Whether any text has no chunk, or one whose text or heading differs from what
    `catalogue_chunk` makes of it now: what makes `import-exercises` queue the source. A model
    change isn't one; it is a re-embed job (ADR-0033 invariant 4)."""
    current = {state.exercise_id: state for state in stored}
    return any(
        not _same_text(current.get(text.exercise_id), catalogue_chunk(text))
        for text in texts
        if text.instructions.strip()
    )


def _same_text(state: CatalogueChunkState | None, wanted: CatalogueChunk) -> bool:
    return (
        state is not None
        and state.text == wanted.chunk.text
        and state.heading_path == wanted.chunk.heading_path
    )


def _matches(state: CatalogueChunkState | None, wanted: CatalogueChunk, model: str) -> bool:
    return state is not None and state.embedding_model == model and _same_text(state, wanted)


def kept_exercises(texts: Sequence[CatalogueText]) -> frozenset[UUID]:
    """The exercises whose chunk stays; any other chunk in the source is removed at the end."""
    return frozenset(text.exercise_id for text in texts if text.instructions.strip())
