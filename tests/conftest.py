from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from libgen_service.api import LibgenAPI
from libgen_service.cache import TTLCache
from libgen_service.config import Settings

FIXTURES = Path(__file__).parent / "fixtures"
DOWNLOAD_PAYLOAD = (b"%PDF-1.4\n" + b"fake book content\n" * 200) + b"%%EOF\n"


class FakeResponse:
    def __init__(
        self,
        *,
        text: str = "",
        content: bytes = b"",
        url: str = "https://libgen.li/",
        status_code: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.text = text
        self.content = content
        self.url = url
        self.status_code = status_code
        self.headers = headers or {}
        self.mirror_base = "https://libgen.li"
        self.closed = False

    def iter_content(self, chunk_size: int = 65536):
        for start in range(0, len(self.content), chunk_size):
            yield self.content[start : start + chunk_size]

    def close(self) -> None:
        self.closed = True


class FakeClient:
    """Drop-in replacement for HttpClient backed by real captured fixtures."""

    def __init__(self, *, payload: bytes = DOWNLOAD_PAYLOAD, corrupt: bool = False) -> None:
        self.payload = payload
        self.corrupt = corrupt
        self.requests: list[tuple[str, str, dict]] = []

    def _json(self, name: str) -> dict:
        return json.loads((FIXTURES / name).read_text(encoding="utf-8-sig"))

    def get(self, path: str, **kwargs):
        params = kwargs.get("params") or {}
        self.requests.append(("GET", path, params))
        if path == "/json.php":
            ids = [i for i in str(params.get("ids", "")).split(",") if i]
            if params.get("object") == "e":
                source = "edition_full.json" if params.get("addkeys") == "*" else "editions_small.json"
            elif params.get("object") == "f":
                source = "files_small.json"
            else:
                source = "editions_small.json"
            data = self._json(source)
            filtered = {key: value for key, value in data.items() if key in ids}
            return FakeResponse(text=json.dumps(filtered), url="https://libgen.li/json.php")
        if "objects%5B%5D=e" in path and "curtab=e" in path:
            return FakeResponse(
                text=(FIXTURES / "editions_like.html").read_text(encoding="utf-8-sig"),
                url="https://libgen.li" + path,
            )
        if path.startswith("/file.php?md5="):
            md5 = path.split("md5=", 1)[-1]
            fid = {
                "184c16eae281bf2ebea72eb6d7aa6f6d": "100406413",
                "abed2924cd3eedf0fac0c8d94a22620a": "104838884",
            }.get(md5)
            html = (
                f'<a href="json.php?object=f&addkeys=*&ids={fid}">JSON</a>'
                if fid
                else "<html><body>File not found</body></html>"
            )
            return FakeResponse(text=html, url=f"https://libgen.li/file.php?md5={md5}")
        if path.startswith("/ads.php"):
            md5 = path.split("md5=", 1)[-1]
            html = (
                "<html><body>"
                f'<a href="get.php?md5={md5}&key=TESTKEY123">download</a>'
                '<a href="https://ads.example/tracker">ad</a>'
                "</body></html>"
            )
            return FakeResponse(text=html, url=f"https://libgen.li/ads.php?md5={md5}")
        if "get.php" in path:
            payload = b"corrupted bytes" if self.corrupt else self.payload
            return FakeResponse(
                content=payload,
                url=path,
                headers={
                    "Content-Type": "application/pdf",
                    "Content-Length": str(len(payload)),
                    "Content-Disposition": 'attachment; filename="book.pdf"',
                },
            )
        if path.startswith("/covers/"):
            return FakeResponse(
                content=b"\xff\xd8\xff\xe0fake-jpeg",
                url="https://libgen.li" + path,
                headers={"Content-Type": "image/jpeg"},
            )
        if path.startswith("/index.php"):
            return FakeResponse(
                text=(FIXTURES / "search.html").read_text(encoding="utf-8-sig"),
                url="https://libgen.li/index.php",
            )
        raise AssertionError(f"Unexpected GET {path}")

    def post(self, path: str, data: dict, **kwargs):
        self.requests.append(("POST", path, data))
        if path == "/batchsearchindex.php":
            if data.get("objects") == "f" and data.get("searchin") == "md5":
                html = (
                    "<table><tr><td>md5</td><td>x</td><td>1</td>"
                    "<td>104838884</td></tr></table>"
                )
            elif data.get("searchin") == "isbn":
                html = (FIXTURES / "batch_isbn_like.html").read_text(
                    encoding="utf-8-sig"
                )
            else:
                html = (FIXTURES / "batch_small.html").read_text(encoding="utf-8-sig")
            return FakeResponse(text=html, url="https://libgen.li/batchsearchindex.php")
        raise AssertionError(f"Unexpected POST {path}")

    def request(self, method: str, path: str, **kwargs):
        if method.upper() == "GET":
            return self.get(path, **kwargs)
        raise AssertionError(f"Unexpected {method} {path}")

    def mirrors(self):
        return [
            {
                "base": "https://libgen.li",
                "healthy": True,
                "failures": 0,
                "blocked_for": 0.0,
                "last_error": None,
                "last_ok": None,
            }
        ]

    def probe_all(self, timeout: float = 8.0):
        return []

    def close(self) -> None:
        pass


@pytest.fixture
def fake_client() -> FakeClient:
    return FakeClient()


@pytest.fixture
def api(tmp_path: Path, fake_client: FakeClient) -> LibgenAPI:
    settings = Settings(
        mirrors=("https://libgen.li",),
        cache_dir=tmp_path / "cache",
        cache_ttl=600,
        min_interval=0.0,
    )
    cache = TTLCache(tmp_path / "cache" / "cache.sqlite3", 600)
    instance = LibgenAPI(settings, client=fake_client, cache=cache)
    yield instance
    instance.close()


@pytest.fixture
def payload_md5() -> str:
    return hashlib.md5(DOWNLOAD_PAYLOAD).hexdigest()
