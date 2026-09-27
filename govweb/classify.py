"""Telling documents and publication pages apart from the rest of a website."""

from __future__ import annotations

import re
from urllib.parse import unquote, urlsplit

DOCUMENT_TYPES = {".pdf": "pdf", ".epub": "epub", ".mobi": "mobi", ".azw3": "azw3", ".docx": "docx", ".doc": "doc"}

# Words that mark a page as a list of publications worth crawling first.
PUBLICATION_HINTS = [
    "publication", "publications", "pubs", "library", "e-library", "reading room", "readingroom",
    "reports", "documents", "books", "bookstore", "ebook", "ebooks", "e-book", "handbook", "handbooks",
    "guide", "guides", "manual", "manuals", "bulletin", "bulletins", "field guide", "catalog",
    "resources", "research", "fact sheets", "factsheets", "brochures", "series",
]  # fmt: skip

# Link texts that say nothing about the document itself.
GENERIC_LINK_TEXT = {"", "pdf", "download", "download pdf", "here", "click here", "view", "view pdf", "link",
                     "read more", "more", "open", "full text", "epub", "document", "file"}  # fmt: skip

_WORDS = re.compile(r"[a-z0-9]+")
_HINT_PHRASES = [tuple(h.replace("-", " ").split()) for h in PUBLICATION_HINTS]


def document_type(url: str) -> str | None:
    path = urlsplit(url).path.lower()
    for extension, kind in DOCUMENT_TYPES.items():
        if path.endswith(extension):
            return kind
    return None


def _words(text: str) -> list[str]:
    return _WORDS.findall(unquote(text).lower())


def hint_score(url: str, text: str = "") -> int:
    """How strongly a link looks like it leads to publications (0 = not at all)."""
    words = _words(urlsplit(url).path) + _words(text)
    joined = f" {' '.join(words)} "
    return sum(1 for phrase in _HINT_PHRASES if f" {' '.join(phrase)} " in joined)


def title_from_url(url: str) -> str:
    """ "/files/Complete_Guide-Home%20Canning_2015.pdf" -> "Complete Guide Home Canning 2015" """
    name = unquote(urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1])
    name = re.sub(r"\.[a-z0-9]{2,5}$", "", name, flags=re.IGNORECASE)
    return " ".join(re.split(r"[\s_\-+.]+", name)).strip()


def best_title(link_text: str, url: str) -> str:
    text = " ".join(link_text.split())
    cleaned = re.sub(r"\(?\b(pdf|epub|mobi)\b[^)]*\)?", "", text, flags=re.IGNORECASE).strip(" -|:")
    if cleaned.lower() in GENERIC_LINK_TEXT or len(cleaned) < 4:
        return title_from_url(url)
    return cleaned[:300]


def same_site(url: str, domain: str) -> bool:
    host = urlsplit(url).netloc.lower().split(":")[0]
    return host == domain or host.endswith("." + domain)
