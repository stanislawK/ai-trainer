"""Small PDFs for the reader and ingestion tests (#425), written by hand so no binary fixture
lives in the repo."""

from io import BytesIO

from pypdf import PdfReader, PdfWriter


def _escape(line: str) -> str:
    return line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _content(lines: list[str]) -> bytes:
    ops = ["BT", "/F1 11 Tf", "14 TL", "50 780 Td"]
    ops.extend(f"({_escape(line)}) Tj T*" for line in lines)
    ops.append("ET")
    return "\n".join(ops).encode("latin-1")


def text_pdf(pages: list[list[str]], *, labels: str | None = None) -> bytes:
    """A PDF with one text line per list item on each page. `labels` is the body of a
    `/PageLabels` number tree, e.g. `"0 << /S /r >> 2 << /S /D >>"`."""
    objects: list[bytes] = []
    first_page = 4
    kids = " ".join(f"{first_page + 2 * i} 0 R" for i in range(len(pages)))
    catalog = "<< /Type /Catalog /Pages 2 0 R"
    if labels is not None:
        catalog += f" /PageLabels << /Nums [{labels}] >>"
    objects.append((catalog + " >>").encode())
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode())
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for number, lines in enumerate(pages):
        page_id = first_page + 2 * number
        objects.append(
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 842] "
                f"/Resources << /Font << /F1 3 0 R >> >> /Contents {page_id + 1} 0 R >>"
            ).encode()
        )
        stream = _content(lines)
        objects.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for index, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % index + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref,
    )
    return bytes(out)


def blank_pdf(pages: int = 2) -> bytes:
    """Pages with no text at all, as a scan with no text layer reads."""
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=612, height=842)
    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def encrypted_pdf(
    pdf: bytes,
    *,
    user_password: str,
    owner_password: str | None = None,
    algorithm: str = "RC4-128",
) -> bytes:
    """`pdf` encrypted with RC4, or with AES, which needs the `cryptography` package."""
    writer = PdfWriter(clone_from=PdfReader(BytesIO(pdf)))
    writer.encrypt(user_password, owner_password, algorithm=algorithm)
    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue()
