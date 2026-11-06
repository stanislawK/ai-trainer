"""The EPUB reader (#426, ADR-0036): spine order, chapter titles from the navigation document,
blocks with heading paths, and a reason for a file it can't read."""

from collections.abc import Callable
from typing import Literal

import pytest
from lxml import etree

from ai_trainer.domain.knowledge import KnowledgeFileType
from ai_trainer.domain.knowledge_document import (
    Block,
    BlockKind,
    KnowledgeDocument,
    SpineLocator,
    Unreadable,
    UnreadableReason,
)
from ai_trainer.knowledge import epub as epub_module
from ai_trainer.knowledge import xhtml as xhtml_module
from ai_trainer.knowledge.epub import _nav_entries, _ncx_entries, read_epub
from ai_trainer.knowledge.readers import DocumentReaders
from tests.epub_support import (
    ADOBE_FONT_OBFUSCATION,
    AES_ALGORITHM,
    IDPF_FONT_OBFUSCATION,
    Chapter,
    encryption_xml,
    epub,
    with_files,
)

_PARAGRAPH = "Deload weeks let the body absorb the work of the hard blocks before them."
_ONE = Chapter("ch1.xhtml", "Warm-up", f"<h1>Warm-up</h1><p>{_PARAGRAPH}</p>")
_TWO = Chapter(
    "ch2.xhtml", "Recovery", "<h1>Recovery</h1><p>Sleep is where adaptation happens.</p>"
)


def _document(data: bytes) -> KnowledgeDocument:
    document = read_epub(data)
    assert isinstance(document, KnowledgeDocument)
    return document


def _unreadable(data: bytes) -> UnreadableReason:
    result = read_epub(data)
    assert isinstance(result, Unreadable)
    return result.reason


def _body(document: KnowledgeDocument) -> list[Block]:
    return [b for b in document.blocks if b.kind is not BlockKind.HEADING]


def test_blocks_follow_the_spine_not_the_manifest() -> None:
    document = _document(epub([_ONE, _TWO], spine=["c1", "c0"]))

    assert [b.text for b in _body(document)] == [
        "Sleep is where adaptation happens.",
        _PARAGRAPH,
    ]


def test_a_block_records_its_spine_item_and_the_chapter_from_the_navigation_document() -> None:
    document = _document(epub([_ONE, _TWO]))

    assert [b.locator for b in _body(document)] == [
        SpineLocator(spine_item="OEBPS/ch1.xhtml", chapter="Warm-up"),
        SpineLocator(spine_item="OEBPS/ch2.xhtml", chapter="Recovery"),
    ]


def test_an_epub2_book_takes_its_chapter_titles_from_the_ncx() -> None:
    document = _document(epub([_ONE, _TWO], nav="ncx"))

    assert [getattr(b.locator, "chapter", None) for b in _body(document)] == [
        "Warm-up",
        "Recovery",
    ]


def test_a_chapter_missing_from_the_contents_takes_its_first_heading() -> None:
    untitled = Chapter("ch3.xhtml", None, "<h2>Mobility drills</h2><p>Ten minutes a day.</p>")

    document = _document(epub([_ONE, untitled]))

    assert getattr(_body(document)[-1].locator, "chapter", None) == "Mobility drills"


def test_a_chapter_with_no_title_anywhere_has_none() -> None:
    bare = Chapter("ch3.xhtml", None, "<p>Just a note on the back of the book.</p>")

    document = _document(epub([bare], nav="none"))

    assert _body(document)[0].locator == SpineLocator(spine_item="OEBPS/ch3.xhtml", chapter=None)


def test_a_navigation_title_is_the_one_for_the_whole_file_not_its_fragments() -> None:
    chapter = Chapter("ch1.xhtml", "Warm-up", "<p>Text.</p>")
    # The same file listed again under a section anchor must not replace the chapter's title.
    data = epub(
        [chapter],
        nav_content=(
            '<html xmlns:epub="http://www.idpf.org/2007/ops"><body><nav epub:type="toc"><ol>'
            '<li><a href="ch1.xhtml">Warm-up</a></li>'
            '<li><a href="ch1.xhtml#s2">Section two</a></li></ol></nav></body></html>'
        ),
    )

    document = _document(data)

    assert getattr(_body(document)[0].locator, "chapter", None) == "Warm-up"


def test_headings_make_the_heading_path_and_a_lower_level_closes_a_higher_one() -> None:
    chapter = Chapter(
        "ch1.xhtml",
        "Training",
        "<h1>Training</h1><p>Intro.</p><h2>Volume</h2><p>Sets.</p><h3>Sets per week</h3>"
        "<p>Ten.</p><h2>Intensity</h2><p>Load.</p>",
    )

    document = _document(epub([chapter]))

    assert [(b.kind, b.heading_path, b.text) for b in document.blocks] == [
        (BlockKind.HEADING, ("Training",), "Training"),
        (BlockKind.PARAGRAPH, ("Training",), "Intro."),
        (BlockKind.HEADING, ("Training", "Volume"), "Volume"),
        (BlockKind.PARAGRAPH, ("Training", "Volume"), "Sets."),
        (BlockKind.HEADING, ("Training", "Volume", "Sets per week"), "Sets per week"),
        (BlockKind.PARAGRAPH, ("Training", "Volume", "Sets per week"), "Ten."),
        (BlockKind.HEADING, ("Training", "Intensity"), "Intensity"),
        (BlockKind.PARAGRAPH, ("Training", "Intensity"), "Load."),
    ]


def test_text_before_any_heading_sits_under_the_chapter_title() -> None:
    chapter = Chapter("ch1.xhtml", "Warm-up", "<p>Before the first heading.</p>")

    document = _document(epub([chapter]))

    assert _body(document)[0].heading_path == ("Warm-up",)


def test_each_file_starts_its_own_heading_path() -> None:
    document = _document(
        epub(
            [
                Chapter("ch1.xhtml", "One", "<h1>One</h1><h2>Deep</h2><p>a</p>"),
                Chapter("ch2.xhtml", "Two", "<p>b</p>"),
            ]
        )
    )

    assert [b.heading_path for b in _body(document)] == [("One", "Deep"), ("Two",)]


def test_lists_tables_and_captions_are_blocks_of_their_own() -> None:
    chapter = Chapter(
        "ch1.xhtml",
        "Plan",
        "<h1>Plan</h1><ul><li>Squat</li><li>Press</li></ul>"
        "<table><caption>Week one</caption><tr><th>Day</th><th>Lift</th></tr>"
        "<tr><td>Mon</td><td>Squat</td></tr><tr><td> </td></tr></table>"
        "<figure><img src='x.png' alt=''/><figcaption>A squat rack.</figcaption></figure>",
    )

    document = _document(epub([chapter]))

    assert [(b.kind, b.text) for b in _body(document)] == [
        (BlockKind.LIST, "Squat\nPress"),
        (BlockKind.TABLE, "Day | Lift\nMon | Squat"),
        (BlockKind.CAPTION, "Week one"),
        (BlockKind.CAPTION, "A squat rack."),
    ]


def test_scripts_styles_and_the_contents_list_are_not_text() -> None:
    chapter = Chapter(
        "ch1.xhtml",
        "Plan",
        '<script>var secret = 1;</script><nav epub:type="toc"><ol><li>Skip me</li></ol></nav>'
        "<p>Real text.</p>",
    )

    document = _document(epub([chapter]))

    assert [b.text for b in document.blocks] == ["Real text."]


def test_whitespace_inside_a_paragraph_is_squashed_and_inline_tags_are_text() -> None:
    chapter = Chapter("ch1.xhtml", "C", "<p>Train  <em>hard</em>,\n   <b>recover</b> harder.</p>")

    assert [b.text for b in _document(epub([chapter])).blocks] == ["Train hard, recover harder."]


def test_html_entities_and_sloppy_markup_are_recovered() -> None:
    chapter = Chapter("ch1.xhtml", "C", "<p>Rest&nbsp;days &amp; sleep<br><p>Second paragraph.")

    texts = [b.text for b in _document(epub([chapter])).blocks]

    assert texts[0].startswith("Rest")
    assert "sleep" in texts[0]
    assert texts[-1] == "Second paragraph."


def test_an_external_entity_is_not_resolved() -> None:
    xxe = (
        '<?xml version="1.0"?><!DOCTYPE html [<!ENTITY x SYSTEM "file:///etc/hosts">]>'
        '<html xmlns="http://www.w3.org/1999/xhtml"><body><p>Before &x; after.</p></body></html>'
    )
    data = with_files(epub([_ONE]), {"OEBPS/ch1.xhtml": xxe.encode()})

    text = " ".join(b.text for b in _document(data).blocks)

    assert "localhost" not in text
    assert "Before" in text


@pytest.mark.parametrize(
    "content", [b"\x00\x01\x02 not markup at all", b""], ids=["binary", "empty"]
)
def test_a_spine_item_that_will_not_parse_is_skipped_and_the_rest_is_read(content: bytes) -> None:
    data = with_files(epub([_ONE, _TWO]), {"OEBPS/ch1.xhtml": content})

    document = _document(data)

    assert [b.text for b in _body(document)] == ["Sleep is where adaptation happens."]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"rights": True},
        {"encryption": encryption_xml((AES_ALGORITHM, "OEBPS/ch1.xhtml"))},
    ],
    ids=["adobe-rights", "encrypted-content"],
)
def test_a_drm_protected_book_is_reported_as_encrypted(kwargs: dict[str, bool | str]) -> None:
    protected = epub(
        [_ONE],
        rights=kwargs.get("rights") is True,
        encryption=str(kwargs["encryption"]) if "encryption" in kwargs else None,
    )

    assert _unreadable(protected) is UnreadableReason.ENCRYPTED


@pytest.mark.parametrize(
    "algorithm", [IDPF_FONT_OBFUSCATION, ADOBE_FONT_OBFUSCATION], ids=["idpf", "adobe"]
)
def test_font_obfuscation_is_not_drm(algorithm: str) -> None:
    data = epub([_ONE], encryption=encryption_xml((algorithm, "OEBPS/font.otf")))

    assert _body(_document(data))[0].text == _PARAGRAPH


def test_a_book_with_an_obfuscated_font_and_an_encrypted_chapter_is_protected() -> None:
    mixed = encryption_xml(
        (IDPF_FONT_OBFUSCATION, "OEBPS/font.otf"), (AES_ALGORITHM, "OEBPS/ch1.xhtml")
    )

    assert _unreadable(epub([_ONE], encryption=mixed)) is UnreadableReason.ENCRYPTED


def test_an_encryption_file_that_is_not_xml_is_reported_as_encrypted() -> None:
    assert _unreadable(epub([_ONE], encryption="<not xml")) is UnreadableReason.ENCRYPTED


@pytest.mark.parametrize(
    "data",
    [
        b"definitely not a zip file",
        b"",
        epub([_ONE], container=None),
        epub([_ONE], container="<container"),
        epub([_ONE], opf="<package"),
        epub([_ONE], opf='<package xmlns="http://www.idpf.org/2007/opf"><spine/></package>'),
        epub([_ONE], spine=["missing"]),
    ],
    ids=[
        "not-a-zip",
        "empty",
        "no-container",
        "broken-container",
        "broken-package",
        "empty-spine",
        "dangling-spine",
    ],
)
def test_a_damaged_book_is_reported_as_corrupt(data: bytes) -> None:
    assert _unreadable(data) is UnreadableReason.CORRUPT


def test_a_book_whose_files_hold_no_text_has_no_text() -> None:
    empty = Chapter("ch1.xhtml", "Blank", "<h1>   </h1><p> </p><img src='x.png'/>")

    assert _unreadable(epub([empty])) is UnreadableReason.NO_TEXT


def test_a_file_larger_than_the_cap_once_unpacked_is_corrupt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(epub_module, "_MAX_MEMBER_BYTES", 200)
    big = Chapter("ch1.xhtml", "Big", f"<p>{'word ' * 200}</p>")

    assert _unreadable(epub([big])) is UnreadableReason.CORRUPT


def test_the_files_together_are_capped_too(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(epub_module, "_MAX_TOTAL_BYTES", 700)

    assert _unreadable(epub([_ONE, _TWO])) is UnreadableReason.CORRUPT


def test_a_password_protected_zip_is_reported_as_encrypted() -> None:
    assert _unreadable(epub([_ONE], locked=True)) is UnreadableReason.ENCRYPTED


def test_a_container_that_names_no_package_is_corrupt() -> None:
    container = (
        '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles/>'
        "</container>"
    )

    assert _unreadable(epub([_ONE], container=container)) is UnreadableReason.CORRUPT


@pytest.mark.parametrize("kind", ["nav", "ncx"])
@pytest.mark.parametrize("nav_content", ["", "<ncx", "<nav>"], ids=["empty", "broken", "stray-tag"])
def test_contents_that_will_not_read_only_cost_the_titles(
    kind: Literal["nav", "ncx"], nav_content: str
) -> None:
    document = _document(epub([_ONE, _TWO], nav=kind, nav_content=nav_content))

    # No titles from the contents, so each chapter takes its first heading.
    assert [getattr(b.locator, "chapter", None) for b in _body(document)] == [
        "Warm-up",
        "Recovery",
    ]


def test_a_page_the_parser_gives_up_on_costs_that_page_not_the_book(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(data: bytes) -> None:
        raise etree.ParserError("refused")

    monkeypatch.setattr(xhtml_module, "parse_html", refuse)

    assert _unreadable(epub([_ONE])) is UnreadableReason.NO_TEXT


def test_navigation_links_without_a_target_or_a_title_are_ignored() -> None:
    nav = (
        b'<nav epub:type="toc"><ol><li><a>No target</a></li>'
        b'<li><a href="c1.xhtml"> </a></li><li><a href="c2.xhtml">Chapter two</a></li>'
        b"</ol></nav>"
    )

    assert _nav_entries(nav, "OEBPS/nav.xhtml") == [("OEBPS/c2.xhtml", "Chapter two")]
    assert _nav_entries(b"", "OEBPS/nav.xhtml") == []


def test_ncx_points_without_a_label_or_a_target_are_ignored() -> None:
    ncx = (
        b'<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/"><navMap>'
        b"<navPoint><navLabel><text>No target</text></navLabel></navPoint>"
        b'<navPoint><content src="c1.xhtml"/></navPoint>'
        b'<navPoint><navLabel><text>Two</text></navLabel><content src="c2.xhtml"/></navPoint>'
        b"</navMap></ncx>"
    )

    assert _ncx_entries(ncx, "OEBPS/toc.ncx") == [("OEBPS/c2.xhtml", "Two")]


def test_the_readers_hand_an_epub_to_this_reader() -> None:
    result = DocumentReaders().parse(KnowledgeFileType.EPUB, epub([_ONE]))

    assert isinstance(result, KnowledgeDocument)


def test_a_table_without_a_caption_is_a_table_block_alone() -> None:
    chapter = Chapter("ch1.xhtml", "Plan", "<table><tr><td>Mon</td><td>Squat</td></tr></table>")

    assert [(b.kind, b.text) for b in _document(epub([chapter])).blocks] == [
        (BlockKind.TABLE, "Mon | Squat")
    ]


def test_a_line_break_keeps_the_words_either_side_apart() -> None:
    chapter = Chapter(
        "ch1.xhtml",
        "Poems",
        "<p>Line one<br/>Line two</p><ul><li>Squat<br>deep</li><li>Press</li></ul>"
        "<table><tr><td>A</td><td>B<br/>C</td></tr></table>",
    )

    assert [(b.kind, b.text) for b in _document(epub([chapter])).blocks] == [
        (BlockKind.PARAGRAPH, "Line one Line two"),
        (BlockKind.LIST, "Squat deep\nPress"),
        (BlockKind.TABLE, "A | B C"),
    ]


def test_a_nested_table_is_read_once_inside_its_cell() -> None:
    chapter = Chapter(
        "ch1.xhtml",
        "Plan",
        "<table><tr><td>Outer</td><td><table><tr><td>A</td><td>B</td></tr></table></td></tr></table>",
    )

    assert [(b.kind, b.text) for b in _document(epub([chapter])).blocks] == [
        (BlockKind.TABLE, "Outer | A B")
    ]


@pytest.mark.parametrize(
    "encode",
    [
        lambda text: b"\xef\xbb\xbf" + text.encode("utf-8"),
        lambda text: text.encode("utf-16"),
        lambda text: b"\xfe\xff" + text.encode("utf-16-be"),
    ],
    ids=["utf8-bom", "utf16-le", "utf16-be"],
)
def test_a_spine_item_with_a_byte_order_mark_or_in_utf16_is_read(
    encode: Callable[[str], bytes],
) -> None:
    page = "<html><body><p>Zażółć gęślą jaźń.</p></body></html>"
    data = with_files(epub([_ONE]), {"OEBPS/ch1.xhtml": encode(page)})

    assert [b.text for b in _document(data).blocks] == ["Zażółć gęślą jaźń."]


def test_an_item_the_spine_lists_twice_is_read_once() -> None:
    document = _document(epub([_ONE, _TWO], spine=["c0", "c1", "c0"]))

    assert [b.text for b in _body(document)] == [
        _PARAGRAPH,
        "Sleep is where adaptation happens.",
    ]


@pytest.mark.parametrize("kind", ["nav", "ncx"])
def test_a_file_the_contents_list_more_than_once_gives_each_block_its_own_chapter(
    kind: Literal["nav", "ncx"],
) -> None:
    book = Chapter(
        "book.xhtml",
        None,
        "<p>Preface text.</p><h1>Warm-up</h1><p>Warm up first.</p><h2>Sets</h2><p>Light sets.</p>"
        "<h1>Recovery</h1><p>Sleep well.</p>",
    )
    nav_content = (
        '<html xmlns:epub="http://www.idpf.org/2007/ops"><body><nav epub:type="toc"><ol>'
        '<li><a href="book.xhtml#a">Part A</a></li><li><a href="book.xhtml#b">Part B</a></li>'
        "</ol></nav></body></html>"
        if kind == "nav"
        else '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/"><navMap>'
        '<navPoint><navLabel><text>Part A</text></navLabel><content src="book.xhtml#a"/></navPoint>'
        '<navPoint><navLabel><text>Part B</text></navLabel><content src="book.xhtml#b"/></navPoint>'
        "</navMap></ncx>"
    )

    document = _document(epub([book], nav=kind, nav_content=nav_content))

    assert [(b.text, getattr(b.locator, "chapter", None)) for b in _body(document)] == [
        ("Preface text.", "Part A"),
        ("Warm up first.", "Warm-up"),
        ("Light sets.", "Warm-up"),
        ("Sleep well.", "Recovery"),
    ]
