"""The EPUB reader (ADR-0036, #426): the container read with `zipfile`, the spine followed for
order, the navigation document (EPUB 3) or the NCX (EPUB 2) for chapter titles, and each spine
item's XHTML parsed with lxml into the shared document model.

- Each block's locator is its spine item (the path in the archive) and the chapter's title; a
  chapter the contents leave out takes its first heading, and text before the first heading sits
  under the chapter title.
- A book with DRM is `ENCRYPTED`: Adobe's `rights.xml`, or an `encryption.xml` naming any
  algorithm beyond the two that only obfuscate an embedded font. The reader never tries to open
  one.
- Each spine item's XHTML becomes blocks in `xhtml.py`, through lxml's HTML parser, which never
  resolves an entity declared in the file. The container, the package and the NCX are XML parsed
  strictly, with entities, DTDs and the network off; a failure there is a corrupt book. A spine
  item that won't parse is skipped.
- A zip bomb is bounded: one file unpacked, and all files together, are capped.

lxml (BSD-3-Clause) is used instead of ebooklib, which is AGPL (ADR-0036).
"""

import posixpath
import zlib
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from io import BytesIO
from urllib.parse import unquote, urldefrag
from zipfile import BadZipFile, ZipFile

from lxml import etree

from ai_trainer.domain.knowledge_document import (
    Block,
    BlockKind,
    KnowledgeDocument,
    SpineLocator,
    Unreadable,
    UnreadableReason,
)
from ai_trainer.knowledge import xhtml
from ai_trainer.knowledge.xhtml import local_name, squash, xhtml_blocks

_MAX_MEMBER_BYTES = 25 * 1024 * 1024
_MAX_TOTAL_BYTES = 150 * 1024 * 1024

_CONTAINER = "META-INF/container.xml"
_RIGHTS = "META-INF/rights.xml"
_ENCRYPTION = "META-INF/encryption.xml"
# The two algorithms that only scramble an embedded font so it can't be lifted; not DRM.
_FONT_OBFUSCATION = frozenset(
    {"http://www.idpf.org/2008/embedding", "http://ns.adobe.com/pdf/enc#RC"}
)
_XHTML_TYPES = frozenset({"application/xhtml+xml", "text/html"})


class _BadBookError(Exception):
    """A book that can't be read, and why."""

    def __init__(self, reason: UnreadableReason) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class _Item:
    path: str
    media_type: str
    properties: frozenset[str]


@dataclass(frozen=True, slots=True)
class _Package:
    items: Mapping[str, _Item]
    spine: tuple[str, ...]
    ncx_id: str | None


class _Archive:
    """The zip, read one file at a time under the size caps."""

    def __init__(self, zipped: ZipFile) -> None:
        self._zip = zipped
        self._total = 0

    def read(self, name: str) -> bytes | None:
        """The file's bytes, or None when the archive has no such file."""
        try:
            info = self._zip.getinfo(name)
        except KeyError:
            return None
        if info.file_size > _MAX_MEMBER_BYTES:
            raise _BadBookError(UnreadableReason.CORRUPT)
        try:
            with self._zip.open(info) as member:
                data = member.read(_MAX_MEMBER_BYTES + 1)
        except RuntimeError:
            # zipfile's way of saying the file is password-protected.
            raise _BadBookError(UnreadableReason.ENCRYPTED) from None
        self._total += len(data)
        if len(data) > _MAX_MEMBER_BYTES or self._total > _MAX_TOTAL_BYTES:
            raise _BadBookError(UnreadableReason.CORRUPT)
        return data

    def require(self, name: str) -> bytes:
        data = self.read(name)
        if data is None:
            raise _BadBookError(UnreadableReason.CORRUPT)
        return data


def _xml(data: bytes) -> etree._Element:
    parser = etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        load_dtd=False,
        dtd_validation=False,
        huge_tree=False,
        remove_comments=True,
    )
    return etree.fromstring(data, parser)


def _resolve(directory: str, href: str) -> str:
    """An href as an archive path, without its fragment."""
    return posixpath.normpath(posixpath.join(directory, unquote(urldefrag(href).url)))


def _protected(archive: _Archive) -> bool:
    if archive.read(_RIGHTS) is not None:
        return True
    data = archive.read(_ENCRYPTION)
    if data is None:
        return False
    try:
        root = _xml(data)
    except etree.XMLSyntaxError:
        # Nothing proves the file only lists fonts, so don't read the book.
        return True
    return any(
        element.get("Algorithm") not in _FONT_OBFUSCATION
        for element in root.iter()
        if local_name(element) == "EncryptionMethod"
    )


def _package(archive: _Archive) -> _Package:
    container = _xml(archive.require(_CONTAINER))
    rootfile = next(
        (e for e in container.iter() if local_name(e) == "rootfile" and e.get("full-path")), None
    )
    if rootfile is None:
        raise _BadBookError(UnreadableReason.CORRUPT)
    package_path = posixpath.normpath(rootfile.get("full-path"))
    directory = posixpath.dirname(package_path)
    items: dict[str, _Item] = {}
    spine: list[str] = []
    ncx_id: str | None = None
    for element in _xml(archive.require(package_path)).iter():
        name = local_name(element)
        if name == "item" and element.get("id") and element.get("href"):
            items[element.get("id")] = _Item(
                path=_resolve(directory, element.get("href")),
                media_type=element.get("media-type", ""),
                properties=frozenset(element.get("properties", "").split()),
            )
        elif name == "itemref" and element.get("idref"):
            spine.append(element.get("idref"))
        elif name == "spine":
            ncx_id = element.get("toc")
    return _Package(items=items, spine=tuple(spine), ncx_id=ncx_id)


type _Entries = list[tuple[str, str]]


@dataclass(frozen=True, slots=True)
class _Titles:
    """Chapter titles by archive path: the first entry listed for a file, and the files the
    contents list more than once, which hold several chapters in one file."""

    by_file: Mapping[str, str]
    shared: frozenset[str]


def _nav_entries(data: bytes, path: str) -> _Entries:
    """The links of an EPUB 3 navigation document's table of contents, in order: the archive
    path each points into, and its title."""
    root = xhtml.parse_html(data)
    if root is None:
        return []
    navs = list(root.iter("nav"))
    toc = [n for n in navs if "toc" in (n.get("epub:type") or "").split()] or navs[:1]
    return [
        (_resolve(posixpath.dirname(path), link.get("href")), title)
        for nav in toc
        for link in nav.iter("a")
        if (title := squash(link.itertext())) and link.get("href")
    ]


def _ncx_entries(data: bytes, path: str) -> _Entries:
    """The points of an EPUB 2 NCX, in the same way."""
    entries: _Entries = []
    for point in _xml(data).iter():
        if local_name(point) != "navPoint":
            continue
        label = next((t for t in point.iter() if local_name(t) == "text"), None)
        content = next((c for c in point if local_name(c) == "content"), None)
        title = squash(label.itertext()) if label is not None else ""
        if title and content is not None and content.get("src"):
            entries.append((_resolve(posixpath.dirname(path), content.get("src")), title))
    return entries


def _titles(archive: _Archive, package: _Package) -> _Titles:
    """The chapter titles. A contents list that won't read only costs the titles; the book is
    still read."""
    nav = next((item for item in package.items.values() if "nav" in item.properties), None)
    ncx = package.items.get(package.ncx_id or "")
    for item, entries_of in ((nav, _nav_entries), (ncx, _ncx_entries)):
        data = None if item is None else archive.read(item.path)
        if item is None or data is None:
            continue
        try:
            entries = entries_of(data, item.path)
        except etree.LxmlError, ValueError:
            continue
        if entries:
            by_file: dict[str, str] = {}
            for entry_path, title in entries:
                by_file.setdefault(entry_path, title)
            listed = Counter(entry_path for entry_path, _ in entries)
            return _Titles(by_file, frozenset(p for p, count in listed.items() if count > 1))
    return _Titles({}, frozenset())


def _item_blocks(data: bytes, path: str, listed_title: str | None, *, shared: bool) -> list[Block]:
    """A spine item's blocks. A file the contents list once is one chapter; one it lists more
    than once holds several, and each block takes its outermost heading as its chapter."""
    raw = xhtml_blocks(data)
    if raw is None:
        return []
    chapter = listed_title or next(
        (text for kind, text, _ in raw if kind is BlockKind.HEADING), None
    )
    fallback = (chapter,) if chapter else ()
    return [
        Block(
            kind=kind,
            text=text,
            heading_path=heading_path or fallback,
            locator=SpineLocator(
                spine_item=path, chapter=heading_path[0] if shared and heading_path else chapter
            ),
        )
        for kind, text, heading_path in raw
    ]


def _read(data: bytes) -> KnowledgeDocument:
    with ZipFile(BytesIO(data)) as zipped:
        archive = _Archive(zipped)
        if _protected(archive):
            raise _BadBookError(UnreadableReason.ENCRYPTED)
        package = _package(archive)
        titles = _titles(archive, package)
        # A spine that lists an item twice (some generators do) reads it once.
        spine = (package.items.get(idref) for idref in dict.fromkeys(package.spine))
        blocks: list[Block] = []
        found = False
        for item in spine:
            content = archive.read(item.path) if item and item.media_type in _XHTML_TYPES else None
            if content is not None and item is not None:
                found = True
                blocks.extend(
                    _item_blocks(
                        content,
                        item.path,
                        titles.by_file.get(item.path),
                        shared=item.path in titles.shared,
                    )
                )
    if not found:
        raise _BadBookError(UnreadableReason.CORRUPT)
    if not blocks:
        raise _BadBookError(UnreadableReason.NO_TEXT)
    return KnowledgeDocument(blocks=tuple(blocks))


def read_epub(data: bytes) -> KnowledgeDocument | Unreadable:
    try:
        return _read(data)
    except _BadBookError as bad:
        return Unreadable(reason=bad.reason)
    except (
        BadZipFile,
        etree.LxmlError,
        zlib.error,
        EOFError,
        NotImplementedError,
        RecursionError,
        ValueError,
    ):
        return Unreadable(reason=UnreadableReason.CORRUPT)
