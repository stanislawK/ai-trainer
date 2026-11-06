"""Picks a file's reader by its type (ADR-0036). A type with no reader gives `NoReader`, so the
source fails with a reason instead of being ingested empty."""

from collections.abc import Callable, Mapping

from ai_trainer.domain.knowledge import KnowledgeFileType
from ai_trainer.domain.knowledge_document import KnowledgeDocument, NoReader, Unreadable
from ai_trainer.knowledge.epub import read_epub
from ai_trainer.knowledge.jats import read_jats
from ai_trainer.knowledge.markdown import read_markdown
from ai_trainer.knowledge.pdf import read_pdf
from ai_trainer.knowledge.plain_text import read_plain_text
from ai_trainer.knowledge.transcript import read_srt, read_vtt

type Reader = Callable[[bytes], KnowledgeDocument | Unreadable]

_READERS: Mapping[KnowledgeFileType, Reader] = {
    KnowledgeFileType.MARKDOWN: read_markdown,
    KnowledgeFileType.TEXT: read_plain_text,
    KnowledgeFileType.PDF: read_pdf,
    KnowledgeFileType.EPUB: read_epub,
    KnowledgeFileType.VTT: read_vtt,
    KnowledgeFileType.SRT: read_srt,
    KnowledgeFileType.JATS: read_jats,
}


class DocumentReaders:
    """Implements `DocumentReadersPort`."""

    def parse(
        self, file_type: KnowledgeFileType, data: bytes
    ) -> KnowledgeDocument | Unreadable | NoReader:
        reader = _READERS.get(file_type)
        return NoReader(file_type=file_type) if reader is None else reader(data)
