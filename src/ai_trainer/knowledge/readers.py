"""Picks a file's reader by its type (ADR-0036). A type with no reader yet gives `NoReader`, so
the source fails with a reason instead of being ingested empty. PDF (#425), EPUB, VTT and SRT
(#426) and JATS (#463) add theirs here."""

from collections.abc import Callable, Mapping

from ai_trainer.domain.knowledge import KnowledgeFileType
from ai_trainer.domain.knowledge_document import KnowledgeDocument, NoReader, Unreadable
from ai_trainer.knowledge.markdown import read_markdown
from ai_trainer.knowledge.plain_text import read_plain_text

type Reader = Callable[[bytes], KnowledgeDocument | Unreadable]

_READERS: Mapping[KnowledgeFileType, Reader] = {
    KnowledgeFileType.MARKDOWN: read_markdown,
    KnowledgeFileType.TEXT: read_plain_text,
}


class DocumentReaders:
    """Implements `DocumentReadersPort`."""

    def parse(
        self, file_type: KnowledgeFileType, data: bytes
    ) -> KnowledgeDocument | Unreadable | NoReader:
        reader = _READERS.get(file_type)
        return NoReader(file_type=file_type) if reader is None else reader(data)
