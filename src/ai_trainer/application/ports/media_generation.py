"""What generating one exercise picture reads and calls (ADR-0031 *Picture generation*, #533)."""

from typing import Protocol
from uuid import UUID

from ai_trainer.domain.knowledge_search import (
    KnowledgeSearchFailed,
    KnowledgeSearchPage,
    KnowledgeSearchQuery,
)
from ai_trainer.domain.media_generation import (
    AmbiguousExerciseName,
    BriefFailed,
    GeneratedImage,
    ImageGenerationFailed,
    NoBrief,
    PictureBrief,
    PictureBriefDeps,
    PictureSubject,
)


class ImageGenerationPort(Protocol):
    """An image model (ADR-0031). It never raises for a model or provider failure, and writes
    one `llm_calls` row per call under `user_id`; the prompt never reaches that row."""

    async def generate(
        self, *, user_id: UUID, prompt: str
    ) -> GeneratedImage | ImageGenerationFailed: ...


class PictureBriefWriterPort(Protocol):
    """`media.picture_brief` through the text gateway (#532). It never raises for a model or
    provider failure."""

    async def write(
        self, *, user_id: UUID, deps: PictureBriefDeps
    ) -> PictureBrief | NoBrief | BriefFailed: ...


class PictureSubjectsPort(Protocol):
    """The shared catalogue, read by exact name. It holds no user data."""

    async def catalogue_exercise(
        self, name: str, *, lang: str
    ) -> PictureSubject | AmbiguousExerciseName | None:
        """The catalogue exercise named `name`, compared case-insensitively, with its text in
        `lang`, else in any language; `AmbiguousExerciseName` when the name is in more than one
        sport. An athlete's own exercise is never returned."""
        ...


class PassageSearchPort(Protocol):
    """`search_knowledge`'s use case, as the picture's description source (ADR-0031)."""

    async def search(
        self, user_id: UUID, query: KnowledgeSearchQuery
    ) -> KnowledgeSearchPage | KnowledgeSearchFailed: ...
