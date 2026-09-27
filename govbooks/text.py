"""Text normalization used for matching agency names, topics and book titles."""

from __future__ import annotations

import difflib
import hashlib
import re

# Library catalogs abbreviate agency names heavily ("U.S. Dept. of Agric.").
ABBREVIATIONS = {
    "dept": "department",
    "depts": "departments",
    "admin": "administration",
    "adm": "administration",
    "natl": "national",
    "govt": "government",
    "gov": "government",
    "div": "division",
    "bur": "bureau",
    "agric": "agriculture",
    "assn": "association",
    "serv": "service",
    "svc": "service",
    "cong": "congress",
    "print": "printing",
    "off": "office",
    "supt": "superintendent",
    "docs": "documents",
    "doc": "documents",
    "comm": "commission",
    "dist": "district",
}

STOPWORDS = {"the", "a", "an", "of", "and", "for", "in", "on", "to", "with", "by", "at", "from"}

_US_DOTTED = re.compile(r"\bu\.\s*s\.(?:\s*a\.)?", re.IGNORECASE)
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_TAGS = re.compile(r"<[^>]+>")


def _tokens(text: str) -> list[str]:
    text = _US_DOTTED.sub(" united states ", text.lower())
    text = text.replace("&", " and ").replace("'", "")
    return [t for t in _NON_ALNUM.sub(" ", text).split() if t]


def normalize_name(text: str | None) -> str:
    """Normalize an agency, author or publisher name for comparison.

    >>> normalize_name("U.S. Dept. of Agriculture")
    'united states department of agriculture'
    """
    if not text:
        return ""
    words = [ABBREVIATIONS.get(t, t) for t in _tokens(text)]
    return " ".join(w for w in words if w != "the")


def _stem(word: str) -> str:
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def normalize_text(text: str | None) -> str:
    """Normalize free text (titles, subjects, descriptions) for keyword matching.

    Plurals are folded so that "honey bees" matches the keyword "honey bee".
    """
    if not text:
        return ""
    return " ".join(_stem(t) for t in _tokens(strip_tags(text)))


def contains_phrase(haystack: str, needle: str) -> bool:
    """Whole-word phrase containment on already-normalized strings."""
    return bool(needle) and f" {needle} " in f" {haystack} "


def strip_tags(text: str) -> str:
    return _TAGS.sub(" ", text)


def title_tokens(title: str) -> set[str]:
    return {t for t in normalize_text(title).split() if t not in STOPWORDS}


def title_match(book_title: str, other_title: str) -> bool:
    """Whether a listing title refers to the same book.

    Reprints tend to add words ("... (Classic Reprint)"), so this checks how much
    of the book's title appears in the other title rather than plain equality.
    """
    wanted = title_tokens(book_title)
    have = title_tokens(other_title)
    if not wanted or not have:
        return False
    if len(wanted) <= 2:
        a, b = normalize_text(book_title), normalize_text(other_title)
        return a == b or difflib.SequenceMatcher(None, a, b).ratio() >= 0.85
    return len(wanted & have) / len(wanted) >= 0.7


def main_title(title: str) -> str:
    """The title without its subtitle."""
    return re.split(r"\s*[:;]\s+|\s+/\s*", title, maxsplit=1)[0].strip() or title


def work_key(title: str, year: int | None) -> str:
    """A stable id for one edition of a work, shared across sources."""
    basis = f"{normalize_text(main_title(title))}|{year or ''}"
    return hashlib.sha1(basis.encode()).hexdigest()[:16]


def parse_year(value: object) -> int | None:
    """Pull a four-digit year out of dates like "1943", "1943-05-01" or "[1943?]"."""
    if value is None:
        return None
    if isinstance(value, list):
        value = value[0] if value else None
        if value is None:
            return None
    match = re.search(r"\b(1[5-9]\d\d|20\d\d)\b", str(value))
    return int(match.group(1)) if match else None


def as_list(value: object) -> list[str]:
    """Sources return a field as a string, a list of strings, or nothing."""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value if v not in (None, "")]
    return [str(value)] if value != "" else []


def isbn13_to_isbn10(isbn: str) -> str | None:
    """Convert a 978-prefixed ISBN-13 to ISBN-10, which is also the Amazon ASIN of a print book."""
    digits = re.sub(r"[^0-9Xx]", "", isbn)
    if len(digits) == 10:
        return digits.upper()
    if len(digits) != 13 or not digits.startswith("978"):
        return None
    core = digits[3:12]
    check = (11 - sum((10 - i) * int(d) for i, d in enumerate(core)) % 11) % 11
    return core + ("X" if check == 10 else str(check))
