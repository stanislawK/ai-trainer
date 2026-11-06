"""The JATS reader (#463, ADR-0036): sections, the abstract as one chunk, tables whole with their
captions, references left out, and a reason for a file it can't read."""

from pathlib import Path

import pytest

from ai_trainer.domain.knowledge import KnowledgeFileType
from ai_trainer.domain.knowledge_document import (
    BlockKind,
    KnowledgeDocument,
    NewKnowledgeChunk,
    SectionKind,
    SectionLocator,
    Unreadable,
    UnreadableReason,
)
from ai_trainer.knowledge.chunker import Chunker
from ai_trainer.knowledge.jats import read_jats
from ai_trainer.knowledge.readers import DocumentReaders

FIXTURE = Path(__file__).parent.parent / "fixtures" / "pmc" / "PMC13692514.1.xml"
_CHUNKER = Chunker(max_words=300, min_words=20)
_DOI = "10.9999/Test.1"

_ARTICLE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE article PUBLIC "-//NLM//DTD JATS (Z39.96) Archiving v1.4//EN" "JATS-archive1-4.dtd">
<article xmlns:xlink="http://www.w3.org/1999/xlink" xmlns:mml="http://www.w3.org/1998/Math/MathML">
<front><article-meta>
<article-id pub-id-type="pmcid">PMC1</article-id>
<article-id pub-id-type="doi">{doi}</article-id>
<title-group><article-title>Deloading</article-title></title-group>
{abstract}
</article-meta></front>
<body>{body}</body>
<back><ref-list><title>References</title>
<ref id="r1"><mixed-citation>Smith J. Reference list entry one.</mixed-citation></ref></ref-list>
<ack><p>Thanks to the funder.</p></ack></back>
</article>"""

_STRUCTURED_ABSTRACT = """<abstract>
<sec><title>Background</title><p>Fatigue masks fitness in trained adults.</p></sec>
<sec><title>Methods</title><p>We compared two deload patterns over eight weeks.</p></sec>
<sec><title>Results</title><p>Halving the sets kept strength and cut soreness.</p></sec>
</abstract>"""

_BODY = """
<sec><title>Introduction</title>
<p>Deloads are common in practice.<xref ref-type="bibr" rid="r1">[1]</xref></p></sec>
<sec><title>Methods</title>
<sec><title>Design</title><p>Twenty lifters were randomised to two arms.</p></sec>
<sec><p>An untitled sub-section keeps its parent's heading.</p></sec>
</sec>
<sec><title>Results</title>
<p>Both arms kept their one-repetition maximum.</p>
<table-wrap id="t1"><label>Table 1</label>
<caption><p>Weekly volume by arm.</p></caption>
<table><thead><tr><th>Arm</th><th>Sets</th></tr></thead>
<tbody><tr><td>Halved</td><td>6</td></tr><tr><td>Kept</td><td>12</td></tr></tbody></table>
</table-wrap></sec>
"""


def _article(*, abstract: str = _STRUCTURED_ABSTRACT, body: str = _BODY, doi: str = _DOI) -> bytes:
    return _ARTICLE.format(abstract=abstract, body=body, doi=doi).encode()


def _document(data: bytes) -> KnowledgeDocument:
    document = read_jats(data)
    assert isinstance(document, KnowledgeDocument)
    return document


def _chunks(data: bytes) -> tuple[NewKnowledgeChunk, ...]:
    return _CHUNKER.chunk(_document(data))


def _unreadable(data: bytes) -> UnreadableReason:
    result = read_jats(data)
    assert isinstance(result, Unreadable)
    return result.reason


def test_structured_abstract_is_one_abstract_chunk() -> None:
    abstract = [c for c in _chunks(_article()) if c.section_kind is SectionKind.ABSTRACT]

    assert len(abstract) == 1
    assert abstract[0].heading_path == ("Abstract",)
    for lead_in in ("Background:", "Methods:", "Results:"):
        assert lead_in in abstract[0].text
    assert "Halving the sets kept strength" in abstract[0].text


def test_an_abstract_over_the_chunk_budget_is_still_one_chunk() -> None:
    # 400 words against the production budget of 300: the skeptic's case at #463.
    sentence = "Resistance training preserved strength in older adults across the trial. "
    long_abstract = (
        "<abstract>"
        + "".join(f"<sec><title>Part {n}</title><p>{sentence * 6}</p></sec>" for n in range(1, 6))
        + "</abstract>"
    )

    chunks = _chunks(_article(abstract=long_abstract))

    abstract = [c for c in chunks if c.section_kind is SectionKind.ABSTRACT]
    assert len(abstract) == 1
    assert len(abstract[0].text.split()) > 300


def test_a_plain_abstract_is_one_abstract_chunk() -> None:
    plain = "<abstract><p>One paragraph.</p><p>And a second.</p></abstract>"

    abstract = [c for c in _chunks(_article(abstract=plain)) if c.heading_path == ("Abstract",)]

    assert [c.section_kind for c in abstract] == [SectionKind.ABSTRACT]
    assert abstract[0].text == "One paragraph.\n\nAnd a second."


def test_graphical_and_translated_abstracts_are_not_the_abstract() -> None:
    other = (
        '<abstract abstract-type="graphical"><p>Picture caption.</p></abstract>'
        '<trans-abstract xml:lang="pl"><p>Streszczenie po polsku.</p></trans-abstract>'
        "<abstract><p>The real abstract.</p></abstract>"
    )

    text = " ".join(c.text for c in _chunks(_article(abstract=other)))

    assert "The real abstract." in text
    assert "Picture caption." not in text
    assert "Streszczenie po polsku." not in text


def test_an_abstract_in_another_language_than_the_article_is_not_the_abstract() -> None:
    abstracts = (
        '<abstract xml:lang="pl"><p>Streszczenie po polsku.</p></abstract>'
        '<abstract xml:lang="en"><p>The English abstract.</p></abstract>'
    )
    data = _article(abstract=abstracts).replace(b"<article ", b'<article xml:lang="en" ', 1)

    text = " ".join(c.text for c in _chunks(data))

    assert "The English abstract." in text
    assert "Streszczenie po polsku." not in text


def test_an_abstract_without_a_language_is_the_abstract_whatever_the_article_says() -> None:
    data = _article(abstract="<abstract><p>Plain.</p></abstract>").replace(
        b"<article ", b'<article xml:lang="en" ', 1
    )

    assert any(c.text == "Plain." for c in _chunks(data))


def test_reference_list_is_never_a_chunk() -> None:
    chunks = _chunks(_article())

    assert chunks
    assert not any("Reference list entry" in c.text for c in chunks)
    assert not any("Thanks to the funder" in c.text for c in chunks)
    assert not any("References" in c.heading_path for c in chunks)


def test_a_references_section_inside_the_body_is_left_out_too() -> None:
    body = _BODY + "<sec><title>References</title><p>Doe J. Inline reference.</p></sec>"

    assert not any("Inline reference" in c.text for c in _chunks(_article(body=body)))


def test_table_and_caption_are_one_chunk() -> None:
    tables = [c for c in _chunks(_article()) if c.section_kind is SectionKind.TABLE]

    assert len(tables) == 1
    assert tables[0].heading_path == ("Results",)
    assert tables[0].text == "Arm | Sets\nHalved | 6\nKept | 12\n\nTable 1. Weekly volume by arm."


_FLOATS = """<floats-group>
<table-wrap id="t9"><label>Table 9</label><caption><p>Cited in Results.</p></caption>
<table><tbody><tr><td>a</td><td>1</td></tr></tbody></table></table-wrap>
<fig id="f9"><label>Figure 9</label><caption><p>Cited in Methods.</p></caption></fig>
<table-wrap id="t10"><label>Table 10</label><caption><p>Never cited.</p></caption>
<table><tbody><tr><td>b</td><td>2</td></tr></tbody></table></table-wrap>
</floats-group>"""
_CITING_BODY = """
<sec><title>Methods</title>
<p>See <xref ref-type="fig" rid="f9">Figure 9</xref>.</p></sec>
<sec><title>Results</title>
<p>Values are in <xref ref-type="table" rid="t9">Table 9</xref>
and again <xref ref-type="table" rid="t9">Table 9</xref>.</p></sec>
"""


def test_a_floating_table_lands_in_the_section_that_cites_it() -> None:
    data = _article(body=_CITING_BODY).replace(b"</article>", _FLOATS.encode() + b"</article>")

    tables = [c for c in _chunks(data) if c.section_kind is SectionKind.TABLE]

    cited = next(c for c in tables if "Cited in Results." in c.text)
    assert cited.heading_path == ("Results",)
    assert cited.text == "a | 1\n\nTable 9. Cited in Results."
    assert sum("Cited in Results." in c.text for c in _chunks(data)) == 1


def test_tables_in_a_group_wrapper_in_floats_are_read_too() -> None:
    floats = (
        "<floats-group><table-wrap-group id='g1'><label>Table 5</label>"
        "<table-wrap id='t5'><label>Table 5a</label><caption><p>First.</p></caption>"
        "<table><tbody><tr><td>p</td><td>1</td></tr></tbody></table></table-wrap>"
        "<table-wrap id='t6'><label>Table 5b</label><caption><p>Second.</p></caption>"
        "<table><tbody><tr><td>q</td><td>2</td></tr></tbody></table></table-wrap>"
        "</table-wrap-group>"
        "<fig-group id='fg'><fig id='f5'><label>Figure 5</label><caption><p>Grouped.</p>"
        "</caption></fig></fig-group></floats-group>"
    )
    body = (
        "<sec><title>Results</title><p><xref ref-type='table' rid='t5'>5a</xref> and "
        "<xref ref-type='fig' rid='f5'>5</xref>.</p></sec>"
    )
    data = _article(body=body).replace(b"</article>", floats.encode() + b"</article>")

    chunks = _chunks(data)

    text = "\n".join(c.text for c in chunks)
    for wanted in ("Table 5a. First.", "Table 5b. Second.", "Figure 5. Grouped."):
        assert wanted in text
    cited = next(c for c in chunks if "Table 5a. First." in c.text)
    assert cited.heading_path == ("Results",)
    assert next(c for c in chunks if "Table 5b." in c.text).heading_path == ()


def test_a_floating_figure_caption_lands_in_the_citing_section() -> None:
    data = _article(body=_CITING_BODY).replace(b"</article>", _FLOATS.encode() + b"</article>")

    methods = [c for c in _chunks(data) if c.heading_path == ("Methods",)]

    assert any("Figure 9. Cited in Methods." in c.text for c in methods)


def test_an_uncited_floating_table_is_kept_at_the_end_without_a_section() -> None:
    data = _article(body=_CITING_BODY).replace(b"</article>", _FLOATS.encode() + b"</article>")

    last = _chunks(data)[-1]

    assert last.section_kind is SectionKind.TABLE
    assert last.heading_path == ()
    assert last.text == "b | 2\n\nTable 10. Never cited."


def test_chunks_name_their_sections_and_carry_the_doi() -> None:
    chunks = _chunks(_article())

    paths = {c.heading_path for c in chunks}
    assert ("Introduction",) in paths
    assert ("Methods", "Design") in paths
    assert ("Results",) in paths
    for chunk in chunks:
        assert isinstance(chunk.locator, SectionLocator)
        assert chunk.locator.doi == "10.9999/test.1"
    design = next(c for c in chunks if c.heading_path == ("Methods", "Design"))
    assert isinstance(design.locator, SectionLocator)
    assert design.locator.section == "Design"


def test_an_untitled_section_keeps_its_parents_path() -> None:
    document = _document(_article())

    untitled = next(b for b in document.blocks if b.text.startswith("An untitled"))

    assert untitled.heading_path == ("Methods",)
    assert untitled.locator == SectionLocator(section="Methods", doi="10.9999/test.1")


def test_inline_citation_marks_stay_in_the_sentence() -> None:
    document = _document(_article())

    intro = next(b for b in document.blocks if b.text.startswith("Deloads are common"))

    assert intro.text == "Deloads are common in practice.[1]"


def test_formulas_and_graphics_add_no_noise() -> None:
    body = (
        "<sec><title>Maths</title><p>Load is <inline-formula><mml:math><mml:mi>x</mml:mi>"
        "</mml:math></inline-formula> kilograms. <inline-graphic xlink:href='a.png'/>"
        "Done.</p><disp-formula><mml:math><mml:mi>y</mml:mi></mml:math></disp-formula></sec>"
    )

    document = _document(_article(body=body))

    paragraph = next(
        b for b in document.blocks if b.kind is BlockKind.PARAGRAPH and "Load" in b.text
    )
    assert paragraph.text == "Load is kilograms. Done."


def test_lists_and_figure_captions_are_blocks() -> None:
    body = (
        "<sec><title>Plan</title><list list-type='bullet'><list-item><p>Halve sets</p></list-item>"
        "<list-item><p>Keep load</p></list-item></list>"
        "<fig id='f1'><label>Figure 1</label><caption><p>Strength over time.</p></caption>"
        "<graphic xlink:href='f.png'/></fig></sec>"
    )

    document = _document(_article(body=body))

    assert [(b.kind, b.text) for b in document.blocks if b.kind is not BlockKind.HEADING][-2:] == [
        (BlockKind.LIST, "Halve sets\nKeep load"),
        (BlockKind.CAPTION, "Figure 1. Strength over time."),
    ]


def test_blocks_inside_an_unknown_container_are_still_read() -> None:
    body = (
        "<sec><title>Notes</title><boxed-text><p>Boxed advice about sleep.</p>"
        "<disp-quote><p>Quoted line.</p></disp-quote></boxed-text></sec>"
    )

    document = _document(_article(body=body))

    assert [(b.heading_path, b.text) for b in document.blocks if b.kind is BlockKind.PARAGRAPH][
        -2:
    ] == [(("Notes",), "Boxed advice about sleep."), (("Notes",), "Quoted line.")]


def test_a_table_inside_a_paragraph_is_its_own_block_after_the_text() -> None:
    body = (
        "<sec><title>Results</title><p>Totals follow.<table-wrap><label>Table 2</label>"
        "<caption><p>Totals.</p></caption><table><tbody><tr><td>x</td><td>9</td></tr></tbody>"
        "</table></table-wrap></p></sec>"
    )

    document = _document(_article(body=body))

    assert [(b.kind, b.text) for b in document.blocks if b.heading_path == ("Results",)][1:] == [
        (BlockKind.PARAGRAPH, "Totals follow."),
        (BlockKind.TABLE, "x | 9"),
        (BlockKind.CAPTION, "Table 2. Totals."),
    ]


def test_a_missing_doi_leaves_the_locator_without_one() -> None:
    document = _document(_article().replace(b'pub-id-type="doi"', b'pub-id-type="other"'))

    assert all(
        isinstance(b.locator, SectionLocator) and b.locator.doi is None for b in document.blocks
    )


def test_a_doi_url_is_reduced_to_the_bare_doi() -> None:
    document = _document(_article(doi="https://doi.org/10.9999/Test.1"))

    assert all(
        isinstance(b.locator, SectionLocator) and b.locator.doi == "10.9999/test.1"
        for b in document.blocks
    )


def test_body_text_outside_any_section_has_no_heading() -> None:
    document = _document(_article(abstract="", body="<p>Loose paragraph.</p>"))

    assert [(b.heading_path, b.text) for b in document.blocks] == [((), "Loose paragraph.")]


def test_registered_for_the_jats_file_type() -> None:
    assert isinstance(
        DocumentReaders().parse(KnowledgeFileType.JATS, _article()), KnowledgeDocument
    )


@pytest.mark.parametrize(
    ("data", "reason"),
    [
        pytest.param(b"<article><front>", UnreadableReason.CORRUPT, id="truncated"),
        pytest.param(b"not xml at all", UnreadableReason.CORRUPT, id="not-xml"),
        pytest.param(
            b"<html><body><p>Hi</p></body></html>", UnreadableReason.CORRUPT, id="other-xml"
        ),
        pytest.param(b"", UnreadableReason.CORRUPT, id="empty"),
        pytest.param(
            b"<article><body><sec><title>Only a title</title></sec></body></article>",
            UnreadableReason.NO_TEXT,
            id="no-text",
        ),
        pytest.param(b"\xff\xfe<article/>", UnreadableReason.CORRUPT, id="bad-encoding"),
    ],
)
def test_a_file_it_cannot_read_gives_a_reason(data: bytes, reason: UnreadableReason) -> None:
    assert _unreadable(data) is reason


def test_an_entity_bomb_is_not_expanded() -> None:
    bomb = (
        b'<?xml version="1.0"?><!DOCTYPE article [<!ENTITY a "aaaaaaaaaa">'
        b'<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">]>'
        b"<article><body><p>&b;&b;&b;&b;</p></body></article>"
    )

    result = read_jats(bomb)

    # Entities are never resolved: nothing they would expand to is read, so the article has no
    # text.
    assert isinstance(result, Unreadable)
    assert result.reason is UnreadableReason.NO_TEXT


def test_an_external_entity_never_reads_a_file() -> None:
    attack = (
        b'<?xml version="1.0"?><!DOCTYPE article [<!ENTITY x SYSTEM "file:///etc/hostname">]>'
        b"<article><body><p>Before &x; after</p></body></article>"
    )

    result = read_jats(attack)

    if isinstance(result, KnowledgeDocument):
        assert all("/" not in b.text.replace("Before", "") for b in result.blocks)


def test_a_deeply_nested_file_is_corrupt_not_a_crash() -> None:
    depth = 5000
    data = (
        b"<article><body>"
        + b"<sec>" * depth
        + b"<p>x</p>"
        + b"</sec>" * depth
        + b"</body></article>"
    )

    assert _unreadable(data) is UnreadableReason.CORRUPT


def test_a_real_pmc_article() -> None:
    chunks = _CHUNKER.chunk(_document(FIXTURE.read_bytes()))

    abstract = [c for c in chunks if c.section_kind is SectionKind.ABSTRACT]
    assert len(abstract) == 1
    assert "Klotho" in abstract[0].text
    assert sum(1 for c in chunks if c.section_kind is SectionKind.TABLE) == 3
    assert any(c.heading_path[:1] == ("2. Materials and Methods",) for c in chunks)
    assert any(c.heading_path[:1] == ("3. Results",) for c in chunks)
    assert not any("References" in c.heading_path for c in chunks)
    assert all(
        isinstance(c.locator, SectionLocator) and c.locator.doi == "10.3390/jfmk11040434"
        for c in chunks
    )
