"""Live smoke tests against real mirrors.

Run with:  pytest -m network -v
These are skipped by default so the normal suite is fully offline.
"""

from __future__ import annotations

import pytest

from libgen_service.api import LibgenAPI
from libgen_service.config import Settings


@pytest.fixture(scope="module")
def live_api(tmp_path_factory):
    settings = Settings.load().with_overrides(
        cache_dir=tmp_path_factory.mktemp("live-cache")
    )
    api = LibgenAPI(settings)
    yield api
    api.close()


@pytest.mark.network
def test_live_search_returns_books(live_api: LibgenAPI):
    result = live_api.search("clean code robert martin", per_page=10)
    assert result.items, "live search returned no results"
    assert result.total > 0
    book = result.items[0]
    assert book.title
    assert book.files
    assert book.files[0].md5


@pytest.mark.network
def test_live_mirror_health(live_api: LibgenAPI):
    health = live_api.health()
    assert health["healthy"] >= 1
    assert any(m["base"].startswith("https://") for m in health["mirrors"])


@pytest.mark.network
def test_live_resolve_download_link(live_api: LibgenAPI):
    result = live_api.search("python essentials", per_page=5)
    assert result.items
    md5 = result.items[0].files[0].md5
    info = live_api.resolve(md5)
    assert "get.php" in info["url"]
    assert "ads.php" not in info["url"]
