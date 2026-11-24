"""High-level Libgen client used by the CLI and the HTTP server.

Design goals:

* never send the user to an advertising interstitial;
* fail over across mirrors transparently;
* cache aggressively so a local UI feels instant;
* expose only clean JSON to callers.
"""

from __future__ import annotations

import hashlib
import os
import re
import threading
import time
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urljoin, urlparse

from .cache import TTLCache
from .config import Settings
from .errors import (
    DownloadError,
    FileUnavailable,
    InvalidInput,
    LibgenError,
    LibgenUnavailable,
    NotFound,
)
from .http import HttpClient, strip_bom
from .models import EXT_PRIORITY, Book, BookFile, SearchResult, human_size, language_name
from .parsers import (
    parse_batch_ids,
    parse_batch_result,
    parse_content_disposition,
    parse_download_key,
    parse_edition_rows,
    parse_file_id,
    parse_json,
    parse_search_rows,
    parse_size,
    sanitize_filename,
)

MD5_RE = re.compile(r"^[0-9a-fA-F]{32}$")
EDITION_FIELDS = (
    "title,author,series_name,publisher,year,pages,cover_exists,cover_url,"
    "libgen_topic,type,doi,commentary,edition,title_add"
)
FILE_FIELDS = (
    "md5,filesize,extension,locator,pages,libgen_id,libgen_topic,time_added"
)
SEARCH_MODES = {"auto", "isbn", "md5", "doi", "edonkey"}
SORTS = {"relevance", "year_desc", "year_asc", "title", "size_desc", "added_desc"}
TOPIC_CODES = {"l", "f", "c", "m", "a", "s", "r", "w"}


@dataclass
class DownloadStream:
    filename: str
    size: int
    content_type: str
    _response: Any

    def chunks(self, size: int = 65536) -> Iterator[bytes]:
        for chunk in self._response.iter_content(chunk_size=size):
            if chunk:
                yield chunk


def _chunks(items: list[str], size: int) -> Iterator[list[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def cover_bucket(libgen_id: str | int) -> int:
    """Libgen stores covers in buckets of 1000 record ids."""
    return (int(libgen_id) // 1000) * 1000


def _split_list(value: str | None) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in re.split(r"[,;]", value) if part.strip()]


class LibgenAPI:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        client: HttpClient | None = None,
        cache: TTLCache | None = None,
    ) -> None:
        self.settings = settings or Settings.load()
        self.client = client or HttpClient(self.settings)
        self.cache = cache or TTLCache(
            self.settings.cache_dir / "cache.sqlite3", self.settings.cache_ttl
        )
        self._locks_guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
        self._stats_lock = threading.Lock()
        self._stats: dict[str, Any] = {
            "since": time.time(),
            "download_attempts": 0,
            "downloads_ok": 0,
            "dead_files": 0,
            "sibling_fallbacks": 0,
            "cdn_hosts": {},
            "last_cdn": None,
            "last_error": None,
        }
        self._load_stats()

    # ---------------------------------------------------------------- telemetry
    def _load_stats(self) -> None:
        stored = self.cache.get("stats:downloads")
        if not isinstance(stored, dict):
            return
        for key, value in stored.items():
            if key in self._stats:
                self._stats[key] = value

    def _save_stats(self) -> None:
        try:
            self.cache.set("stats:downloads", dict(self._stats), ttl=0)
        except Exception:  # noqa: BLE001 - telemetry must never break downloads
            pass

    def _note(self, key: str, amount: int = 1) -> None:
        with self._stats_lock:
            self._stats[key] = int(self._stats.get(key) or 0) + amount
            self._save_stats()

    def note_dead_file(self, md5: str) -> None:
        self._note("dead_files")
        with self._stats_lock:
            self._stats["last_error"] = f"file missing: {md5}"
            self._save_stats()

    def note_sibling_fallback(self, md5: str) -> None:
        self._note("sibling_fallbacks")
        with self._stats_lock:
            self._stats["last_error"] = f"fell back from {md5}"
            self._save_stats()

    def note_download_ok(self, md5: str, cdn_host: str | None = None) -> None:
        self._note("downloads_ok")
        if cdn_host:
            with self._stats_lock:
                hosts = self._stats.setdefault("cdn_hosts", {})
                hosts[cdn_host] = int(hosts.get(cdn_host) or 0) + 1
                self._stats["last_cdn"] = cdn_host
                self._save_stats()

    def note_download_error(self, message: str) -> None:
        with self._stats_lock:
            self._stats["last_error"] = message[:300]
            self._save_stats()

    def download_stats(self) -> dict[str, Any]:
        with self._stats_lock:
            data = dict(self._stats)
            data["cdn_hosts"] = dict(self._stats.get("cdn_hosts") or {})
        attempts = int(data.get("download_attempts") or 0)
        dead = int(data.get("dead_files") or 0)
        ok = int(data.get("downloads_ok") or 0)
        data["dead_file_rate"] = round(dead / attempts, 4) if attempts else 0.0
        data["success_rate"] = round(ok / attempts, 4) if attempts else 0.0
        return data

    # ------------------------------------------------------------------ utils
    def _lock_for(self, key: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks[key]

    def _get_json(self, path: str, params: dict[str, str]) -> dict:
        response = self.client.get(path, params=params)
        try:
            data = parse_json(strip_bom(response.text))
        finally:
            response.close()
        if isinstance(data, dict):
            return data
        if isinstance(data, list):
            return {}
        return {}

    # ----------------------------------------------------------------- search
    def search(
        self,
        query: str,
        *,
        mode: str = "auto",
        topic: str = "all",
        ext: str | list[str] | None = None,
        lang: str | None = None,
        year_from: int | None = None,
        year_to: int | None = None,
        sort: str = "relevance",
        page: int = 1,
        per_page: int = 20,
    ) -> SearchResult:
        query = (query or "").strip()
        if not query:
            raise InvalidInput("Search query is empty")
        if len(query) > 300:
            raise InvalidInput("Search query is too long")

        mode = (mode or "auto").lower()
        if mode not in SEARCH_MODES:
            raise InvalidInput(f"Unknown search mode: {mode}")
        if mode == "auto" and MD5_RE.match(query):
            mode = "md5"
        if mode == "md5" and not MD5_RE.match(query):
            raise InvalidInput("MD5 search expects a 32 character hash")
        if mode == "isbn":
            query = re.sub(r"[^0-9Xx]", "", query)
            if not query:
                raise InvalidInput("ISBN search expects digits")

        topic = (topic or "all").lower()
        if topic not in TOPIC_CODES and topic != "all":
            raise InvalidInput(f"Unknown topic: {topic}")

        sort = (sort or "relevance").lower()
        if sort not in SORTS:
            raise InvalidInput(f"Unknown sort order: {sort}")

        extensions = {
            e.lower().lstrip(".")
            for e in (ext if isinstance(ext, list) else _split_list(ext))
        }
        language = (lang or "").strip().lower()
        page = max(1, int(page or 1))
        per_page = min(100, max(1, int(per_page or 20)))

        started = time.monotonic()
        ids, total, ids_cached, rows = self._search_ids(query, mode, topic)

        need = page * per_page
        collected: list[Book] = []
        if rows:
            # The batch endpoint was unavailable; the classic HTML table
            # already contains everything needed for a result list.
            collected = [self._book_from_row(row) for row in rows]
            collected = [
                book
                for book in collected
                if self._matches(book, topic, extensions, language, year_from, year_to)
            ]
        else:
            position = 0
            while position < len(ids) and len(collected) < need and position < 800:
                batch = ids[position : position + 60]
                position += 60
                try:
                    books = self._load_books(batch, mode)
                except LibgenUnavailable:
                    if collected:
                        break
                    raise
                for book in books:
                    if self._matches(
                        book, topic, extensions, language, year_from, year_to
                    ):
                        collected.append(book)

        collected = self._sort_books(collected, sort)
        start = (page - 1) * per_page
        items = collected[start : start + per_page]
        elapsed = int((time.monotonic() - started) * 1000)
        return SearchResult(
            query=query,
            mode=mode,
            total=total,
            found=len(collected),
            page=page,
            per_page=per_page,
            items=items,
            mirror=self._current_mirror(),
            cached=ids_cached,
            elapsed_ms=elapsed,
            filters={
                "topic": topic,
                "ext": sorted(extensions),
                "lang": language,
                "year_from": year_from,
                "year_to": year_to,
                "sort": sort,
            },
        )

    def _search_ids(
        self, query: str, mode: str, topic: str
    ) -> tuple[list[str], int, bool, list[dict[str, Any]]]:
        cache_key = f"ids:{mode}:{topic}:{query.lower()}"
        cached = self.cache.get(cache_key)
        if cached and isinstance(cached.get("ids"), list):
            rows = self.cache.get(f"htmlrows:{query.lower()}") or []
            return (
                [str(i) for i in cached["ids"]],
                int(cached.get("total", 0)),
                True,
                rows if isinstance(rows, list) else [],
            )

        searchin = "auto" if mode == "auto" else mode
        objects = "f" if searchin in {"md5", "edonkey"} else "e"
        data = {
            "objects": objects,
            "searchin": searchin,
            "strings": query,
        }
        if any(ch in query for ch in ('"', "*")):
            data["gmode"] = "on"
        if topic != "all":
            data["topics[]"] = topic

        try:
            response = self.client.post("/batchsearchindex.php", data=data)
            try:
                html = strip_bom(response.text)
            finally:
                response.close()
            total, ids, link = parse_batch_result(html)
        except LibgenUnavailable:
            raise
        except Exception as exc:  # pragma: no cover - defensive
            raise LibgenUnavailable(f"Batch search failed: {exc}") from exc

        rows: list[dict[str, Any]] = []
        if not ids:
            # Libgen sometimes reports a count without listing ids (ISBN, DOI,
            # MD5 modes).  Follow the search link it provides and parse the
            # regular results table instead.  Older mirrors may not implement
            # the batch endpoint at all, hence the second branch.
            rows = self._search_html(
                query, mode=mode, link=link if total else ""
            )
        if not ids and rows:
            file_rows = [row for row in rows if row.get("md5")]
            if file_rows:
                total = total or len(file_rows)
                ids = [
                    row["edition_id"]
                    for row in file_rows
                    if row.get("edition_id")
                ]
                rows = file_rows
                self.cache.set(
                    f"htmlrows:{query.lower()}",
                    rows,
                    ttl=min(300, self.settings.cache_ttl),
                )
            else:
                # Edition rows have no download links; enrich them via JSON.
                total = total or len(rows)
                ids = [row["edition_id"] for row in rows if row.get("edition_id")]
                rows = []

        ids = ids[:2000]
        self.cache.set(cache_key, {"total": total, "ids": ids})
        return ids, total, False, rows

    def _search_html(
        self, query: str, *, mode: str = "auto", link: str = ""
    ) -> list[dict[str, Any]]:
        try:
            if link:
                path = urljoin("/", link)
                if "objects%5B%5D=e" in path or "objects[]=e" in path:
                    if "curtab=" not in path:
                        path += ("&" if "?" in path else "?") + "curtab=e"
                response = self.client.get(
                    path,
                    headers={"Referer": "__BASE__/batchsearchindex.php"},
                )
            else:
                response = self.client.get(
                    "/index.php",
                    params={"req": query, "res": 50, "page": 1},
                    headers={"Referer": "__BASE__/"},
                )
            try:
                html = strip_bom(response.text)
            finally:
                response.close()
            rows = parse_search_rows(html)
            if not rows:
                rows = parse_edition_rows(html)
            return rows
        except LibgenError:
            return []

    def _matches(
        self,
        book: Book,
        topic: str,
        extensions: set[str],
        language: str,
        year_from: int | None,
        year_to: int | None,
    ) -> bool:
        if topic != "all" and book.topic != topic:
            return False
        if extensions:
            if not any(f.ext.lower() in extensions for f in book.files):
                return False
        if language:
            if language not in {book.language.lower(), book.language_name.lower()}:
                return False
        if year_from or year_to:
            try:
                year = int(book.year)
            except (TypeError, ValueError):
                return False
            if year_from and year < int(year_from):
                return False
            if year_to and year > int(year_to):
                return False
        return True

    def _sort_books(self, books: list[Book], sort: str) -> list[Book]:
        if sort == "relevance":
            return books

        def year_key(book: Book) -> int:
            try:
                return int(book.year)
            except (TypeError, ValueError):
                return 0

        if sort == "year_desc":
            return sorted(books, key=year_key, reverse=True)
        if sort == "year_asc":
            return sorted(books, key=year_key)
        if sort == "title":
            return sorted(books, key=lambda b: b.title.casefold())
        if sort == "size_desc":
            return sorted(
                books, key=lambda b: max((f.size for f in b.files), default=0), reverse=True
            )
        if sort == "added_desc":
            return sorted(
                books,
                key=lambda b: max((f.added for f in b.files), default=""),
                reverse=True,
            )
        return books

    # --------------------------------------------------------------- metadata
    def _load_books(self, ids: list[str], mode: str) -> list[Book]:
        if mode == "md5":
            return self._load_books_from_files(ids)
        editions = self._editions(ids)
        file_ids: list[str] = []
        for edition in editions.values():
            for item in (edition.get("files") or {}).values():
                fid = str(item.get("f_id") or "")
                if fid:
                    file_ids.append(fid)
        files = self._files(file_ids)
        books: list[Book] = []
        for index, edition_id in enumerate(ids):
            edition = editions.get(edition_id)
            if not edition:
                continue
            book = self._book_from(edition_id, edition, files)
            book.relevance = index
            books.append(book)
        return books

    def _load_books_from_files(self, file_ids: list[str]) -> list[Book]:
        files = self._files(file_ids)
        edition_ids: list[str] = []
        for fid in file_ids:
            record = files.get(fid) or {}
            for item in (record.get("editions") or {}).values():
                eid = str(item.get("e_id") or "")
                if eid and eid not in edition_ids:
                    edition_ids.append(eid)
        if not edition_ids:
            return []
        editions = self._editions(edition_ids)
        books: list[Book] = []
        for index, eid in enumerate(edition_ids):
            edition = editions.get(eid)
            if not edition:
                continue
            book = self._book_from(eid, edition, files)
            book.relevance = index
            books.append(book)
        return books

    def _editions(self, ids: list[str]) -> dict[str, dict]:
        result: dict[str, dict] = {}
        missing: list[str] = []
        for eid in ids:
            eid = str(eid)
            cached = self.cache.get(f"ed:{eid}")
            if isinstance(cached, dict) and cached:
                result[eid] = cached
            else:
                missing.append(eid)
        for batch in _chunks(missing, 40):
            params = {
                "object": "e",
                "ids": ",".join(batch),
                "addkeys": "101,401,505",
                "fields": EDITION_FIELDS,
            }
            try:
                data = self._get_json("/json.php", params)
            except LibgenError:
                if result:
                    continue
                raise
            for eid, payload in data.items():
                if not isinstance(payload, dict):
                    continue
                result[str(eid)] = payload
                self.cache.set(f"ed:{eid}", payload, ttl=6 * 3600)
        return result

    def _editions_full(self, ids: list[str]) -> dict[str, dict]:
        result: dict[str, dict] = {}
        missing: list[str] = []
        for eid in ids:
            eid = str(eid)
            cached = self.cache.get(f"edfull:{eid}")
            if isinstance(cached, dict) and cached:
                result[eid] = cached
            else:
                missing.append(eid)
        for batch in _chunks(missing, 10):
            params = {"object": "e", "ids": ",".join(batch), "addkeys": "*"}
            try:
                data = self._get_json("/json.php", params)
            except LibgenError:
                if result:
                    continue
                raise
            for eid, payload in data.items():
                if not isinstance(payload, dict):
                    continue
                result[str(eid)] = payload
                self.cache.set(f"edfull:{eid}", payload, ttl=6 * 3600)
        return result

    def _files(self, ids: list[str]) -> dict[str, dict]:
        result: dict[str, dict] = {}
        missing: list[str] = []
        for fid in ids:
            fid = str(fid)
            cached = self.cache.get(f"f:{fid}")
            if isinstance(cached, dict) and cached:
                result[fid] = cached
            else:
                missing.append(fid)
        for batch in _chunks(missing, 100):
            params = {
                "object": "f",
                "ids": ",".join(batch),
                "fields": FILE_FIELDS,
            }
            try:
                data = self._get_json("/json.php", params)
            except LibgenError:
                if result:
                    continue
                raise
            for fid, payload in data.items():
                if not isinstance(payload, dict):
                    continue
                result[str(fid)] = payload
                self.cache.set(f"f:{fid}", payload, ttl=6 * 3600)
        return result

    def _book_from(
        self, edition_id: str, edition: dict, files: dict[str, dict]
    ) -> Book:
        additions = edition.get("add") or {}

        def add_values(key: str) -> list[str]:
            values: list[str] = []
            for item in additions.values():
                if str(item.get("key")) == key:
                    value = str(item.get("value") or "").strip()
                    if value and value not in values:
                        values.append(value)
            return values

        def add_value(key: str) -> str:
            values = add_values(key)
            return values[0] if values else ""

        book_files: list[BookFile] = []
        for item in (edition.get("files") or {}).values():
            fid = str(item.get("f_id") or "")
            meta = files.get(fid) or {}
            md5 = str(meta.get("md5") or item.get("md5") or "").lower()
            if not md5:
                continue
            try:
                pages = int(meta.get("pages") or edition.get("pages") or 0)
            except (TypeError, ValueError):
                pages = 0
            locator = str(meta.get("locator") or "")
            filename = re.split(r"[\\/]", locator)[-1] if locator else ""
            book_files.append(
                BookFile(
                    md5=md5,
                    file_id=fid,
                    ext=str(meta.get("extension") or "").lower(),
                    size=int(meta.get("filesize") or 0),
                    pages=pages,
                    filename=filename,
                    topic=str(meta.get("libgen_topic") or edition.get("libgen_topic") or ""),
                    added=str(meta.get("time_added") or item.get("time_added") or ""),
                    edition_id=str(edition_id),
                    libgen_id=str(meta.get("libgen_id") or ""),
                )
            )
        book_files.sort(
            key=lambda f: (EXT_PRIORITY.get(f.ext, len(EXT_PRIORITY)), -f.size)
        )

        try:
            pages = int(edition.get("pages") or 0)
        except (TypeError, ValueError):
            pages = 0

        return Book(
            edition_id=str(edition_id),
            title=str(edition.get("title") or "").strip(),
            subtitle=str(edition.get("title_add") or "").strip(),
            series=str(edition.get("series_name") or "").strip(),
            author=(
                str(edition.get("author") or "").strip()
                or ", ".join(add_values("401"))
            ),
            publisher=str(edition.get("publisher") or "").strip(),
            year=str(edition.get("year") or "").strip(),
            language=add_value("101"),
            pages=pages,
            isbn=[x for x in re.split(r"[;,\s]+", add_value("505")) if x],
            doi=str(edition.get("doi") or "").strip(),
            description=add_value("996"),
            toc=add_value("995"),
            topic=str(edition.get("libgen_topic") or ""),
            kind=str(edition.get("type") or ""),
            files=book_files,
            cover_exists=str(edition.get("cover_exists") or "") == "1",
        )

    def _book_from_row(self, row: dict[str, Any]) -> Book:
        """Build a result card from a parsed HTML search row."""
        cover_bucket = ""
        match = re.search(r"/covers/(\d+)/", str(row.get("cover_url") or ""))
        if match:
            cover_bucket = match.group(1)
        return Book(
            edition_id=str(row.get("edition_id") or ""),
            title=str(row.get("title") or ""),
            series=str(row.get("series") or ""),
            author=str(row.get("author") or ""),
            publisher=str(row.get("publisher") or ""),
            year=str(row.get("year") or ""),
            language=str(row.get("language") or ""),
            pages=int(row.get("pages") or 0),
            isbn=list(row.get("isbn") or []),
            topic=str(row.get("topic") or ""),
            kind=str(row.get("kind") or ""),
            cover_exists=bool(row.get("cover_url")),
            files=[
                BookFile(
                    md5=str(row.get("md5") or ""),
                    file_id=str(row.get("file_id") or ""),
                    ext=str(row.get("ext") or ""),
                    size=int(row.get("size") or 0),
                    pages=int(row.get("pages") or 0),
                    topic=str(row.get("topic") or ""),
                    edition_id=str(row.get("edition_id") or ""),
                    libgen_id=cover_bucket,
                )
            ],
        )

    # ------------------------------------------------------------------ detail
    def book(self, edition_id: str) -> Book:
        edition_id = str(edition_id).strip()
        if not edition_id.isdigit():
            raise InvalidInput("Edition id must be numeric")
        editions = self._editions_full([edition_id])
        edition = editions.get(edition_id)
        if not edition:
            raise NotFound(f"Edition {edition_id} not found")
        file_ids = [
            str(item.get("f_id"))
            for item in (edition.get("files") or {}).values()
            if item.get("f_id")
        ]
        files = self._files(file_ids)
        return self._book_from(edition_id, edition, files)

    def file(self, md5: str) -> dict[str, Any]:
        md5 = self._validate_md5(md5)
        fid = self._file_id_for_md5(md5)
        if not fid:
            raise NotFound(f"No file with MD5 {md5}")
        data = self._files([fid])
        record = data.get(fid)
        if not record:
            raise NotFound(f"No file with MD5 {md5}")
        edition_ids = [
            str(item.get("e_id"))
            for item in (record.get("editions") or {}).values()
            if item.get("e_id")
        ]
        books = []
        if edition_ids:
            editions = self._editions(edition_ids)
            for eid, edition in editions.items():
                books.append(self._book_from(eid, edition, data).to_dict(detailed=True))
        return {
            "md5": md5,
            "file_id": fid,
            "extension": record.get("extension"),
            "size": int(record.get("filesize") or 0),
            "size_display": human_size(record.get("filesize")),
            "pages": int(record.get("pages") or 0),
            "filename": record.get("locator"),
            "libgen_id": record.get("libgen_id"),
            "topic": record.get("libgen_topic"),
            "added": record.get("time_added"),
            "editions": books,
            "download_url": f"/api/download/{md5}",
        }

    def book_by_md5(self, md5: str) -> list[Book]:
        md5 = self._validate_md5(md5)
        fid = self._file_id_for_md5(md5)
        if not fid:
            raise NotFound(f"No file with MD5 {md5}")
        record = self._files([fid]).get(fid) or {}
        edition_ids = [
            str(item.get("e_id"))
            for item in (record.get("editions") or {}).values()
            if item.get("e_id")
        ]
        if not edition_ids:
            raise NotFound(f"File {md5} has no edition record")
        editions = self._editions(edition_ids)
        files = {fid: record}
        return [
            self._book_from(eid, editions[eid], files)
            for eid in edition_ids
            if eid in editions
        ]

    def _file_id_for_md5(self, md5: str) -> str:
        cached = self.cache.get(f"fid:{md5}")
        if cached:
            return str(cached)
        # The batch endpoint reports a match but omits the file id, while
        # file.php?md5= renders the full record (including "File ID:").
        response = self.client.get(
            f"/file.php?md5={md5}", headers={"Referer": "__BASE__/"}
        )
        try:
            html = strip_bom(response.text)
        finally:
            response.close()
        fid = parse_file_id(html)
        if not fid:
            return ""
        self.cache.set(f"fid:{md5}", fid, ttl=24 * 3600)
        return fid

    # ---------------------------------------------------------------- download
    def _validate_md5(self, md5: str) -> str:
        md5 = (md5 or "").strip().lower()
        if not MD5_RE.match(md5):
            raise InvalidInput("MD5 must be 32 hexadecimal characters")
        return md5

    def resolve(self, md5: str, *, force: bool = False) -> dict[str, Any]:
        """Resolve an MD5 to the real CDN link, skipping the ad page."""
        md5 = self._validate_md5(md5)
        cache_key = f"dl:{md5}"
        if not force:
            cached = self.cache.get(cache_key)
            if isinstance(cached, dict) and cached.get("url"):
                return cached

        lock = self._lock_for(cache_key)
        with lock:
            if not force:
                cached = self.cache.get(cache_key)
                if isinstance(cached, dict) and cached.get("url"):
                    return cached
            response = self.client.get(
                f"/ads.php?md5={md5}",
                headers={"Referer": "__BASE__/"},
            )
            try:
                html = strip_bom(response.text)
                ads_url = response.url
                mirror = getattr(response, "mirror_base", "")
            finally:
                response.close()
            key = parse_download_key(html, md5)
            if not key:
                raise NotFound(
                    "This mirror did not return a download link for the file"
                )
            info = {
                "md5": md5,
                "url": urljoin(ads_url, key),
                "ads_url": ads_url,
                "mirror": mirror,
                "resolved_at": time.time(),
            }
            self.cache.set(cache_key, info, ttl=300)
            return info

    @contextmanager
    def open_download(self, md5: str) -> Iterator[DownloadStream]:
        md5 = self._validate_md5(md5)
        self._note("download_attempts")
        errors: list[str] = []
        for attempt in range(3):
            try:
                info = self.resolve(md5, force=attempt > 0)
            except FileUnavailable:
                self.note_dead_file(md5)
                raise
            except LibgenUnavailable as exc:
                errors.extend(exc.errors or [str(exc)])
                continue
            except LibgenError as exc:
                errors.append(str(exc))
                continue
            try:
                response = self.client.request(
                    "GET",
                    info["url"],
                    headers={
                        "Referer": info.get("ads_url") or "https://libgen.li/",
                        "Accept": "*/*",
                    },
                    stream=True,
                    allow_redirects=True,
                    timeout=max(60.0, self.settings.timeout),
                )
            except FileUnavailable:
                # No mirror has this file; retrying only wastes time.
                self.note_dead_file(md5)
                raise
            except LibgenUnavailable as exc:
                errors.extend(exc.errors or [str(exc)])
                self.cache.delete(f"dl:{md5}")
                continue
            except LibgenError as exc:
                errors.append(str(exc))
                self.cache.delete(f"dl:{md5}")
                continue

            content_type = (response.headers.get("Content-Type") or "").lower()
            if "text/html" in content_type or response.status_code >= 400:
                # Some CDN nodes answer with an HTML error page even on 200.
                try:
                    body = response.text[:4000]
                except Exception:  # noqa: BLE001
                    body = ""
                response.close()
                if "file not found in db" in body.lower():
                    self.note_dead_file(md5)
                    raise FileUnavailable(
                        "The file is missing from Libgen storage "
                        "(File not found in DB)"
                    )
                errors.append(f"mirror returned {content_type or response.status_code}")
                self.cache.delete(f"dl:{md5}")
                continue

            filename = parse_content_disposition(
                response.headers.get("Content-Disposition")
            )
            if not filename:
                filename = self._fallback_filename(md5)
            try:
                size = int(response.headers.get("Content-Length") or 0)
            except ValueError:
                size = 0

            stream = DownloadStream(
                filename=sanitize_filename(filename, fallback=f"{md5}"),
                size=size,
                content_type=content_type or "application/octet-stream",
                _response=response,
            )
            cdn_host = urlparse(getattr(response, "url", "") or "").hostname
            self.note_download_ok(md5, cdn_host)
            try:
                yield stream
            finally:
                response.close()
            return
        unique = list(dict.fromkeys(errors))[-4:]
        message = "Could not download the file. " + (
            "; ".join(unique) or "unknown error"
        )
        self.note_download_error(message)
        raise DownloadError(message)

    def check_availability(self, md5: str) -> dict[str, Any]:
        """Cheap probe: can this file actually be downloaded right now?

        Opens the CDN stream, reads only the headers and closes.  The result
        is cached: "missing" files occasionally reappear, so even a negative
        answer expires after 15 minutes.
        """
        md5 = self._validate_md5(md5)
        cache_key = f"avail:{md5}"
        cached = self.cache.get(cache_key)
        if isinstance(cached, dict) and cached.get("status"):
            return cached
        result: dict[str, Any] = {
            "md5": md5,
            "status": "unknown",
            "checked_at": time.time(),
        }
        try:
            info = self.resolve(md5)
            response = self.client.request(
                "GET",
                info["url"],
                headers={
                    "Referer": info.get("ads_url") or "https://libgen.li/",
                    "Accept": "*/*",
                    "Range": "bytes=0-0",
                },
                stream=True,
                allow_redirects=True,
                timeout=max(30.0, self.settings.timeout),
                attempts=1,
            )
            try:
                content_type = (response.headers.get("Content-Type") or "").lower()
                ok = response.status_code < 400 and "text/html" not in content_type
                result["status"] = "ok" if ok else "unknown"
                result["size"] = int(response.headers.get("Content-Length") or 0)
            finally:
                response.close()
        except FileUnavailable:
            result["status"] = "missing"
        except LibgenError:
            result["status"] = "unknown"
        # Do not pin "unknown" for long: it usually means a flaky mirror.
        self.cache.set(cache_key, result, ttl=900 if result["status"] != "unknown" else 60)
        return result

    def _fallback_filename(self, md5: str) -> str:
        try:
            fid = self._file_id_for_md5(md5)
            record = self._files([fid]).get(fid) if fid else None
            if record:
                locator = str(record.get("locator") or "")
                ext = str(record.get("extension") or "")
                if locator:
                    return locator if "." in locator else f"{locator}.{ext}"
                if ext:
                    return f"{md5}.{ext}"
        except LibgenError:
            pass
        return f"{md5}.bin"

    def download_to(
        self,
        md5: str,
        destination: str | Path,
        *,
        verify: bool = True,
        progress: Any = None,
    ) -> dict[str, Any]:
        md5 = self._validate_md5(md5)
        destination = Path(destination).expanduser()
        if destination.suffix:
            target_dir = destination.parent
            requested_name = destination.name
        else:
            target_dir = destination
            requested_name = ""
        target_dir.mkdir(parents=True, exist_ok=True)

        with self.open_download(md5) as stream:
            filename = sanitize_filename(requested_name or stream.filename)
            path = self._unique_path(target_dir / filename)
            part = path.with_name(path.name + ".part")
            digest = hashlib.md5()
            written = 0
            try:
                with part.open("wb") as handle:
                    for chunk in stream.chunks():
                        handle.write(chunk)
                        digest.update(chunk)
                        written += len(chunk)
                        if progress is not None:
                            progress(written, stream.size)
                if verify and digest.hexdigest().lower() != md5:
                    raise DownloadError(
                        "Downloaded file is corrupted (MD5 mismatch)"
                    )
                part.replace(path)
            except BaseException:
                part.unlink(missing_ok=True)
                raise
        return {
            "path": str(path),
            "filename": path.name,
            "size": written,
            "md5": md5,
            "verified": bool(verify),
        }

    def _unique_path(self, path: Path) -> Path:
        if not path.exists():
            return path
        stem, suffix = path.stem, path.suffix
        for index in range(1, 1000):
            candidate = path.with_name(f"{stem} ({index}){suffix}")
            if not candidate.exists():
                return candidate
        return path

    # ------------------------------------------------------------------- cover
    def cover(self, md5: str, libgen_id: str | None = None) -> tuple[bytes, str]:
        md5 = self._validate_md5(md5)
        cache_path = self.settings.cache_dir / "covers" / f"{md5}.jpg"
        if cache_path.exists() and time.time() - cache_path.stat().st_mtime < 7 * 86400:
            return cache_path.read_bytes(), "image/jpeg"

        if not libgen_id:
            try:
                fid = self._file_id_for_md5(md5)
                record = self._files([fid]).get(fid) if fid else None
                libgen_id = str(record.get("libgen_id") or "") if record else ""
            except LibgenError:
                libgen_id = ""
        if not libgen_id or not str(libgen_id).isdigit():
            raise NotFound("Cover location is unknown")

        bucket = cover_bucket(libgen_id)
        for suffix in ("", "_small"):
            path = f"/covers/{bucket}/{md5}{suffix}.jpg"
            try:
                response = self.client.get(
                    path, headers={"Referer": "__BASE__/index.php"}
                )
            except LibgenError:
                continue
            content_type = (response.headers.get("Content-Type") or "").lower()
            try:
                if response.status_code == 200 and content_type.startswith("image/"):
                    data = response.content
                    if data:
                        cache_path.parent.mkdir(parents=True, exist_ok=True)
                        cache_path.write_bytes(data)
                        return data, content_type or "image/jpeg"
            finally:
                response.close()
        raise NotFound("Cover image is not available")

    # ------------------------------------------------------------------ status
    def _current_mirror(self) -> str:
        mirrors = self.client.mirrors()
        for mirror in mirrors:
            if mirror.get("healthy"):
                return str(mirror.get("base") or "")
        return str(mirrors[0].get("base") or "") if mirrors else ""

    def mirrors(self, *, probe: bool = False) -> list[dict[str, Any]]:
        if probe:
            self.client.probe_all()
        return self.client.mirrors()

    def health(self) -> dict[str, Any]:
        mirrors = self.client.mirrors()
        healthy = [m for m in mirrors if m.get("healthy")]
        return {
            "status": "ok" if healthy else "degraded",
            "mirrors": mirrors,
            "healthy": len(healthy),
            "cache": "sqlite" if self.cache.persistent else "disabled",
        }

    def close(self) -> None:
        self.client.close()
        self.cache.close()

    def __enter__(self) -> "LibgenAPI":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()
