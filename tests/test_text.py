from govbooks.text import (
    isbn13_to_isbn10,
    main_title,
    normalize_name,
    normalize_text,
    parse_year,
    title_match,
    work_key,
)


def test_normalize_name_expands_catalog_abbreviations():
    assert normalize_name("U.S. Dept. of Agriculture") == "united states department of agriculture"
    assert normalize_name("U.S. Govt. Print. Off.") == "united states government printing office"
    assert normalize_name("Fish & Wildlife Service") == "fish and wildlife service"
    assert normalize_name(None) == ""


def test_normalize_text_folds_plurals_and_tags():
    assert normalize_text("Honey Bees & <b>Colonies</b>") == "honey bee and colony"
    assert normalize_text("Glass") == "glass"


def test_title_match_allows_reprint_noise():
    assert title_match("The Complete Guide to Home Canning", "Complete Guide to Home Canning (Classic Reprint)")
    assert not title_match("The Complete Guide to Home Canning", "Canning Vegetables at Home")


def test_title_match_is_strict_for_short_titles():
    assert title_match("Bees", "Bees")
    assert not title_match("Bees", "Bees of the World")


def test_work_key_ignores_subtitle_and_case():
    assert work_key("Beekeeping: A Manual", 1943) == work_key("BEEKEEPING", 1943)
    assert work_key("Beekeeping", 1943) != work_key("Beekeeping", 1950)
    assert main_title("Survival / Department of the Army") == "Survival"


def test_parse_year():
    assert parse_year("[1943?]") == 1943
    assert parse_year("2019-05-01") == 2019
    assert parse_year(["1901", "1902"]) == 1901
    assert parse_year("n.d.") is None
    assert parse_year(None) is None


def test_isbn13_to_isbn10():
    assert isbn13_to_isbn10("978-0-306-40615-7") == "0306406152"
    assert isbn13_to_isbn10("0306406152") == "0306406152"
    assert isbn13_to_isbn10("9791234567896") is None  # 979 prefixes have no ISBN-10
