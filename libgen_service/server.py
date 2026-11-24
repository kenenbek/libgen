"""Local HTTP service: JSON API + a clean web UI.

The browser only ever talks to this process.  Downloads are streamed through
it, so advertising interstitials and redirect chains never reach the client.
"""

from __future__ import annotations

import html
import json
import re
import threading
import time
import traceback
import webbrowser
from contextlib import contextmanager
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import parse_qs, quote, unquote, urlparse

from .api import DownloadStream, LibgenAPI
from .errors import (
    DownloadError,
    FileUnavailable,
    InvalidInput,
    LibgenError,
    LibgenUnavailable,
    NotFound,
)
from .infra import InfraMonitor, build_download_chain

WEB_DIR = Path(__file__).resolve().parent / "web"
INDEX_FILE = WEB_DIR / "index.html"


class LibgenServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        api: LibgenAPI,
        infra: InfraMonitor | None = None,
    ) -> None:
        super().__init__(address, LibgenRequestHandler)
        self.api = api
        self.infra = infra

    def server_close(self) -> None:
        monitor = self.infra
        if monitor is not None:
            try:
                monitor.stop()
            except Exception:  # noqa: BLE001 - shutdown must stay clean
                pass
        super().server_close()


class LibgenRequestHandler(BaseHTTPRequestHandler):
    server_version = "LibgenService/1.0"
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------- HTTP verbs
    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_HEAD(self) -> None:  # noqa: N802
        self._dispatch("HEAD")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    # ---------------------------------------------------------------- routing
    def _dispatch(self, method: str) -> None:
        started = time.monotonic()
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query, keep_blank_values=True)
        status = 200
        try:
            status = self._route(method, path, query)
        except InvalidInput as exc:
            status = self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except NotFound as exc:
            status = self._send_json({"error": str(exc)}, HTTPStatus.NOT_FOUND)
        except (LibgenUnavailable, DownloadError) as exc:
            status = self._send_json({"error": str(exc)}, HTTPStatus.BAD_GATEWAY)
        except LibgenError as exc:
            status = self._send_json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
            status = 499
        except Exception:  # pragma: no cover - last-resort safety net
            traceback.print_exc()
            status = self._send_json(
                {"error": "Internal server error"}, HTTPStatus.INTERNAL_SERVER_ERROR
            )
        elapsed = (time.monotonic() - started) * 1000
        self.log_message("%s %s -> %s (%.0f ms)", method, parsed.path, status, elapsed)

    def _route(self, method: str, path: str, query: dict[str, list[str]]) -> int:
        if path in ("/", "/index.html"):
            return self._serve_index()
        if path == "/favicon.ico":
            return self._send_bytes(b"", "image/x-icon", HTTPStatus.NO_CONTENT)
        if path == "/api/health":
            return self._send_json(self.server.api.health())
        if path == "/api/infra":
            return self._infra(query)
        if path == "/api/mirrors":
            probe = self._flag(query, "probe")
            return self._send_json({"mirrors": self.server.api.mirrors(probe=probe)})
        if path == "/api/search":
            return self._search(query)
        if path.startswith("/api/book/"):
            edition_id = path.rsplit("/", 1)[-1]
            book = self.server.api.book(edition_id)
            return self._send_json(book.to_dict(detailed=True))
        if path.startswith("/api/file/"):
            md5 = path.rsplit("/", 1)[-1]
            return self._send_json(self.server.api.file(md5))
        if path.startswith("/api/resolve/"):
            md5 = path.rsplit("/", 1)[-1]
            info = self.server.api.resolve(md5)
            return self._send_json(
                {"md5": info["md5"], "url": info["url"], "mirror": info.get("mirror")}
            )
        if path.startswith("/api/cover/"):
            md5 = path.rsplit("/", 1)[-1]
            return self._cover(md5, self._one(query, "libgen_id"))
        if path.startswith("/api/download/"):
            if method == "HEAD":
                return self._send_json(
                    {"error": "HEAD is not supported for downloads"},
                    HTTPStatus.METHOD_NOT_ALLOWED,
                )
            return self._download(path.rsplit("/", 1)[-1])
        if path == "/api/download":
            md5 = self._one(query, "md5") or ""
            return self._download(md5)
        return self._send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)

    # ------------------------------------------------------------------ pages
    def _serve_index(self) -> int:
        try:
            body = INDEX_FILE.read_bytes()
        except OSError:
            return self._send_json(
                {"error": "Web UI files are missing"}, HTTPStatus.INTERNAL_SERVER_ERROR
            )
        return self._send_bytes(
            body,
            "text/html; charset=utf-8",
            HTTPStatus.OK,
            cache="no-cache",
        )

    # --------------------------------------------------------------- endpoints
    def _infra(self, query: dict[str, list[str]]) -> int:
        monitor = self.server.infra
        if monitor is None:
            monitor = InfraMonitor(
                self.server.api.settings,
                self.server.api.cache,
                client=self.server.api.client,
            )
            self.server.infra = monitor
        report = monitor.report(
            refresh=self._flag(query, "refresh"),
            dumps=self._flag(query, "dumps"),
        )
        stats = self.server.api.download_stats()
        report["stats"] = stats
        report["chain"] = build_download_chain(self.server.api.settings, stats)
        return self._send_json(report)

    def _search(self, query: dict[str, list[str]]) -> int:
        q = (self._one(query, "q") or "").strip()
        if not q:
            raise InvalidInput("Missing query parameter 'q'")
        page = self._int(query, "page", 1)
        per_page = self._int(query, "per_page", 20)
        year_from = self._int(query, "year_from", None)
        year_to = self._int(query, "year_to", None)
        result = self.server.api.search(
            q,
            mode=self._one(query, "mode") or "auto",
            topic=self._one(query, "topic") or "all",
            ext=self._one(query, "ext"),
            lang=self._one(query, "lang"),
            year_from=year_from,
            year_to=year_to,
            sort=self._one(query, "sort") or "relevance",
            page=page,
            per_page=per_page,
        )
        return self._send_json(result.to_dict())

    def _cover(self, md5: str, libgen_id: str | None) -> int:
        try:
            data, content_type = self.server.api.cover(md5, libgen_id)
        except NotFound:
            # A 404 lets the UI draw its own typographic placeholder instead
            # of shipping a generic image.
            return self._send_bytes(
                b"",
                "image/svg+xml",
                HTTPStatus.NOT_FOUND,
                cache="public, max-age=60",
            )
        return self._send_bytes(
            data, content_type, HTTPStatus.OK, cache="public, max-age=604800"
        )

    def _download(self, md5: str) -> int:
        headers_sent = False
        try:
            with self._open_with_fallback(md5) as stream:
                filename = stream.filename
                ascii_name = filename.encode("ascii", "replace").decode("ascii")
                ascii_name = ascii_name.replace('"', "_")

                disposition = (
                    f'attachment; filename="{ascii_name}"; '
                    f"filename*=UTF-8''{quote(filename)}"
                )
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", stream.content_type)
                self.send_header("Content-Disposition", disposition)
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Access-Control-Allow-Origin", "*")
                if stream.size:
                    self.send_header("Content-Length", str(stream.size))
                else:
                    self.send_header("Connection", "close")
                    self.close_connection = True
                self.end_headers()
                headers_sent = True
                for chunk in stream.chunks():
                    self.wfile.write(chunk)
            return 200
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
            return 499
        except FileUnavailable:
            if headers_sent:
                self.close_connection = True
                return 500
            return self._download_error_page(
                md5,
                HTTPStatus.NOT_FOUND,
                "Файл недоступен",
                "Запись есть в каталоге, но самого файла нет на зеркалах "
                "Libgen. Попробуйте другое издание или другой формат.",
            )
        except (InvalidInput, NotFound) as exc:
            if headers_sent:
                self.close_connection = True
                return 500
            if isinstance(exc, InvalidInput):
                # Let the dispatcher map this to a JSON 400 response.
                raise
            return self._download_error_page(
                md5,
                HTTPStatus.NOT_FOUND,
                "Файл не найден",
                "Этого файла нет в каталоге. Возможно, ссылка устарела.",
            )
        except LibgenError as exc:
            if headers_sent:
                self.close_connection = True
                return 500
            return self._download_error_page(
                md5,
                HTTPStatus.BAD_GATEWAY,
                "Не удалось скачать файл",
                "Зеркала Libgen не ответили. Попробуйте ещё раз через минуту.",
                detail=str(exc),
            )

    @contextmanager
    def _open_with_fallback(self, md5: str) -> Iterator[DownloadStream]:
        """Open a file, falling back to another file of the same edition.

        Libgen keeps metadata even when the underlying file is gone.  When
        that happens we try the other copies of the same book before giving
        up, so a dead MD5 does not fail the whole download.
        """
        try:
            with self.server.api.open_download(md5) as stream:
                yield stream
            return
        except FileUnavailable:
            pass
        for candidate in self._sibling_md5s(md5):
            try:
                with self.server.api.open_download(candidate) as stream:
                    self.server.api.note_sibling_fallback(md5)
                    yield stream
                return
            except FileUnavailable:
                continue
        raise FileUnavailable("No copy of this file is available on Libgen")

    def _sibling_md5s(self, md5: str) -> list[str]:
        try:
            info = self.server.api.file(md5)
        except LibgenError:
            return []
        seen: list[str] = []
        for edition in info.get("editions") or []:
            for item in edition.get("files") or []:
                candidate = str(item.get("md5") or "").lower()
                if candidate and candidate != md5 and candidate not in seen:
                    seen.append(candidate)
        return seen

    def _download_error_page(
        self,
        md5: str,
        status: int,
        title: str,
        message: str,
        *,
        detail: str | None = None,
    ) -> int:
        detail_html = (
            f'<pre class="detail">{html.escape(detail)}</pre>' if detail else ""
        )
        body = f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)} — Libgen</title>
<style>
  :root {{ --ink:#111; --paper:#fff; --red:#e1000f; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; min-height:100vh; display:grid; place-items:center;
    background:var(--paper); color:var(--ink); padding:24px;
    font:16px/1.5 Helvetica,Arial,sans-serif; }}
  main {{ width:100%; max-width:640px; border-top:2px solid var(--ink);
    padding-top:24px; }}
  .eyebrow {{ margin:0 0 28px; font-size:11px; letter-spacing:.16em;
    text-transform:uppercase; color:#666; }}
  .eyebrow b {{ color:var(--red); font-weight:700; }}
  h1 {{ margin:0 0 16px; font-size:clamp(32px,7vw,56px); line-height:.95;
    letter-spacing:-.03em; font-weight:700; }}
  .lead {{ margin:0 0 24px; max-width:46ch; color:#333; }}
  .detail {{ margin:0 0 24px; padding:12px; background:#f4f4f4;
    border-left:3px solid var(--red); font:12px/1.5 ui-monospace,monospace;
    white-space:pre-wrap; word-break:break-word; color:#444; }}
  .actions {{ display:flex; flex-wrap:wrap; gap:12px; margin:0; }}
  .actions a {{ display:inline-block; padding:11px 18px; border:1px solid var(--ink);
    color:var(--ink); text-decoration:none; font-size:12px; letter-spacing:.08em;
    text-transform:uppercase; }}
  .actions a.primary {{ background:var(--ink); color:var(--paper); }}
  .actions a:hover {{ background:var(--red); border-color:var(--red); color:#fff; }}
</style>
</head>
<body>
<main>
  <p class="eyebrow">libgen <b>/</b> download</p>
  <h1>{html.escape(title)}</h1>
  <p class="lead">{html.escape(message)}</p>
  {detail_html}
  <p class="actions">
    <a class="primary" href="/?q={quote(md5)}">Искать это издание</a>
    <a href="/">Вернуться к поиску</a>
  </p>
</main>
</body>
</html>"""
        return self._send_bytes(
            body.encode("utf-8"),
            "text/html; charset=utf-8",
            status,
            cache="no-store",
        )

    # ------------------------------------------------------------------ output
    def _send_json(self, payload: Any, status: int = HTTPStatus.OK) -> int:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        return self._send_bytes(
            body,
            "application/json; charset=utf-8",
            status,
            cache="no-store",
            extra={"Access-Control-Allow-Origin": "*"},
        )

    def _send_bytes(
        self,
        body: bytes,
        content_type: str,
        status: int = HTTPStatus.OK,
        *,
        cache: str = "no-store",
        extra: dict[str, str] | None = None,
    ) -> int:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        if extra:
            for key, value in extra.items():
                self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD" and body:
            self.wfile.write(body)
        return int(status)

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _one(query: dict[str, list[str]], key: str) -> str | None:
        values = query.get(key)
        if not values:
            return None
        return values[0]

    @staticmethod
    def _flag(query: dict[str, list[str]], key: str) -> bool:
        value = (LibgenRequestHandler._one(query, key) or "").lower()
        return value in {"1", "true", "yes", "on"}

    @classmethod
    def _int(
        cls, query: dict[str, list[str]], key: str, default: int | None
    ) -> int | None:
        raw = cls._one(query, key)
        if raw is None or raw == "":
            return default
        try:
            return int(raw)
        except ValueError:
            raise InvalidInput(f"'{key}' must be an integer") from None

    def log_message(self, fmt: str, *args: Any) -> None:
        message = fmt % args
        print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def serve(
    host: str = "127.0.0.1",
    port: int = 8765,
    *,
    api: LibgenAPI | None = None,
    open_browser: bool = False,
) -> None:
    api = api or LibgenAPI()
    monitor = InfraMonitor(api.settings, api.cache, client=api.client)
    server = LibgenServer((host, port), api, infra=monitor)
    monitor.start()
    actual_host, actual_port = server.server_address[:2]
    url = f"http://{actual_host}:{actual_port}/"
    print(f"Libgen service is running at {url}")
    print("Press Ctrl+C to stop.")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        server.shutdown()
        server.server_close()
        api.close()
