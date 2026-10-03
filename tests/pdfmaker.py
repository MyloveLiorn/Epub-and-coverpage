"""Tiny PDFs for tests: pages drawn from content-stream text."""

from __future__ import annotations

import zlib


def make_pdf(pages: list[str], image: bytes | None = None) -> bytes:
    """A PDF whose pages draw the given content streams (Helvetica as /F1; with ``image``, raw
    2x2 RGB pixels as /Im1)."""
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    catalog = add(b"")  # filled in below
    pages_obj = add(b"")
    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    xobjects = b""
    if image is not None:
        data = zlib.compress(image)
        im = add(b"<< /Type /XObject /Subtype /Image /Width 2 /Height 2 /ColorSpace /DeviceRGB "
                 b"/BitsPerComponent 8 /Filter /FlateDecode /Length %d >>\nstream\n" % len(data) + data
                 + b"\nendstream")
        xobjects = b" /XObject << /Im1 %d 0 R >>" % im
    kids = []
    for content in pages:
        stream = content.encode()
        contents = add(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
        kids.append(add(b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 612 792] /Contents %d 0 R "
                        b"/Resources << /Font << /F1 %d 0 R >>%s >> >>" % (pages_obj, contents, font, xobjects)))
    objects[catalog - 1] = b"<< /Type /Catalog /Pages %d 0 R >>" % pages_obj
    objects[pages_obj - 1] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (
        b" ".join(b"%d 0 R" % k for k in kids), len(kids))
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, catalog, xref)
    return bytes(out)


def text_page(lines: int, size: int = 10, words: str = "Immigration history of the United States chapter") -> str:
    body = "".join(f"BT /F1 {size} Tf 50 {760 - i * (size + 4)} Td ({words} {i}) Tj ET\n" for i in range(lines))
    return body
