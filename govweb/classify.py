"""Telling documents and publication pages apart from the rest of a website."""

from __future__ import annotations

import html
import re
from urllib.parse import unquote, urlsplit

from govbooks.text import contains_phrase, normalize_text

DOCUMENT_TYPES = {".pdf": "pdf", ".epub": "epub", ".mobi": "mobi", ".azw3": "azw3", ".docx": "docx", ".doc": "doc"}

# Words that mark a page as a list of publications worth crawling first.
PUBLICATION_HINTS = [
    "publication", "publications", "pubs", "library", "e-library", "reading room", "readingroom",
    "reports", "documents", "books", "bookstore", "ebook", "ebooks", "e-book", "handbook", "handbooks",
    "guide", "guides", "manual", "manuals", "bulletin", "bulletins", "field guide", "catalog",
    "resources", "research", "fact sheets", "factsheets", "brochures", "series",
]  # fmt: skip

# Link texts that say nothing about the document itself (compared on their words only, so
# "Download »" counts too).
GENERIC_LINK_TEXT = {"", "pdf", "download", "download pdf", "here", "click here", "view", "view pdf", "link",
                     "read more", "more", "open", "full text", "epub", "document", "file", "download now",
                     "download file", "download document", "download report", "click here to download", "read",
                     "learn more", "full report", "report", "access", "details", "view document", "view report"}  # fmt: skip
# File names that say nothing either: /ReadLibraryItem.ashx?id=12, /download.aspx?file=9, /get?id=77
GENERIC_FILE_NAMES = {"readlibraryitem", "download", "downloads", "file", "files", "get", "getfile", "view",
                      "viewdocument", "document", "documents", "showdocument", "attachment", "index", "default",
                      "item", "fetch", "content", "resource", "blob", "handler", "pdf"}  # fmt: skip

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


def topic_phrases(keywords: list[str]) -> list[str]:
    """Topic keywords in the normalized form topic_score matches against."""
    return [p for p in (normalize_text(k) for k in keywords) if p]


def topic_score(url: str, text: str, phrases: list[str]) -> int:
    """How many topic keywords a link mentions, in its address or its text ("honey-bees" in a
    URL matches the keyword "honey bee")."""
    if not phrases:
        return 0
    haystack = normalize_text(f"{unquote(urlsplit(url).path)} {text}")
    return sum(contains_phrase(haystack, p) for p in phrases)


# Parts of a site's name that suggest it publishes books: armypubs.army.mil, history.army.mil,
# armyupress.army.mil, nal.usda.gov (National Agricultural Library), alabamaarchives.gov.
PUBLISHER_HOST_HINTS = ("pub", "histor", "librar", "press", "book", "manual", "doctrine", "museum", "heritage",
                        "archiv", "learn", "universit", "college", "school", "research", "geolog", "extension")  # fmt: skip
PUBLISHER_HOST_LABELS = {"nal", "nlm", "gpo", "apd", "tsl"}


def host_hint_score(host: str, phrases: list[str] | None = None) -> int:
    """How strongly a site's name suggests books: publisher words, then topic keywords."""
    labels = host.lower().removeprefix("www.").split(".")[:-1]  # "armypubs.army.mil" -> ["armypubs", "army"]
    score = 2 * sum(1 for hint in PUBLISHER_HOST_HINTS if any(hint in label for label in labels))
    score += 2 * sum(1 for label in labels if label in PUBLISHER_HOST_LABELS)
    compact = "".join(labels).replace("-", "")
    return score + sum(1 for p in phrases or [] if p.replace(" ", "") in compact)


def title_from_url(url: str) -> str:
    """ "/files/Complete_Guide-Home%20Canning_2015.pdf" -> "Complete Guide Home Canning 2015" """
    name = unquote(urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1])
    name = re.sub(r"\.[a-z0-9]{2,5}$", "", name, flags=re.IGNORECASE)
    return " ".join(re.split(r"[\s_\-+.]+", name)).strip()


def clean_title(text: str) -> str:
    """ "K9H2F Handbook 2026 K9H2F Handbook 2026" -> "K9H2F Handbook 2026", "Don&#39;t" -> "Don't",
    and no trailing dot or separator."""
    text = " ".join(html.unescape(html.unescape(text)).split())
    words = text.split()
    half = len(words) // 2
    if half and len(words) % 2 == 0 and words[:half] == words[half:]:
        text = " ".join(words[:half])
    return text.strip(" -|:._")


def is_generic_title(title: str) -> bool:
    return " ".join(_WORDS.findall(title.lower())) in GENERIC_LINK_TEXT


def meaningful_file_name(url: str) -> bool:
    """False for file names like "ReadLibraryItem", "download" or "3f9a2c7e11"."""
    words = [w for w in _words(title_from_url(url)) if not re.fullmatch(r"\d+|[0-9a-f]{8,}", w)]
    return bool(words) and "".join(words) not in GENERIC_FILE_NAMES


def best_title(link_text: str, url: str, context: str = "") -> str:
    """The link text, or the file name when the text says nothing ("Download"), or ``context`` (the
    title of the page linking to it) when the file name says nothing either."""
    text = clean_title(link_text)
    cleaned = re.sub(r"\(?\b(pdf|epub|mobi)\b[^)]*\)?", "", text, flags=re.IGNORECASE).strip(" -|:")
    if is_generic_title(cleaned) or len(cleaned) < 4:
        if not meaningful_file_name(url) and context.strip():
            return clean_title(context)[:300]
        return title_from_url(url)
    return cleaned[:300]


def same_site(url: str, domain: str) -> bool:
    host = urlsplit(url).netloc.lower().split(":")[0]
    return host == domain or host.endswith("." + domain)


# Words that make a document look like a book worth republishing, and words that mark
# paperwork. Matched as whole words in the title and file name; plurals count too.
BOOK_WORDS = {
    "handbook": 2, "manual": 2, "guide": 2, "guidebook": 2, "book": 2, "cookbook": 2, "atlas": 2,
    "encyclopedia": 2, "primer": 2, "textbook": 2, "workbook": 2, "almanac": 2, "yearbook": 2,
    "history": 1, "bulletin": 1, "report": 1, "proceedings": 1, "series": 1, "curriculum": 1,
    "story": 1, "stories": 1, "field": 1, "introduction": 1, "principles": 1,
}  # fmt: skip
PAPERWORK_WORDS = {
    "agenda": -3, "minutes": -3, "invoice": -3, "receipt": -3, "rfp": -3, "form": -2, "application": -2,
    "permit": -2, "notice": -2, "flyer": -2, "memo": -2, "memorandum": -2, "resolution": -2, "ordinance": -2,
    "contract": -2, "bid": -2, "schedule": -2, "calendar": -2, "press": -1, "release": -1, "newsletter": -1,
    "checklist": -1, "packet": -1, "budget": -1, "audit": -1, "testimony": -1, "letter": -1,
}  # fmt: skip


_TITLE_STOPWORDS = {"the", "and", "for", "with", "from", "into", "pdf", "doc", "file", "download"}


def book_score(title: str, url: str) -> int:
    """Above zero: looks like a book or book-length guide. Below zero: forms, agendas, minutes.

    A descriptive title of three or more real words ("Beekeeping in the United States") earns a
    point even without a word like "guide", since many books are titled that way."""
    words = set(_words(f"{title} {title_from_url(url)}"))
    words |= {w[:-1] for w in words if len(w) > 3 and w.endswith("s")}
    score = sum(BOOK_WORDS.get(w, 0) + PAPERWORK_WORDS.get(w, 0) for w in words)
    title_words = [w for w in _words(title or title_from_url(url)) if len(w) >= 3 and w not in _TITLE_STOPWORDS]
    if len(title_words) >= 3:
        score += 1
    return max(-5, min(5, score))
