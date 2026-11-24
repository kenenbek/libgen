from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from libgen_service.api import LibgenAPI
from libgen_service.server import LibgenServer

from tests.conftest import DOWNLOAD_PAYLOAD


class StubInfra:
    def report(self, *, refresh: bool = False, dumps: bool = False):
        return {
            "generated_at": "2026-11-24T12:00:00+00:00",
            "cached": False,
            "mirrors": [],
            "warnings": [],
            "dumps": None,
        }

    def stop(self) -> None:
        pass


@pytest.fixture
def base_url(api: LibgenAPI):
    server = LibgenServer(("127.0.0.1", 0), api, infra=StubInfra())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def get_json(url: str):
    with urllib.request.urlopen(url, timeout=10) as response:
        return response.status, response.headers, json.loads(response.read().decode())


def test_health_endpoint(base_url: str):
    status, _, data = get_json(base_url + "/api/health")
    assert status == 200
    assert data["status"] == "ok"
    assert data["healthy"] == 1


def test_infra_endpoint(base_url: str):
    status, _, data = get_json(base_url + "/api/infra")
    assert status == 200
    assert data["mirrors"] == []
    assert data["stats"]["download_attempts"] == 0
    assert data["chain"]["steps"][0]["step"] == "search"
    assert "libgen.li" in data["chain"]["mirrors"][0]


def test_index_page_is_served(base_url: str):
    with urllib.request.urlopen(base_url + "/", timeout=10) as response:
        body = response.read().decode()
    assert response.status == 200
    assert "text/html" in response.headers["Content-Type"]
    assert "Libgen" in body
    assert "/api/search" in body
    assert "googleapis" not in body
    assert "doubleclick" not in body


def test_search_endpoint(base_url: str):
    status, _, data = get_json(base_url + "/api/search?q=harry+potter&per_page=5")
    assert status == 200
    assert data["total"] == 2
    assert len(data["items"]) == 2
    assert data["items"][0]["title"]


def test_search_endpoint_requires_query(base_url: str):
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(base_url + "/api/search", timeout=10)
    assert excinfo.value.code == 400
    payload = json.loads(excinfo.value.read().decode())
    assert "q" in payload["error"]


def test_book_endpoint(base_url: str):
    status, _, data = get_json(base_url + "/api/book/147748488")
    assert status == 200
    assert data["title"].startswith("Python Essentials 1")
    assert data["description"]
    assert data["files"][0]["download_url"].startswith("/api/download/")


def test_book_endpoint_404(base_url: str):
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(base_url + "/api/book/999999999", timeout=10)
    assert excinfo.value.code == 404


def test_download_endpoint_streams_file(base_url: str, payload_md5: str):
    url = base_url + "/api/download/" + payload_md5
    with urllib.request.urlopen(url, timeout=10) as response:
        body = response.read()
        disposition = response.headers["Content-Disposition"]
    assert response.status == 200
    assert body == DOWNLOAD_PAYLOAD
    assert "attachment" in disposition
    assert "book.pdf" in disposition


def test_download_missing_file_returns_html_page(
    base_url: str, api: LibgenAPI, payload_md5: str
):
    api.client.missing_file = True
    url = base_url + "/api/download/" + payload_md5
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(url, timeout=10)
    assert excinfo.value.code == 404
    assert "text/html" in excinfo.value.headers["Content-Type"]
    body = excinfo.value.read().decode()
    assert "Файл недоступен" in body
    assert '"error"' not in body


def test_download_rejects_bad_md5(base_url: str):
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(base_url + "/api/download/not-an-md5", timeout=10)
    assert excinfo.value.code == 400


def test_cover_endpoint(base_url: str):
    with urllib.request.urlopen(
        base_url + "/api/cover/184c16eae281bf2ebea72eb6d7aa6f6d?libgen_id=4920181",
        timeout=10,
    ) as response:
        body = response.read()
    assert response.status == 200
    assert body.startswith(b"\xff\xd8\xff")


def test_cover_endpoint_404_when_missing(base_url: str, api: LibgenAPI, monkeypatch):
    from libgen_service.errors import NotFound

    def missing(*args, **kwargs):
        raise NotFound("no cover")

    monkeypatch.setattr(api, "cover", missing)
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(
            base_url + "/api/cover/184c16eae281bf2ebea72eb6d7aa6f6d?libgen_id=1",
            timeout=10,
        )
    assert excinfo.value.code == 404


def test_unknown_endpoint(base_url: str):
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(base_url + "/nope", timeout=10)
    assert excinfo.value.code == 404


def test_favicon(base_url: str):
    with urllib.request.urlopen(base_url + "/favicon.ico", timeout=10) as response:
        assert response.status == 204
