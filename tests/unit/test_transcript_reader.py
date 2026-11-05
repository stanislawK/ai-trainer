"""The VTT and SRT readers (#426, ADR-0036): cues joined into windows that end on a sentence,
each window's start and end in seconds as the locator, and a reason for a file they can't read."""

from collections.abc import Callable

import pytest

from ai_trainer.domain.knowledge import KnowledgeFileType
from ai_trainer.domain.knowledge_document import (
    BlockKind,
    CueLocator,
    KnowledgeDocument,
    Unreadable,
    UnreadableReason,
)
from ai_trainer.knowledge.readers import DocumentReaders
from ai_trainer.knowledge.transcript import read_srt, read_vtt

_VTT = """WEBVTT

NOTE a comment the reader must ignore

00:00:01.000 --> 00:00:03.500
Welcome back to the show.

00:00:03.500 --> 00:00:06.000
Today we talk about
progressive overload

00:00:06.000 --> 00:00:09.250
and why it matters. Do you train

00:01:10.000 --> 00:01:12.000
every day? Not necessarily!
"""

_SRT = """1
00:00:01,000 --> 00:00:03,500
Welcome back to the show.

2
00:00:03,500 --> 00:00:06,000
Today we talk about
progressive overload

3
00:00:06,000 --> 00:00:09,250
and why it matters.
"""


def _document(result: KnowledgeDocument | Unreadable) -> KnowledgeDocument:
    assert isinstance(result, KnowledgeDocument)
    return result


def _reason(result: KnowledgeDocument | Unreadable) -> UnreadableReason:
    assert isinstance(result, Unreadable)
    return result.reason


def test_cues_are_joined_until_a_sentence_ends() -> None:
    document = _document(read_vtt(_VTT.encode()))

    # A window ends at a cue boundary where the text ends a sentence, so the third cue, which
    # stops mid-sentence ("Do you train"), carries on into the fourth.
    assert [b.text for b in document.blocks] == [
        "Welcome back to the show.",
        "Today we talk about progressive overload and why it matters. "
        "Do you train every day? Not necessarily!",
    ]


def test_a_window_runs_from_its_first_cues_start_to_its_last_cues_end() -> None:
    document = _document(read_vtt(_VTT.encode()))

    assert [(b.kind, b.locator) for b in document.blocks] == [
        (BlockKind.CUE, CueLocator(start_seconds=1, end_seconds=3)),
        (BlockKind.CUE, CueLocator(start_seconds=3, end_seconds=72)),
    ]


def test_timestamps_past_an_hour_are_seconds() -> None:
    vtt = "WEBVTT\n\n01:02:03.500 --> 01:02:05.000\nAn hour in.\n"

    block = _document(read_vtt(vtt.encode())).blocks[0]

    assert block.locator == CueLocator(start_seconds=3723, end_seconds=3725)


def test_cue_tags_and_voice_labels_are_dropped_from_the_text() -> None:
    vtt = (
        "WEBVTT\n\n00:00:01.000 --> 00:00:03.000\n"
        "<v Coach>Keep the <c.yellow>bar</c> close</v> to you.\n"
    )

    assert _document(read_vtt(vtt.encode())).blocks[0].text == "Keep the bar close to you."


def test_a_sentence_end_may_be_followed_by_a_closing_quote_or_bracket() -> None:
    vtt = (
        "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nShe said “stop.”\n\n"
        "00:00:02.000 --> 00:00:03.000\nThen (we rested.)\n\n"
        "00:00:03.000 --> 00:00:04.000\nNext\n"
    )

    assert [b.text for b in _document(read_vtt(vtt.encode())).blocks] == [
        "She said “stop.”",
        "Then (we rested.)",
        "Next",
    ]


def test_captions_with_no_punctuation_are_cut_at_a_cue_boundary_past_the_word_cap() -> None:
    cues = "".join(
        f"00:00:{n:02d}.000 --> 00:00:{n + 1:02d}.000\nsix words in every single cue\n\n"
        for n in range(40)
    )

    blocks = _document(read_vtt(f"WEBVTT\n\n{cues}".encode())).blocks

    starts = [b.locator.start_seconds for b in blocks if isinstance(b.locator, CueLocator)]
    assert len(blocks) > 1
    assert all(len(b.text.split()) <= 126 for b in blocks)
    assert sum(len(b.text.split()) for b in blocks) == 40 * 6
    assert len(starts) == len(blocks)
    assert starts == sorted(starts)


def test_an_srt_file_reads_the_same_way() -> None:
    document = _document(read_srt(_SRT.encode()))

    assert [(b.text, b.locator) for b in document.blocks] == [
        ("Welcome back to the show.", CueLocator(start_seconds=1, end_seconds=3)),
        (
            "Today we talk about progressive overload and why it matters.",
            CueLocator(start_seconds=3, end_seconds=9),
        ),
    ]


@pytest.mark.parametrize(
    "prefix", [b"\xef\xbb\xbf", b""], ids=["with-byte-order-mark", "without-byte-order-mark"]
)
def test_an_srt_file_with_windows_line_ends_and_a_bom_is_read(prefix: bytes) -> None:
    data = prefix + _SRT.replace("\n", "\r\n").encode()

    assert len(_document(read_srt(data)).blocks) == 2


def test_an_empty_text_cue_is_skipped() -> None:
    vtt = (
        "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n\n\n00:00:02.000 --> 00:00:03.000\nHello there.\n"
    )

    assert [b.text for b in _document(read_vtt(vtt.encode())).blocks] == ["Hello there."]


@pytest.mark.parametrize(
    ("read", "data", "reason"),
    [
        (read_vtt, b"\x89PNG\r\n\x1a\n\x00\x00", UnreadableReason.NOT_UTF8),
        (read_srt, b"\xff\xfe\x00bad", UnreadableReason.NOT_UTF8),
        (read_vtt, b"This is not a transcript at all.\nJust prose.", UnreadableReason.CORRUPT),
        (read_vtt, b"", UnreadableReason.CORRUPT),
        (read_srt, b"This is not a transcript at all.\nJust prose.", UnreadableReason.CORRUPT),
        (read_srt, b"", UnreadableReason.CORRUPT),
        (read_vtt, b"WEBVTT\n\n", UnreadableReason.NO_TEXT),
        (
            read_vtt,
            b"WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n<c></c>\n",
            UnreadableReason.NO_TEXT,
        ),
    ],
    ids=[
        "binary-vtt",
        "binary-srt",
        "prose-as-vtt",
        "empty-vtt",
        "prose-as-srt",
        "empty-srt",
        "header-only",
        "tags-only",
    ],
)
def test_a_file_that_is_not_a_readable_transcript_gives_its_reason(
    read: Callable[[bytes], KnowledgeDocument | Unreadable], data: bytes, reason: UnreadableReason
) -> None:
    assert _reason(read(data)) is reason


def test_the_readers_hand_vtt_and_srt_to_these_readers() -> None:
    readers = DocumentReaders()

    assert isinstance(readers.parse(KnowledgeFileType.VTT, _VTT.encode()), KnowledgeDocument)
    assert isinstance(readers.parse(KnowledgeFileType.SRT, _SRT.encode()), KnowledgeDocument)
