import pytest

from govbooks import db
from govbooks.market import check_books, evaluate, topic_demand
from govbooks.market.base import ProviderError
from govbooks.market.catalog import CatalogProvider
from govbooks.market.creators import CreatorsProvider, parse_item
from govbooks.market.keepa import KeepaProvider
from govbooks.models import Listing

TITLE = "The Complete Guide to Home Canning"


def listing(asin, title=TITLE, rank=None, price=None):
    return Listing(asin=asin, title=title, sales_rank=rank, price=price)


def test_evaluate_open_gap_when_nobody_sells_it_but_topic_sells():
    result = evaluate(TITLE, [listing("A1", "Canning for dummies", rank=900)], topic_rank=20_000, rights="public_domain")
    assert result.verdict == "open_gap"
    assert result.matching == []
    assert result.score == 100  # 60 demand + 40 competition


def test_evaluate_proven_demand():
    listings = [listing("A1", rank=40_000, price=9.99), listing("A2", rank=900_000, price=14.99), listing("A3", "Other")]
    result = evaluate(TITLE, listings, topic_rank=None, rights="likely_public_domain")
    assert result.verdict == "proven_demand"
    assert result.best_rank == 40_000
    assert result.median_price == 12.49
    assert result.score == round((60 + 18) * 0.9)


def test_evaluate_crowded_and_rights_penalty():
    listings = [listing(f"A{i}", rank=100_000 + i) for i in range(5)]
    result = evaluate(TITLE, listings, topic_rank=None, rights="unknown")
    assert result.verdict == "crowded"
    assert result.score == round((45 + 8) * 0.4)


def test_evaluate_without_sales_ranks():
    result = evaluate(TITLE, [listing("A1")], topic_rank=None, rights="public_domain", has_rank=False)
    assert result.verdict == "some_competition"


def test_keepa_search_parses_stats_and_skips_non_books(fake_http):
    current = [1299, 999, -1, 45000]
    fake_http.add("GET", "api.keepa.com/search", {"products": [
        {"asin": "B01", "title": TITLE, "productGroup": "Book", "author": "USDA", "stats": {"current": current}},
        {"asin": "B02", "title": "Canning jar", "productGroup": "Kitchen", "stats": {"current": current}},
        {"asin": "B03", "title": "No stats", "productGroup": "eBooks"},
    ]})  # fmt: skip
    listings = KeepaProvider(fake_http, "KEY").search("home canning")

    assert [item.asin for item in listings] == ["B01", "B03"]
    assert (listings[0].sales_rank, listings[0].price, listings[0].authors) == (45000, 9.99, ["USDA"])
    assert listings[1].sales_rank is None and listings[1].price is None
    params = fake_http.calls[0][2]["params"]
    assert (params["key"], params["domain"], params["type"], params["term"]) == ("KEY", 1, "product", "home canning")


def test_keepa_error_is_raised(fake_http):
    fake_http.add("GET", "api.keepa.com", {"error": {"type": "invalidKey", "message": "bad key"}})
    with pytest.raises(ProviderError, match="bad key"):
        KeepaProvider(fake_http, "KEY").lookup(["B01"])


def test_creators_parses_camel_and_pascal_case():
    camel = {"asin": "B1", "detailPageURL": "https://amazon/B1",
             "itemInfo": {"title": {"displayValue": TITLE},
                          "byLineInfo": {"contributors": [{"name": "USDA", "role": "Author"}]}},
             "offersV2": {"listings": [{"price": {"money": {"amount": 12.5, "currency": "USD"}}}]},
             "browseNodeInfo": {"websiteSalesRank": {"salesRank": 1234}}}  # fmt: skip
    pascal = {"ASIN": "B2", "ItemInfo": {"Title": {"DisplayValue": "X"}},
              "BrowseNodeInfo": {"WebsiteSalesRank": {"SalesRank": 99}}}  # fmt: skip
    a, b = parse_item(camel), parse_item(pascal)
    assert (a.asin, a.title, a.sales_rank, a.price, a.authors, a.url) == ("B1", TITLE, 1234, 12.5, ["USDA"], "https://amazon/B1")
    assert (b.asin, b.title, b.sales_rank, b.price) == ("B2", "X", 99, None)


def test_creators_token_is_cached_and_sent(fake_http):
    fake_http.add("POST", "auth/o2/token", {"access_token": "TOKEN", "expires_in": 3600})
    fake_http.add("POST", "creatorsapi.amazon/catalog/v1/searchItems",
                  {"searchResult": {"items": [{"asin": "B1", "itemInfo": {"title": {"displayValue": TITLE}}}]}})  # fmt: skip
    provider = CreatorsProvider(fake_http, "ID", "SECRET", "tag-20")
    provider.search("home canning")
    provider.search("canning")

    token_calls = [c for c in fake_http.calls if "token" in c[1]]
    assert len(token_calls) == 1
    assert token_calls[0][2]["data"]["grant_type"] == "client_credentials"
    method, url, kwargs = fake_http.calls[1]
    assert kwargs["headers"]["Authorization"] == "Bearer TOKEN"
    assert kwargs["headers"]["x-marketplace"] == "www.amazon.com"
    assert kwargs["json"]["partnerTag"] == "tag-20"
    assert kwargs["json"]["searchIndex"] == "Books"


def test_creators_items_without_wrapper_and_regional_token(fake_http):
    from govbooks.market.creators import token_url_for

    fake_http.add("POST", "getItems", {"items": [{"asin": "B9", "itemInfo": {"title": {"displayValue": "T"}}}]})
    fake_http.add("POST", "api.amazon.co.uk/auth/o2/token", {"access_token": "T"})
    provider = CreatorsProvider(fake_http, "ID", "S", "tag", "www.amazon.de", token_url_for("www.amazon.de"))
    assert [item.asin for item in provider.lookup(["B9"])] == ["B9"]
    assert fake_http.calls[1][2]["json"]["itemIds"] == ["B9"]
    assert token_url_for("www.amazon.co.jp").endswith("amazon.co.jp/auth/o2/token")
    assert token_url_for("www.amazon.com").startswith("https://api.amazon.com/")


def test_creators_no_results_is_not_an_error(fake_http):
    fake_http.add("POST", "token", {"access_token": "T"})
    fake_http.add("POST", "searchItems", {"errors": [{"code": "NoResults", "message": "none"}]})
    assert CreatorsProvider(fake_http, "ID", "S", "tag").search("zzz") == []


def test_catalog_provider_turns_isbns_into_asins(fake_http):
    fake_http.add("GET", "googleapis.com/books", {"items": [
        {"volumeInfo": {"title": TITLE, "industryIdentifiers": [{"type": "ISBN_13", "identifier": "9780306406157"}]},
         "saleInfo": {"listPrice": {"amount": 7.99}}},
        {"volumeInfo": {"title": "No ISBN"}},
    ]})  # fmt: skip
    fake_http.add("GET", "openlibrary.org/search.json", {"docs": [
        {"title": TITLE, "isbn": ["0306406152"]},  # same edition as above
        {"title": TITLE + " (reprint)", "isbn": ["9780486453415"]},
    ]})  # fmt: skip
    listings = CatalogProvider(fake_http).search(TITLE)
    assert [item.asin for item in listings] == ["0306406152", "0486453413"]
    assert listings[0].price == 7.99
    assert listings[0].url == "https://www.amazon.com/dp/0306406152"


class FakeProvider:
    name = "fake"
    has_sales_rank = True

    def __init__(self, results):
        self.results = results
        self.queries = []

    def search(self, query, limit=10):
        self.queries.append(query)
        return self.results.get(query, [])

    def lookup(self, asins):
        return []


def seed_book(conn, book_id="b1", title=TITLE, rights="likely_public_domain"):
    stamp = db.now()
    db.save_book(conn, {
        "id": book_id, "title": title, "subtitle": None, "authors": [], "publisher": None, "year": 1988,
        "subjects": [], "description": None, "isbns": [], "agency_id": "fr:12", "level": "federal", "jurisdiction": "United States",
        "rights": rights, "rights_note": "", "fulltext_url": None, "sources": [], "first_seen": stamp,
        "last_seen": stamp,
    })  # fmt: skip
    db.set_book_topic(conn, book_id, "food_preservation", 6)
    conn.commit()


def test_topic_demand_uses_median_of_top_ranks():
    provider = FakeProvider({"canning": [listing("A", rank=10), listing("B", rank=30)], "pickling": [listing("C", rank=20)]})
    assert topic_demand(provider, ["canning", "pickling"]) == 20


def test_check_books_stores_results_and_skips_recent(conn):
    seed_book(conn)
    provider = FakeProvider({TITLE: [listing("A1", rank=40_000, price=9.99)]})
    books = db.list_books(conn)
    results = check_books(conn, provider, books)
    assert results[0][1].verdict == "proven_demand"

    row = db.list_books(conn)[0]
    assert (row["verdict"], row["best_rank"], row["provider"]) == ("proven_demand", 40_000, "fake")
    assert check_books(conn, provider, db.list_books(conn)) == []  # checked within 7 days
    assert len(check_books(conn, provider, db.list_books(conn), skip_checked_within_days=0)) == 1
