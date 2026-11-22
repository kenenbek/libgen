from __future__ import annotations

from pathlib import Path

from libgen_service.parsers import (
    language_code,
    parse_batch_ids,
    parse_batch_result,
    parse_content_disposition,
    parse_edition_rows,
    parse_download_key,
    parse_file_id,
    parse_pages,
    parse_search_rows,
    parse_size,
    sanitize_filename,
)

FIXTURES = Path(__file__).parent / "fixtures"


def read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8-sig")


def test_batch_ids_from_real_page():
    total, ids = parse_batch_ids(read("batch.html"))
    assert total == 39704
    assert len(ids) > 50
    assert ids[0] == "149352601"
    assert ids[1] == "149428393"
    assert len(set(ids)) == len(ids)


def test_batch_ids_from_compact_page():
    total, ids = parse_batch_ids(read("batch_small.html"))
    assert total == 2
    assert ids == ["144156688", "147748488"]


def test_batch_result_exposes_followup_link():
    total, ids, link = parse_batch_result(read("batch_small.html"))
    assert total == 2
    assert ids == ["144156688", "147748488"]
    assert "/index.php?req=harry+potter" in link

    total, ids, link = parse_batch_result(read("batch_isbn_like.html"))
    assert total == 1
    assert ids == []
    assert "objects%5B%5D=e" in link


def test_edition_rows_have_no_file_columns():
    rows = parse_edition_rows(read("isbn_editions.html"))
    assert len(rows) == 25
    first = rows[0]
    assert first["edition_id"] == "135984510"
    assert first["title"].startswith("Clean Code")
    assert first["author"].startswith("Martin")
    assert first["publisher"].startswith("Prentice Hall")
    assert first["year"] == "2008"
    assert first["pages"] == 462
    assert first["language"] == "eng"
    assert first["md5"] == ""
    assert "9780132350884" in first["isbn"]


def test_download_key_is_extracted_from_ad_page():
    html = read("ads.html")
    key = parse_download_key(html, "56be2a6f3f21391fcc03c6cb452e208f")
    assert key == (
        "get.php?md5=56be2a6f3f21391fcc03c6cb452e208f&key=NJDM2S6S5HOFKCBK"
    )
    assert parse_download_key(html, "0" * 32) is None


def test_search_rows_real_libgen_page():
    rows = parse_search_rows(read("search.html"))
    assert len(rows) >= 20
    first = rows[0]
    assert first["md5"] == "56be2a6f3f21391fcc03c6cb452e208f"
    assert first["edition_id"] == "149352601"
    assert first["file_id"] == "106665206"
    assert first["title"].startswith("tutorial python 3.9")
    assert first["author"] == "python organization"
    assert first["publisher"] == "python organization"
    assert first["year"] == "2020"
    assert first["language"] == "ind"
    assert first["pages"] == 138
    assert first["ext"] == "pdf"
    assert first["size"] > 0
    assert first["topic"] == "l"
    assert "9789859203428" in first["isbn"]
    assert first["cover_url"].endswith("_small.jpg")


def test_search_rows_fiction_layout_has_no_cover_column():
    rows = parse_search_rows(read("fiction_search.html"))
    assert rows
    first = rows[0]
    assert first["md5"] == "184c16eae281bf2ebea72eb6d7aa6f6d"
    assert first["edition_id"] == "144156688"
    assert first["title"] == "Harry Potter à l'école des sorciers 1"
    assert first["author"].startswith("J. K. Rowling")
    assert first["language"] == "fre"
    assert first["ext"] == "epub"
    assert first["topic"] == "f"
    assert first["cover_url"] == ""


def test_size_and_page_parsing():
    assert parse_size("2 MB") == 2 * 1024**2
    assert parse_size("342 kB") == 342 * 1024
    assert parse_size("1.5 GB") == int(1.5 * 1024**3)
    assert parse_size("") == 0
    assert parse_pages("138 / 138") == 138
    assert parse_pages("") == 0


def test_language_codes():
    assert language_code("English") == "eng"
    assert language_code("Russian") == "rus"
    assert language_code("fre") == "fre"
    assert language_code("") == ""


def test_content_disposition_parsing():
    assert parse_content_disposition('attachment; filename="book.pdf"') == "book.pdf"
    assert (
        parse_content_disposition("attachment; filename*=UTF-8''%D0%BA%D0%BD%D0%B8%D0%B3%D0%B0.pdf")
        == "книга.pdf"
    )
    assert parse_content_disposition(None) == ""


def test_file_id_from_md5_page():
    assert parse_file_id(read("file_by_md5.html")) == "104838884"
    assert parse_file_id("<html>nothing</html>") == ""


def test_content_disposition_mojibake_and_escapes():
    raw = 'attachment; filename="Harry Potter Ã\u00a0 l&_039_Ã©cole.epub"'
    parsed = parse_content_disposition(raw)
    assert parsed == "Harry Potter à l&_039_école.epub"
    assert sanitize_filename(parsed) == "Harry Potter à l'école.epub"


def test_filename_sanitizing_blocks_traversal():
    assert sanitize_filename("../../etc/passwd") == "passwd"
    assert sanitize_filename("a:b*c?.pdf") == "a_b_c_.pdf"
    assert sanitize_filename("") == "book"
    long_name = "x" * 300 + ".pdf"
    cleaned = sanitize_filename(long_name)
    assert len(cleaned) <= 180
    assert cleaned.endswith(".pdf")
