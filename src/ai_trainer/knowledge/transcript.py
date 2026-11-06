"""The VTT and SRT readers (ADR-0036, #426): webvtt-py parses the cues, and consecutive cues are
joined into windows that end where a cue ends a sentence.

- A window's block is a `CUE` with a locator from its first cue's start to its last cue's end, in
  whole seconds (webvtt-py rounds down), so a cited passage can be played from its timestamp.
- Captions with no punctuation, as auto-generated ones often are, are cut at a cue boundary once
  a window reaches `_MAX_WINDOW_WORDS`, so a window never grows without bound. The chunker then
  packs windows up to its own word budget and splits only a window over it.
- Cue tags (`<v Coach>`, `<c.yellow>`, `<i>`) are dropped and entities (`&amp;`, `&nbsp;`)
  unescaped; a cue with no text is skipped.
- Rolling captions, as YouTube writes them, repeat the last cue's text in the next one: a
  line the last cue already had, or a cue that starts with the last cue's whole text, adds
  only what is new.

webvtt-py (MIT) reads both formats from memory; pysrt is GPLv3 and isn't used (ADR-0036).
"""

import html
import re
from collections.abc import Callable, Iterator, Sequence
from io import StringIO
from typing import Protocol

import webvtt
from webvtt.errors import MalformedCaptionError, MalformedFileError

from ai_trainer.domain.knowledge_document import (
    Block,
    BlockKind,
    CueLocator,
    KnowledgeDocument,
    Unreadable,
    UnreadableReason,
)
from ai_trainer.knowledge.text_decoding import decode_text

_MAX_WINDOW_WORDS = 120
# A sentence end, possibly followed by a closing quote or bracket.
_SENTENCE_END = re.compile(r"[.!?…][\"'\u201d\u2019»)\]]*$")
_TAG = re.compile(r"<[^>]*>")


class _Cue(Protocol):
    """What this module reads from a `webvtt.Caption`, which the library leaves untyped."""

    @property
    def raw_text(self) -> str: ...

    @property
    def start_in_seconds(self) -> int: ...

    @property
    def end_in_seconds(self) -> int: ...


type _Parse = Callable[[str], Sequence[_Cue]]


def _parse_vtt(text: str) -> Sequence[_Cue]:
    return [cue for cue in webvtt.from_string(text).captions]


def _parse_srt(text: str) -> Sequence[_Cue]:
    return [cue for cue in webvtt.from_buffer(StringIO(text), format="srt").captions]


def _window(texts: list[str], start: int, end: int) -> Block:
    return Block(
        kind=BlockKind.CUE,
        text=" ".join(texts),
        locator=CueLocator(start_seconds=start, end_seconds=end),
    )


def _lines(caption: _Cue) -> list[str]:
    plain = html.unescape(_TAG.sub("", caption.raw_text))
    return [line for line in (" ".join(raw.split()) for raw in plain.splitlines()) if line]


def _new_cues(captions: Sequence[_Cue]) -> Iterator[tuple[_Cue, str]]:
    """Each cue with the text it adds to the one before it."""
    previous: list[str] = []
    for caption in captions:
        lines = _lines(caption)
        if not lines:
            continue
        fresh = " ".join(line for line in lines if line not in previous)
        shown = " ".join(previous)
        if shown and fresh.startswith(f"{shown} "):
            fresh = fresh[len(shown) + 1 :]
        previous = lines
        if fresh:
            yield caption, fresh


def _windows(captions: Sequence[_Cue]) -> Iterator[Block]:
    texts: list[str] = []
    words = 0
    start = end = 0
    for caption, text in _new_cues(captions):
        if not texts:
            start = caption.start_in_seconds
        texts.append(text)
        words += len(text.split())
        end = caption.end_in_seconds
        if _SENTENCE_END.search(text) or words >= _MAX_WINDOW_WORDS:
            yield _window(texts, start, end)
            texts, words = [], 0
    if texts:
        yield _window(texts, start, end)


def _read(data: bytes, parse: _Parse) -> KnowledgeDocument | Unreadable:
    text = decode_text(data)
    if text is None:
        return Unreadable(reason=UnreadableReason.NOT_UTF8)
    try:
        captions = parse(text)
    except MalformedFileError, MalformedCaptionError, ValueError:
        return Unreadable(reason=UnreadableReason.CORRUPT)
    blocks = tuple(_windows(captions))
    if not blocks:
        return Unreadable(reason=UnreadableReason.NO_TEXT)
    return KnowledgeDocument(blocks=blocks)


def read_vtt(data: bytes) -> KnowledgeDocument | Unreadable:
    return _read(data, _parse_vtt)


def read_srt(data: bytes) -> KnowledgeDocument | Unreadable:
    return _read(data, _parse_srt)
