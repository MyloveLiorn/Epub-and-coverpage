"""Counting a PDF's pages without a PDF library.

A PDF's page tree has a root "/Type /Pages" dictionary whose "/Count" is the number of pages.
Newer PDFs keep it inside compressed object streams, so those are inflated too. As a last resort
the individual "/Type /Page" objects are counted.
"""

from __future__ import annotations

import re
import zlib

_PAGES = re.compile(rb"/Type\s*/Pages\b")
_COUNT = re.compile(rb"/Count\s+(\d+)")
_PAGE = re.compile(rb"/Type\s*/Page\b(?!s)")
_STREAM = re.compile(rb"stream\r?\n(.*?)\r?\nendstream", re.S)
MAX_INFLATED = 50_000_000  # bytes inflated in total, in case of a compression bomb
WINDOW = 1500  # bytes around "/Type /Pages" searched for its "/Count"


def _tree_count(data: bytes) -> int:
    """The largest "/Count" of the page-tree dictionaries in ``data`` (the root's), or 0."""
    best = 0
    for match in _PAGES.finditer(data):
        # The dictionary's own object: from its "obj" to its "endobj" when those are present.
        start = max(data.rfind(b"obj", max(0, match.start() - WINDOW), match.start()), match.start() - WINDOW, 0)
        end = data.find(b"endobj", match.end(), match.end() + WINDOW)
        segment = data[start : end if end != -1 else match.end() + WINDOW]
        for count in _COUNT.findall(segment):
            best = max(best, int(count))
    return best


def count_pages(data: bytes) -> int | None:
    """The number of pages of a PDF, or None when it can't be told."""
    if not data.startswith(b"%PDF"):
        return None
    pages = _tree_count(data)
    if pages:
        return pages
    inflated_total = 0
    page_objects = len(_PAGE.findall(data))
    for match in _STREAM.finditer(data):
        if inflated_total > MAX_INFLATED:
            break
        try:
            inflater = zlib.decompressobj()
            text = inflater.decompress(match.group(1), MAX_INFLATED - inflated_total)
        except zlib.error:
            continue
        inflated_total += len(text)
        pages = max(pages, _tree_count(text))
        page_objects += len(_PAGE.findall(text))
    return pages or page_objects or None
