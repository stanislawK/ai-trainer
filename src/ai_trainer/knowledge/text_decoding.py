"""Decoding a text file's bytes, shared by the Markdown and plain-text readers."""


def decode_text(data: bytes) -> str | None:
    """The file as text with `\\n` line ends, or None when it isn't UTF-8 text. A byte-order
    mark is dropped, and a NUL byte means binary."""
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None
    if "\x00" in text:
        return None
    return text.replace("\r\n", "\n").replace("\r", "\n")


def squash(lines: list[str]) -> str:
    """Wrapped lines joined into one, with runs of whitespace as one space."""
    return " ".join(" ".join(lines).split())
