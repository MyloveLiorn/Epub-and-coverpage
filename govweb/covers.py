"""Does a PDF open with a cover? Its first page is looked at the way a person would: a picture or
a lot of colour makes a cover; a mostly white page with a few words is a plain title page; a page
of text (a table of contents, the first chapter) means the PDF has no cover.

Needs pypdfium2 (pip install ".[covers]"); without it nothing is told.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from govweb.classify import year_in

COVER = "cover"
TITLE_PAGE = "title page"
NO_COVER = "no cover"
SCANNED = "scanned"  # every page is a picture: which one is the cover can't be told

LABELS = {COVER: "Yes", TITLE_PAGE: "Title page only", NO_COVER: "No", SCANNED: "Scanned - check"}

SCALE = 0.5  # 36 dots per inch is plenty to tell a photo from a page of text
FULL_PAGE = 0.85  # a picture covering this much of a page is the whole page
FEW_WORDS = 80


@dataclass
class PageLook:
    text: str
    words: int
    picture: float  # share of the page covered by pictures
    ink: float  # share of the page that isn't white
    colour: float  # share of the page in clear colour (not black, grey or white)


def _look(page) -> PageLook:
    import pypdfium2.raw as pdfium_c

    width, height = page.get_size()
    area = max(width * height, 1.0)
    text = page.get_textpage()
    try:
        words = text.get_text_range()
    finally:
        text.close()
    picture = 0.0
    for obj in page.get_objects(filter=(pdfium_c.FPDF_PAGEOBJ_IMAGE,), max_depth=3):
        left, bottom, right, top = obj.get_bounds()
        picture += max(0.0, min(right, width) - max(left, 0.0)) * max(0.0, min(top, height) - max(bottom, 0.0))
    bitmap = page.render(scale=SCALE)
    try:
        ink, colour = _ink_and_colour(bytes(bitmap.buffer), bitmap.width, bitmap.height, bitmap.stride,
                                      bitmap.n_channels)  # fmt: skip
    finally:
        bitmap.close()
    return PageLook(words, len(words.split()), min(picture / area, 1.0), ink, colour)


def _ink_and_colour(buffer: bytes, width: int, height: int, stride: int, channels: int) -> tuple[float, float]:
    ink = colour = 0
    for y in range(height):
        row = buffer[y * stride : y * stride + width * channels]
        for b, g, r in zip(row[0::channels], row[1::channels], row[2::channels]):
            high, low = max(r, g, b), min(r, g, b)
            if high < 230:
                ink += 1
            if high - low > 48 and high > 60:
                colour += 1
    pixels = max(width * height, 1)
    return ink / pixels, colour / pixels


def _page_look(pdf, index: int) -> PageLook:
    page = pdf[index]
    try:
        return _look(page)
    finally:
        page.close()


def classify(first: PageLook, second: PageLook | None = None) -> str:
    """cover, title page, no cover or scanned, from how the first (and second) page look."""
    if first.picture >= FULL_PAGE:
        # A page-size picture: a cover, unless the next page is one too (a scan, perhaps with the
        # scanned text readable behind the picture).
        if second is not None and second.picture >= FULL_PAGE:
            return SCANNED
        return COVER
    if first.picture >= 0.3 or first.colour >= 0.12 or first.ink >= 0.45:
        return COVER
    if first.words <= FEW_WORDS:
        return TITLE_PAGE
    return NO_COVER


def available() -> bool:
    try:
        import pypdfium2  # noqa: F401
    except ImportError:
        return False
    return True


def cover_of(data: bytes) -> str | None:
    """What the PDF opens with (see classify), or None when it can't be told."""
    return look_at(data)[0]


def _metadata_year(pdf) -> int | None:
    try:
        created = pdf.get_metadata_dict().get("CreationDate", "")
    except Exception:
        return None
    match = re.match(r"D?:?((?:19|20)\d\d)", created or "")
    return int(match.group(1)) if match else None


def look_at(data: bytes) -> tuple[str | None, int | None]:
    """What the PDF opens with (see classify) and the year it was published: the latest year its
    first page names ("July 2017, Volume 65"), else the year the file was made. (None, None) when
    the PDF can't be read."""
    try:
        import pypdfium2 as pdfium
    except ImportError:
        return None, None
    try:
        pdf = pdfium.PdfDocument(data)
    except Exception:  # damaged or encrypted
        return None, None
    try:
        if len(pdf) == 0:
            return None, None
        first = _page_look(pdf, 0)
        second = None
        if first.picture >= FULL_PAGE and len(pdf) > 1:  # a scan?
            second = _page_look(pdf, 1)
        return classify(first, second), year_in(first.text) or _metadata_year(pdf)
    except Exception:
        return None, None
    finally:
        pdf.close()
