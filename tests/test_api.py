from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from libgen_service.api import LibgenAPI, cover_bucket
from libgen_service.errors import DownloadError, InvalidInput, NotFound
from tests.conftest import DOWNLOAD_PAYLOAD, FakeClient

HARRY_MD5 = "184c16eae281bf2ebea72eb6d7aa6f6d"
PYTHON_MD5 = "abed2924cd3eedf0fac0c8d94a22620a"


def test_search_returns_books(api: LibgenAPI):
    result = api.search("harry potter")
    assert result.total == 2
    assert result.found == 2
    assert len(result.items) == 2
    titles = {book.title for book in result.items}
    assert "Harry Potter à l'école des sorciers 1" in titles
    harry = next(b for b in result.items if b.edition_id == "144156688")
    assert harry.language == "fre"
    assert harry.language_name == "French"
    assert harry.files[0].md5 == HARRY_MD5
    assert harry.files[0].ext == "epub"
    assert harry.files[0].size == 350208
    assert harry.to_dict()["cover_url"] == f"/api/cover/{HARRY_MD5}"


def test_search_filters_by_extension(api: LibgenAPI):
    only_pdf = api.search("harry potter", ext="pdf")
    assert only_pdf.items == []
    assert only_pdf.found == 0

    only_epub = api.search("harry potter", ext="epub")
    assert len(only_epub.items) == 2


def test_search_filters_by_language_and_year(api: LibgenAPI):
    assert len(api.search("harry potter", lang="fre").items) == 1
    assert len(api.search("harry potter", lang="eng").items) == 1
    assert len(api.search("harry potter", year_from=2020).items) == 1
    assert len(api.search("harry potter", year_to=2010).items) == 0


def test_search_pagination(api: LibgenAPI):
    first = api.search("harry potter", per_page=1, page=1)
    second = api.search("harry potter", per_page=1, page=2)
    assert first.pages == 2
    assert len(first.items) == 1 and len(second.items) == 1
    assert first.items[0].edition_id != second.items[0].edition_id


def test_search_sorting_by_title(api: LibgenAPI):
    result = api.search("harry potter", sort="title")
    titles = [book.title for book in result.items]
    assert titles == sorted(titles, key=str.casefold)


def test_search_by_md5(api: LibgenAPI):
    result = api.search(PYTHON_MD5)
    assert result.mode == "md5"
    assert result.items
    assert result.items[0].title.startswith("Python Essentials 1")


def test_search_isbn_follows_edition_fallback(api: LibgenAPI):
    result = api.search("9785557654326", mode="isbn")
    assert result.mode == "isbn"
    assert result.total == 1
    assert len(result.items) == 1
    book = result.items[0]
    assert book.edition_id == "144156688"
    assert book.files[0].md5 == HARRY_MD5
    assert book.files[0].ext == "epub"


def test_search_validation(api: LibgenAPI):
    with pytest.raises(InvalidInput):
        api.search("   ")
    with pytest.raises(InvalidInput):
        api.search("abc", mode="md5")
    with pytest.raises(InvalidInput):
        api.search("x", topic="nope")
    with pytest.raises(InvalidInput):
        api.search("x", sort="nope")


def test_book_details(api: LibgenAPI):
    book = api.book("147748488")
    assert book.title.startswith("Python Essentials 1")
    assert book.author == "The OpenEDG Python Institute"
    assert book.language == "eng"
    assert "9798987762202" in book.isbn
    assert "Unleash Your Potential" in book.description
    assert "Welcome to Python Essentials 1" in book.toc
    assert book.files[0].ext == "epub"
    assert book.files[0].size == 3625273
    data = book.to_dict(detailed=True)
    assert data["description"]
    assert data["files"][0]["download_url"] == f"/api/download/{PYTHON_MD5}"


def test_author_falls_back_to_add_key_401(api: LibgenAPI):
    edition = {
        "title": "Normalized record",
        "author": "",
        "add": {
            "1": {"key": "401", "value": "Doe, John"},
            "2": {"key": "401", "value": "Roe, Jane"},
        },
        "files": {},
    }
    book = api._book_from("1", edition, {})
    assert book.author == "Doe, John, Roe, Jane"


def test_book_not_found(api: LibgenAPI):
    with pytest.raises(NotFound):
        api.book("999999999")
    with pytest.raises(InvalidInput):
        api.book("not-a-number")


def test_file_info_by_md5(api: LibgenAPI):
    info = api.file(PYTHON_MD5)
    assert info["md5"] == PYTHON_MD5
    assert info["extension"] == "epub"
    assert info["editions"][0]["title"].startswith("Python Essentials 1")
    with pytest.raises(InvalidInput):
        api.file("nope")


def test_resolve_skips_ad_page(api: LibgenAPI):
    info = api.resolve(HARRY_MD5)
    assert "ads.php" not in info["url"]
    assert "get.php?md5=" in info["url"]
    assert "key=TESTKEY123" in info["url"]
    # cached on the second call
    again = api.resolve(HARRY_MD5)
    assert again["url"] == info["url"]


def test_download_verifies_md5(api: LibgenAPI, payload_md5: str, tmp_path: Path):
    target = tmp_path / "books"
    result = api.download_to(payload_md5, target)
    path = Path(result["path"])
    assert path.exists()
    assert path.read_bytes() == DOWNLOAD_PAYLOAD
    assert result["verified"] is True
    assert hashlib.md5(path.read_bytes()).hexdigest() == payload_md5


def test_download_rejects_corrupted_file(tmp_path: Path):
    client = FakeClient(corrupt=True)
    from libgen_service.cache import TTLCache
    from libgen_service.config import Settings

    settings = Settings(
        mirrors=("https://libgen.li",),
        cache_dir=tmp_path / "cache",
        min_interval=0.0,
    )
    api = LibgenAPI(
        settings,
        client=client,
        cache=TTLCache(tmp_path / "cache" / "cache.sqlite3", 600),
    )
    try:
        with pytest.raises(DownloadError):
            api.download_to("abed2924cd3eedf0fac0c8d94a22620a", tmp_path / "books")
        leftovers = list((tmp_path / "books").glob("*"))
        assert leftovers == []
    finally:
        api.close()


def test_cover_bucket_matches_real_paths():
    # Observed on libgen.li: /covers/5880000/... for id 5880701,
    # /covers/5545000/... for id 5545006.
    assert cover_bucket(5880701) == 5880000
    assert cover_bucket(5545006) == 5545000
    assert cover_bucket("5902123") == 5902000


def test_cover_is_cached(api: LibgenAPI):
    data, content_type = api.cover(HARRY_MD5, "4920181")
    assert data.startswith(b"\xff\xd8\xff")
    assert content_type == "image/jpeg"
    # second call must be served from disk
    again, _ = api.cover(HARRY_MD5, "4920181")
    assert again == data


def test_mirror_health_shape(api: LibgenAPI):
    health = api.health()
    assert health["status"] == "ok"
    assert health["mirrors"][0]["base"] == "https://libgen.li"
