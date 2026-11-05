"""Small EPUBs for the reader and ingestion tests (#426), built in memory so no binary fixture
lives in the repo."""

from dataclasses import dataclass
from io import BytesIO
from typing import Literal
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile

_XHTML = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">'
    "<head><title>t</title><style>p {{ color: red; }}</style></head><body>{body}</body></html>"
)
_CONTAINER = (
    '<?xml version="1.0"?>'
    '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
    '<rootfiles><rootfile full-path="OEBPS/content.opf" '
    'media-type="application/oebps-package+xml"/></rootfiles></container>'
)

# The two algorithms that only obfuscate an embedded font; they are not DRM.
IDPF_FONT_OBFUSCATION = "http://www.idpf.org/2008/embedding"
ADOBE_FONT_OBFUSCATION = "http://ns.adobe.com/pdf/enc#RC"
AES_ALGORITHM = "http://www.w3.org/2001/04/xmlenc#aes128-cbc"


@dataclass(frozen=True)
class Chapter:
    """One spine item: its file name, its title in the table of contents (None leaves it out),
    and the XHTML inside `<body>`."""

    file: str
    toc_title: str | None
    body: str


def encryption_xml(*entries: tuple[str, str]) -> str:
    """META-INF/encryption.xml for (algorithm, file URI) entries."""
    encrypted = "".join(
        f'<enc:EncryptedData><enc:EncryptionMethod Algorithm="{algorithm}"/>'
        f'<enc:CipherData><enc:CipherReference URI="{uri}"/></enc:CipherData>'
        "</enc:EncryptedData>"
        for algorithm, uri in entries
    )
    return (
        '<?xml version="1.0"?>'
        '<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container" '
        f'xmlns:enc="http://www.w3.org/2001/04/xmlenc#">{encrypted}</encryption>'
    )


def _nav(chapters: list[Chapter]) -> str:
    items = "".join(
        f'<li><a href="{c.file}">{c.toc_title}</a></li>' for c in chapters if c.toc_title
    )
    return _XHTML.format(
        body=f'<nav epub:type="toc"><h1>Contents</h1><ol>{items}</ol></nav>'
        '<nav epub:type="landmarks"><ol><li><a href="ch1.xhtml">Start</a></li></ol></nav>'
    )


def _ncx(chapters: list[Chapter]) -> str:
    points = "".join(
        f'<navPoint id="n{i}"><navLabel><text>{c.toc_title}</text></navLabel>'
        f'<content src="{c.file}"/></navPoint>'
        for i, c in enumerate(chapters)
        if c.toc_title
    )
    return (
        '<?xml version="1.0"?><ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">'
        f"<navMap>{points}</navMap></ncx>"
    )


def _opf(chapters: list[Chapter], nav: str, spine: list[str]) -> str:
    items = "".join(
        f'<item id="c{i}" href="{c.file}" media-type="application/xhtml+xml"/>'
        for i, c in enumerate(chapters)
    )
    if nav == "nav":
        items += (
            '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
        )
    if nav == "ncx":
        items += '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>'
    refs = "".join(f'<itemref idref="{idref}"/>' for idref in spine)
    toc = ' toc="ncx"' if nav == "ncx" else ""
    return (
        '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0" '
        'unique-identifier="id"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
        "<dc:title>Book</dc:title></metadata>"
        f"<manifest>{items}</manifest><spine{toc}>{refs}</spine></package>"
    )


def epub(
    chapters: list[Chapter],
    *,
    nav: Literal["nav", "ncx", "none"] = "nav",
    spine: list[str] | None = None,
    encryption: str | None = None,
    rights: bool = False,
    container: str | None = _CONTAINER,
    opf: str | None = None,
    nav_content: str | None = None,
    locked: bool = False,
) -> bytes:
    """An EPUB with the chapters in spine order (or `spine`'s manifest ids). `container`,
    `opf` and `nav_content` (the navigation document or the NCX) replace those files; `None`
    for `container` leaves it out. `locked` marks every file as password-protected in the zip."""
    buffer = BytesIO()
    with ZipFile(buffer, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip", compress_type=ZIP_STORED)
        if container is not None:
            archive.writestr("META-INF/container.xml", container, compress_type=ZIP_DEFLATED)
        if encryption is not None:
            archive.writestr("META-INF/encryption.xml", encryption)
        if rights:
            archive.writestr("META-INF/rights.xml", "<rights/>")
        order = spine if spine is not None else [f"c{i}" for i in range(len(chapters))]
        archive.writestr("OEBPS/content.opf", _opf(chapters, nav, order) if opf is None else opf)
        for chapter in chapters:
            archive.writestr(f"OEBPS/{chapter.file}", _XHTML.format(body=chapter.body))
        if nav == "nav":
            archive.writestr(
                "OEBPS/nav.xhtml", _nav(chapters) if nav_content is None else nav_content
            )
        if nav == "ncx":
            archive.writestr(
                "OEBPS/toc.ncx", _ncx(chapters) if nav_content is None else nav_content
            )
        if locked:
            # zipfile can't write an encrypted member; the flag in the central directory is what
            # makes it refuse to open one without a password.
            for info in archive.infolist():
                info.flag_bits |= 0x1
    return buffer.getvalue()


def with_files(data: bytes, files: dict[str, bytes]) -> bytes:
    """The EPUB with these files replaced, by their path in the archive."""
    buffer = BytesIO()
    with ZipFile(BytesIO(data)) as source, ZipFile(buffer, "w") as target:
        for name in source.namelist():
            target.writestr(name, files.get(name, source.read(name)))
    return buffer.getvalue()
