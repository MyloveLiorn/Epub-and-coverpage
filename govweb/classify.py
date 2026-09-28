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
    # Letters to Congress, court filings, and slides and handouts from meetings, which agency
    # sites (USCIS, DHS, Justice) publish by the hundred.
    "representative": -3, "senator": -3, "congressman": -3, "congresswoman": -3, "dkt": -3, "ecf": -3,
    "docket": -2, "exhibit": -2, "filing": -2, "motion": -2, "complaint": -2, "affidavit": -2, "stipulation": -2,
    "presentation": -2, "powerpoint": -2, "slide": -2, "webinar": -2, "infographic": -2, "poster": -2,
    "factsheet": -2, "engagement": -1, "talking": -1, "listening": -1, "sheet": -1, "transcript": -1,
}  # fmt: skip

# A court case's name: "Ahmed v. DHS", "Doe vs Smith"; but not "Title V Grants" or "Part V".
_CAPTION = re.compile(r"\b([a-z][\w.'&-]*)[\s_-]+vs?\.?[\s_-]+[a-z]", re.IGNORECASE)
_ROMAN_V_AFTER = {"title", "part", "chapter", "section", "volume", "vol", "phase", "appendix", "annex", "book",
                  "article", "form", "schedule", "level", "tier", "stage", "module", "unit", "grade", "class", "type",
                  "category", "division", "subpart", "subchapter", "table", "figure", "exhibit", "attachment",
                  "enclosure", "tab", "step", "lesson", "session", "year", "no", "number", "item"}  # fmt: skip


def is_court_case(text: str) -> bool:
    return any(m.group(1).lower().rstrip(".") not in _ROMAN_V_AFTER for m in _CAPTION.finditer(text))


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
    if is_court_case(title) or is_court_case(title_from_url(url)):
        score -= 3
    return max(-5, min(5, score))


# One book in many languages: "Asylum Guide - Spanish", "Asylum Guide (Haitian Creole)",
# "asylum-guide-es.pdf". Only a language at the start or end of the title, or in brackets,
# marks a translation; "Chinese Immigration to America" is its own book.
LANGUAGE_NAMES = [
    "english", "spanish", "espanol", "español", "french", "francais", "français", "haitian creole", "haitian",
    "creole", "kreyol", "kreyòl", "arabic", "chinese", "simplified chinese", "traditional chinese", "mandarin",
    "cantonese", "russian", "korean", "vietnamese", "tagalog", "filipino", "dari", "pashto", "farsi", "persian",
    "urdu", "hindi", "bengali", "punjabi", "burmese", "armenian", "amharic", "nepali", "indonesian", "portuguese",
    "somali", "swahili", "tigrinya", "ukrainian", "polish", "japanese", "german", "italian", "hmong", "khmer",
    "lao", "thai", "turkish", "uzbek", "kinyarwanda", "kirundi", "karen", "chuukese", "marshallese", "samoan",
    "tongan", "ilocano", "greek", "hebrew", "romanian", "albanian", "bosnian", "serbian", "croatian", "oromo",
    "dinka", "nuer", "pular", "wolof", "fulani", "mam", "kiche", "k'iche'", "quiche",
]  # fmt: skip
_LANG = "|".join(re.escape(n).replace(r"\ ", r"\s+") for n in sorted(LANGUAGE_NAMES, key=len, reverse=True))
_QUALIFIER = r"(?:\s+(?:simplified|traditional|version|translation|language|edition))*"
_LANG_END = re.compile(
    rf"(?:[\s\-–—:|,/]*[(\[]?\s*(?:in\s+|en\s+)?(?:{_LANG}){_QUALIFIER}\s*[)\]]?)+\s*$", re.IGNORECASE
)
_LANG_START = re.compile(rf"^\s*[(\[]?\s*(?:{_LANG}){_QUALIFIER}\s*[)\]]?\s*[-–—:|]\s*", re.IGNORECASE)
_LANG_BRACKETS = re.compile(rf"[(\[]\s*(?:{_LANG}){_QUALIFIER}\s*[)\]]", re.IGNORECASE)
# Language codes at the end of a file name: "guide-es", "guide_SP".
_LANG_CODE_END = re.compile(r"[\s_\-(\[]+(?:es|sp|spa|fr|zh|ko|vi|ru|ht|pt|tl|ar|en|eng)[)\]]?\s*$", re.IGNORECASE)


def split_language(title: str, file_name: bool = False) -> tuple[str, str]:
    """ "Asylum Guide - Spanish" -> ("Asylum Guide", "spanish"); ("Asylum Guide", "") without one.
    With ``file_name``, a language code at the end counts too ("asylum guide es")."""
    language: list[str] = []
    for pattern in (_LANG_BRACKETS, _LANG_START, _LANG_END):
        found: list[str] = []
        stripped = pattern.sub(lambda m, found=found: found.append(m.group(0)) or " ", title)
        if stripped.strip(" -|:._,"):  # a title that is only a language stays as it is
            title, language = stripped, language + found
    if file_name:
        title = _LANG_CODE_END.sub(lambda m: language.append(m.group(0)) or "", title)
    base = " ".join(title.split()).strip(" -|:._,")
    return base, " ".join(" ".join(part.strip(" -|:._,()[]") for part in language).lower().split())


def title_key(title: str) -> str:
    """The title's words without the language it is in, for telling translations of a book apart."""
    return " ".join(_words(split_language(title)[0]))
