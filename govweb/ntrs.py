"""NASA's Technical Reports Server (ntrs.nasa.gov): a public search API over NASA's reports,
mission reports, technical memoranda and conference papers, with links to their PDFs.

Crawling can't reach these: the NTRS website is a search application. Its results are stored as
the documents of the site ntrs.nasa.gov, so the Amazon check and the results tables treat them
like documents found on any other site.
"""

from __future__ import annotations

from govweb.classify import clean_title, document_type
from govweb.crawl import FoundDocument, SiteResult

NTRS = "https://ntrs.nasa.gov"
SEARCH_URL = NTRS + "/api/citations/search"
PAGE_SIZE = 100  # the most NTRS returns per request


def search(http, query: str, limit: int = 300) -> list[dict]:
    """Citations matching ``query``, most relevant first."""
    results: list[dict] = []
    while len(results) < limit:
        data = http.get_json(SEARCH_URL, params={"q": query, "page.size": PAGE_SIZE, "page.from": len(results)})
        batch = data.get("results") or []
        results += batch
        total = (data.get("stats") or {}).get("total")
        if not batch or (total is not None and len(results) >= total):
            break
    return results[:limit]


def _download_url(citation: dict) -> str | None:
    """The link to the citation's PDF (or other file), if NTRS has one."""
    for download in citation.get("downloads") or []:
        links = download.get("links") or {}
        link = links.get("original") or links.get("pdf")
        if not link and download.get("name"):
            link = f"/api/citations/{citation.get('id')}/downloads/{download['name']}"
        if link:
            return link if link.startswith("http") else NTRS + link
    return None


def to_result(citations: list[dict]) -> SiteResult:
    """The citations with a public download, as a crawl result for the site ntrs.nasa.gov."""
    result = SiteResult("ntrs.nasa.gov", home_url=NTRS + "/")
    for citation in citations:
        if (citation.get("distribution") or "PUBLIC").upper() != "PUBLIC":
            continue
        url = _download_url(citation)
        title = clean_title(str(citation.get("title") or ""))
        if not url or not title:
            continue
        result.documents.setdefault(
            url,
            FoundDocument(url, title[:300], document_type(url) or "pdf", f"{NTRS}/citations/{citation.get('id')}", "api"),
        )
    return result
