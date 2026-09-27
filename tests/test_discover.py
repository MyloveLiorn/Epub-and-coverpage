from datetime import date

from govbooks import db
from govbooks.agencies import AgencyIndex
from govbooks.config import Topic
from govbooks.discover import assess_rights, discover, score_topic
from govbooks.http import HttpError
from govbooks.models import Record

TOPIC = Topic(name="beekeeping", keywords=["beekeeping", "honey bee"], exclude=["honey bee virus"], min_score=3)


class FakeSource:
    def __init__(self, name, records, fail=False, status=503):
        self.name = name
        self.records = records
        self.fail = fail
        self.status = status
        self.calls = []

    def search(self, keyword, author=None, limit=100):
        self.calls.append((keyword, author))
        if self.fail:
            raise HttpError(self.status, "https://example", "down")
        if keyword == "beekeeping":
            yield from self.records


def rec(source, title, year=1943, authors=(), publisher=None, **kw):
    return Record(source=source, source_id=f"{source}:{title}", title=title, year=year,
                  authors=list(authors), publisher=publisher, **kw)  # fmt: skip


def test_score_topic_weights_title_subjects_description():
    record = rec("x", "Beekeeping basics", subjects=["Honey bees"], description="All about beekeeping.")
    assert score_topic(TOPIC, record) == 3 + 2 + 1
    assert score_topic(TOPIC, rec("x", "Road maintenance")) == 0
    assert score_topic(TOPIC, rec("x", "Honey bee virus survey")) == 0  # excluded


def test_assess_rights():
    today = date(2026, 9, 27)
    assert assess_rights(None, 1930, None, today)[0] == "public_domain"
    assert assess_rights("state", 1931, None, today)[0] == "check"
    assert assess_rights("federal", 1990, None, today)[0] == "likely_public_domain"
    assert assess_rights(None, 2000, True, today)[0] == "public_domain"
    assert assess_rights(None, 2000, None, today)[0] == "unknown"


def test_discover_merges_sources_and_filters(conn, agencies):
    usda_book = rec("ia", "Beekeeping for beginners", authors=["United States. Department of Agriculture"],
                    fulltext_url="https://archive.org/details/x")  # fmt: skip
    same_book = rec("gb", "Beekeeping for Beginners: a manual", publisher="U.S. Government Printing Office",
                    isbns=["0306406152"], subtitle="a manual")  # fmt: skip
    commercial = rec("gb", "Beekeeping for profit", publisher="Penguin Books")
    irrelevant = rec("ia", "Soil survey of Iowa", authors=["Soil Conservation Service"])
    sources = [FakeSource("ia", [usda_book, irrelevant]), FakeSource("gb", [same_book, commercial])]

    stats = discover(conn, TOPIC, sources, AgencyIndex(agencies))

    assert stats.queries == 4  # 2 keywords x 2 sources
    assert (stats.records, stats.kept, stats.new_books) == (4, 2, 1)
    books = db.list_books(conn, topic="beekeeping")
    assert len(books) == 1
    book = books[0]
    assert book["agency_name"] == "Agriculture Department"
    assert book["level"] == "federal"
    assert book["rights"] == "likely_public_domain"
    assert book["subtitle"] == "a manual"
    assert db.loads(book["isbns"]) == ["0306406152"]
    assert {s["source"] for s in db.loads(book["sources"])} == {"ia", "gb"}
    assert book["fulltext_url"] == "https://archive.org/details/x"


def test_discover_by_agency_assigns_that_agency(conn, agencies):
    dwr = next(a for a in agencies if a.id == "wd:Q5020016")
    record = rec("ol", "Honey bee pollination of almonds", year=1999, authors=["Water Resources"])
    source = FakeSource("ol", [record])
    discover(conn, TOPIC, [source], AgencyIndex(agencies), agencies=[dwr])

    assert source.calls[0] == ("beekeeping", "California Department of Water Resources")
    book = db.list_books(conn)[0]
    assert (book["agency_id"], book["level"], book["jurisdiction"]) == ("wd:Q5020016", "state", "California")
    assert book["rights"] == "likely_public_domain"  # California: public records are free to reuse


def test_discover_survives_a_failing_source(conn, agencies):
    good = FakeSource("ok", [rec("ok", "Beekeeping", publisher="Government Printing Office")])
    stats = discover(conn, TOPIC, [FakeSource("bad", [], fail=True), good], AgencyIndex(agencies))
    assert len(stats.errors) == 2
    assert stats.new_books == 1


def test_discover_stops_querying_a_source_over_its_quota(conn, agencies):
    topic = Topic(name="t", keywords=["a", "b", "c", "d", "e"])
    limited = FakeSource("limited", [], fail=True, status=429)
    discover(conn, topic, [limited], AgencyIndex(agencies))
    assert len(limited.calls) == 3


def test_discover_stops_querying_an_unreachable_source(conn, agencies):
    topic = Topic(name="t", keywords=["a", "b", "c", "d", "e"])
    offline = FakeSource("offline", [], fail=True, status=None)
    stats = discover(conn, topic, [offline], AgencyIndex(agencies))
    assert len(offline.calls) == 3
    assert len(stats.errors) == 3
