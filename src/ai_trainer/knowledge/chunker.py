"""The one chunker every reader's document goes through (ADR-0036, research
knowledge-ingestion-at-scale.md recs 9-10, knowledge-base.md rec 5).

- Blocks are grouped into sections by heading path.
- One word budget: a section's blocks are packed up to it, and only a block over it is split, on
  sentence ends and then on words.
- Small sibling sections merge under their common parent, each keeping its heading as a line.
- The abstract is its own chunk, never merged. A table stays whole with the caption after it,
  whatever its size.
- A references section is not embedded.

The budget is counted in words, since the app has no tokenizer for the embedding model; about
300 words is a few hundred tokens. The sizes are provisional until `search_knowledge`'s
retrieval checks settle them (#424).
"""

import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from itertools import groupby

from ai_trainer.domain.knowledge_document import (
    Block,
    BlockKind,
    KnowledgeDocument,
    Locator,
    NewKnowledgeChunk,
    SectionKind,
    word_count,
)

# Headings that mark a section, in English and Polish (PRD G5), compared case-insensitively.
_ABSTRACT = frozenset({"abstract", "streszczenie"})
_REFERENCES = frozenset(
    {"references", "bibliography", "works cited", "literatura", "bibliografia", "piśmiennictwo"}
)
_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+")


@dataclass(frozen=True)
class _Draft:
    text: str
    heading_path: tuple[str, ...]
    kind: SectionKind
    locator: Locator


@dataclass(frozen=True)
class _Unit:
    text: str
    # Joined to the previous unit of the same block with a space, otherwise a blank line.
    continues: bool


def _names(path: Sequence[str]) -> set[str]:
    return {heading.casefold() for heading in path}


class Chunker:
    """Implements `ChunkerPort`."""

    def __init__(self, *, max_words: int, min_words: int) -> None:
        self._max = max_words
        self._min = min_words

    def chunk(self, document: KnowledgeDocument) -> tuple[NewKnowledgeChunk, ...]:
        drafts = [*self._drafts(document)]
        return tuple(
            NewKnowledgeChunk(
                ordinal=ordinal,
                text=draft.text,
                heading_path=draft.heading_path,
                section_kind=draft.kind,
                locator=draft.locator,
            )
            for ordinal, draft in enumerate(self._merge_siblings(drafts))
        )

    def _drafts(self, document: KnowledgeDocument) -> Iterator[tuple[_Draft, bool]]:
        """Each section's drafts, with whether it may merge with a small sibling."""
        body = (block for block in document.blocks if block.kind is not BlockKind.HEADING)
        for path, section in groupby(body, key=lambda block: block.heading_path):
            names = _names(path)
            if names & _REFERENCES:
                continue
            kind = SectionKind.ABSTRACT if names & _ABSTRACT else SectionKind.BODY
            drafts = [*self._section(path, kind, [*section])]
            mergeable = (
                len(drafts) == 1
                and kind is SectionKind.BODY
                and bool(path)
                and word_count(drafts[0].text) < self._min
            )
            for draft in drafts:
                yield draft, mergeable

    def _section(
        self, path: tuple[str, ...], kind: SectionKind, blocks: list[Block]
    ) -> Iterator[_Draft]:
        run: list[Block] = []
        at = 0
        while at < len(blocks):
            block = blocks[at]
            if block.kind is BlockKind.TABLE:
                yield from self._pack(path, kind, run)
                run = []
                caption = blocks[at + 1] if at + 1 < len(blocks) else None
                texts = [block.text]
                if caption is not None and caption.kind is BlockKind.CAPTION:
                    texts.append(caption.text)
                    at += 1
                yield _Draft("\n\n".join(texts), path, SectionKind.TABLE, block.locator)
            else:
                run.append(block)
            at += 1
        yield from self._pack(path, kind, run)

    def _pack(
        self, path: tuple[str, ...], kind: SectionKind, blocks: list[Block]
    ) -> Iterator[_Draft]:
        texts: list[str] = []
        words = 0
        locator: Locator | None = None
        for block in blocks:
            for unit in self._units(block.text):
                size = word_count(unit.text)
                if texts and words + size > self._max:
                    assert locator is not None
                    yield _Draft("".join(texts), path, kind, locator)
                    texts, words, locator = [], 0, None
                if texts:
                    texts.append(" " if unit.continues else "\n\n")
                texts.append(unit.text)
                words += size
                locator = locator or block.locator
        if texts:
            assert locator is not None
            yield _Draft("".join(texts), path, kind, locator)

    def _units(self, text: str) -> Iterator[_Unit]:
        """A block as one unit, or split on sentence ends and then words when over budget."""
        if word_count(text) <= self._max:
            yield _Unit(text, continues=False)
            return
        first = True
        for sentence in _SENTENCE_END.split(text):
            words = sentence.split()
            for start in range(0, len(words), self._max):
                yield _Unit(" ".join(words[start : start + self._max]), continues=not first)
                first = False

    def _merge_siblings(self, drafts: list[tuple[_Draft, bool]]) -> Iterator[_Draft]:
        group: list[_Draft] = []
        for draft, mergeable in drafts:
            if group and not (
                mergeable
                and draft.heading_path[:-1] == group[0].heading_path[:-1]
                and self._merged_words([*group, draft]) <= self._max
            ):
                yield self._merged(group)
                group = []
            if mergeable:
                group.append(draft)
            else:
                yield draft
        if group:
            yield self._merged(group)

    @staticmethod
    def _labelled(draft: _Draft) -> str:
        return f"{draft.heading_path[-1]}\n\n{draft.text}"

    def _merged_words(self, group: list[_Draft]) -> int:
        return sum(word_count(self._labelled(draft)) for draft in group)

    def _merged(self, group: list[_Draft]) -> _Draft:
        if len(group) == 1:
            return group[0]
        first = group[0]
        text = "\n\n".join(self._labelled(draft) for draft in group)
        return _Draft(text, first.heading_path[:-1], first.kind, first.locator)
