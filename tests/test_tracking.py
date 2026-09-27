from govbooks import db
from govbooks.market.base import ProviderError
from govbooks.models import Listing
from govbooks.tracking import run_tracking
from tests.test_market import TITLE, FakeProvider, listing, seed_book


class LookupProvider(FakeProvider):
    def __init__(self, results, lookups):
        super().__init__(results)
        self.lookups = lookups

    def lookup(self, asins):
        return [self.lookups[a] for a in asins if a in self.lookups]


def messages(changes):
    return [c.message for c in changes]


def test_book_watch_reports_new_competitors(conn):
    seed_book(conn)
    db.add_watch(conn, label="canning", book_id="b1")
    provider = FakeProvider({TITLE: [listing("A1", rank=100_000, price=9.99)]})
    assert messages(run_tracking(conn, provider)) == ["first snapshot: 1 listing(s), best rank 100,000, price $9.99"]

    provider.results[TITLE] = [listing("A1", rank=50_000, price=9.99), listing("A2", rank=70_000, price=11.99)]
    assert messages(run_tracking(conn, provider)) == [
        "new listing(s): A2",
        "sales rank improved: 100,000 -> 50,000",
        "price: $9.99 -> $10.99",
    ]
    assert len(db.snapshots_for(conn, 1)) == 2


def test_asin_watch_tracks_rank_and_price(conn):
    db.add_watch(conn, label="some book", asin="B0001")
    provider = LookupProvider({}, {"B0001": Listing("B0001", "Some book", sales_rank=10_000, price=5.0)})
    run_tracking(conn, provider)
    provider.lookups["B0001"] = Listing("B0001", "Some book", sales_rank=10_500, price=5.0)
    assert run_tracking(conn, provider) == []  # a 5% rank move is noise


def test_failed_lookup_is_reported_not_raised(conn):
    db.add_watch(conn, label="some book", asin="B0001")

    class Broken(FakeProvider):
        def lookup(self, asins):
            raise ProviderError("quota exhausted")

    assert messages(run_tracking(conn, Broken({}))) == ["not checked: quota exhausted"]
    assert db.snapshots_for(conn, 1) == []


def test_add_watch_is_idempotent(conn):
    seed_book(conn)
    first = db.add_watch(conn, label="x", book_id="b1")
    db.deactivate_watch(conn, first)
    assert db.add_watch(conn, label="x", book_id="b1") == first
    assert len(db.list_watches(conn)) == 1
