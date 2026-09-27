from govbooks.sources.google_books import GoogleBooksSource
from govbooks.sources.govinfo import GovInfoSource
from govbooks.sources.internet_archive import InternetArchiveSource
from govbooks.sources.open_library import OpenLibrarySource


def test_govinfo_search_pages_with_offset_mark(fake_http):
    pages = iter([
        {"count": 2, "offsetMark": "next1", "results": [
            {"title": "Beekeeping in the United States", "packageId": "GOVPUB-A1-123", "dateIssued": "1980-01-01",
             "governmentAuthor": ["Department of Agriculture"], "collectionCode": "GPO"},
        ]},
        {"count": 2, "offsetMark": "next2", "results": [
            {"title": "Honey Bee Pests", "packageId": "GOVPUB-A1-456", "granuleId": "G1", "dateIssued": "2001"},
        ]},
        {"count": 2, "offsetMark": "next2", "results": []},
    ])  # fmt: skip
    fake_http.add("POST", "api.govinfo.gov/search", lambda url, **kw: next(pages))
    records = list(GovInfoSource(fake_http, "KEY", ["GPO", "CPRT"]).search("beekeeping", limit=10))

    assert [r.source_id for r in records] == ["GOVPUB-A1-123", "GOVPUB-A1-456/G1"]
    assert records[0].authors == ["Department of Agriculture"]
    assert records[0].year == 1980
    assert records[1].url == "https://www.govinfo.gov/app/details/GOVPUB-A1-456/G1"
    first = fake_http.calls[0][2]
    assert first["params"] == {"api_key": "KEY"}
    assert first["json"]["query"] == 'title:("beekeeping") AND collection:(GPO OR CPRT)'
    assert first["json"]["offsetMark"] == "*"
    assert fake_http.calls[1][2]["json"]["offsetMark"] == "next1"


def test_govinfo_skips_author_searches(fake_http):
    assert list(GovInfoSource(fake_http, "KEY", []).search("bees", author="Forest Service")) == []
    assert fake_http.calls == []


def test_internet_archive_parses_mixed_field_types(fake_http):
    fake_http.add("GET", "archive.org/advancedsearch.php", {"response": {"numFound": 1, "docs": [
        {"identifier": "beekeeping00usda", "title": ["Beekeeping for beginners"],
         "creator": "United States. Department of Agriculture", "publisher": ["U.S. Govt. Print. Off."],
         "date": "1943-01-01T00:00:00Z", "subject": ["Bees; Honey", "Apiculture"],
         "description": "<p>A <b>farmers' bulletin</b></p>"},
        {"identifier": "no-title"},
    ]}})  # fmt: skip
    records = list(InternetArchiveSource(fake_http, ["fedlink"]).search("beekeeping"))

    assert len(records) == 1
    record = records[0]
    assert record.title == "Beekeeping for beginners"
    assert record.authors == ["United States. Department of Agriculture"]
    assert record.publisher == "U.S. Govt. Print. Off."
    assert record.year == 1943
    assert record.subjects == ["Bees", "Honey", "Apiculture"]
    assert "farmers' bulletin" in record.description and "<b>" not in record.description
    assert record.fulltext_url == "https://archive.org/details/beekeeping00usda"
    query = fake_http.calls[0][2]["params"]["q"]
    assert 'title:("beekeeping")' in query and "collection:(fedlink)" in query and "mediatype:texts" in query


def test_internet_archive_author_search_uses_creator(fake_http):
    fake_http.add("GET", "archive.org", {"response": {"numFound": 0, "docs": []}})
    list(InternetArchiveSource(fake_http, ["fedlink"]).search("bees", author="Forest Service"))
    query = fake_http.calls[0][2]["params"]["q"]
    assert 'creator:("Forest Service")' in query and "collection" not in query


def test_open_library(fake_http):
    fake_http.add("GET", "openlibrary.org/search.json", {"numFound": 1, "docs": [
        {"key": "/works/OL1W", "title": "Complete guide to home canning", "author_name": ["United States. Extension Service"],
         "publisher": ["Dover", "U.S. Dept. of Agriculture"], "first_publish_year": 1988, "subject": ["Canning and preserving"],
         "isbn": ["9780486453415"], "ia": ["completeguidetoh00unit"], "ebook_access": "public"},
    ]})  # fmt: skip
    records = list(OpenLibrarySource(fake_http).search("home canning"))
    record = records[0]
    assert record.publisher == "U.S. Dept. of Agriculture"  # the government publisher wins
    assert record.public_domain is True
    assert record.fulltext_url == "https://archive.org/details/completeguidetoh00unit"
    assert fake_http.calls[0][2]["params"]["q"].startswith('(title:"home canning" OR subject:"home canning") AND publisher:(')


def test_google_books_searches_both_gpo_names(fake_http):
    volume = {"id": "abc", "volumeInfo": {"title": "Survival", "authors": ["United States. Department of the Army"],
              "publisher": "U.S. Government Printing Office", "publishedDate": "1970",
              "industryIdentifiers": [{"type": "ISBN_10", "identifier": "0000000000"}, {"type": "OTHER", "identifier": "x"}],
              "categories": ["Wilderness survival"], "infoLink": "https://books.google.com/abc"},
              "accessInfo": {"publicDomain": True, "viewability": "ALL_PAGES", "webReaderLink": "https://read/abc"}}  # fmt: skip
    fake_http.add("GET", "googleapis.com/books", {"items": [volume]})
    records = list(GoogleBooksSource(fake_http, "GKEY").search("survival"))

    assert len(records) == 2  # one per GPO spelling; merged later by discover
    assert records[0].isbns == ["0000000000"]
    assert records[0].public_domain is True
    assert records[0].fulltext_url == "https://read/abc"
    queries = [call[2]["params"]["q"] for call in fake_http.calls]
    assert queries == ['"survival" inpublisher:"Government Printing Office"',
                       '"survival" inpublisher:"Government Publishing Office"']  # fmt: skip
    assert fake_http.calls[0][2]["params"]["key"] == "GKEY"
