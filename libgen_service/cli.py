"""Command line interface for the Libgen helper service."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .api import LibgenAPI
from .config import Settings
from .errors import LibgenError
from .models import Book, human_size

__version__ = "1.0.0"


def _print_book(book: Book, index: int) -> None:
    best = book.best_file
    badges: list[str] = []
    if book.extensions:
        badges.append("/".join(book.extensions))
    if best and best.size:
        badges.append(human_size(best.size))
    if book.pages:
        badges.append(f"{book.pages} pp")
    if book.language_name:
        badges.append(book.language_name)
    if book.year:
        badges.append(book.year)
    meta = " · ".join(badges)
    print(f"{index:>3}. {book.title}")
    line = f"     {book.author or 'unknown author'}"
    if book.publisher:
        line += f" — {book.publisher}"
    print(line)
    if meta:
        print(f"     {meta}")
    if best:
        print(f"     md5 {best.md5}   edition {book.edition_id}   {best.size_display}")


def cmd_search(args: argparse.Namespace, api: LibgenAPI) -> int:
    result = api.search(
        args.query,
        mode=args.mode,
        topic=args.topic,
        ext=args.ext,
        lang=args.lang,
        year_from=args.year_from,
        year_to=args.year_to,
        sort=args.sort,
        page=args.page,
        per_page=args.per_page,
    )
    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        return 0
    if not result.items:
        print(f"Nothing found for {result.query!r} (database reports {result.total} records).")
        return 0
    print(
        f"Found {result.found} matching of {result.total} database records "
        f"· page {result.page}/{result.pages} · {result.elapsed_ms} ms"
        + (" · cached" if result.cached else "")
    )
    print()
    for index, book in enumerate(result.items, start=(result.page - 1) * result.per_page + 1):
        _print_book(book, index)
    print()
    print("Download with:  libgen-service get <md5>")
    return 0


def cmd_info(args: argparse.Namespace, api: LibgenAPI) -> int:
    target = args.target.strip()
    books: list[Book] = []
    file_info: dict[str, Any] | None = None
    if target.isdigit():
        books = [api.book(target)]
    else:
        books = api.book_by_md5(target)
        try:
            file_info = api.file(target)
        except LibgenError:
            file_info = None

    if args.json:
        payload: dict[str, Any] = {"books": [b.to_dict(detailed=True) for b in books]}
        if file_info:
            payload["file"] = file_info
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    for book in books:
        print(book.title)
        if book.series:
            print(f"  series:      {book.series}")
        print(f"  author:      {book.author}")
        print(f"  publisher:   {book.publisher}")
        print(f"  year:        {book.year}")
        print(f"  language:    {book.language_name or book.language}")
        print(f"  pages:       {book.pages}")
        if book.isbn:
            print(f"  isbn:        {', '.join(book.isbn)}")
        if book.doi:
            print(f"  doi:         {book.doi}")
        print(f"  edition id:  {book.edition_id}")
        print("  files:")
        for book_file in book.files:
            print(
                f"    - {book_file.ext:<5} {book_file.size_display:>8}  "
                f"md5 {book_file.md5}  {book_file.filename}"
            )
        if book.description:
            print("  description:")
            for line in book.description.splitlines()[:12]:
                print(f"    {line}")
        print()
    if file_info and not books:
        print(json.dumps(file_info, ensure_ascii=False, indent=2))
    return 0


def cmd_get(args: argparse.Namespace, api: LibgenAPI) -> int:
    def progress(written: int, total: int) -> None:
        if not sys.stdout.isatty():
            return
        if total:
            percent = written * 100 / total
            sys.stdout.write(f"\r  downloading… {percent:5.1f}%  ({human_size(written)})")
        else:
            sys.stdout.write(f"\r  downloading… {human_size(written)}")
        sys.stdout.flush()

    result = api.download_to(
        args.md5,
        args.output,
        verify=not args.no_verify,
    )
    if sys.stdout.isatty():
        sys.stdout.write("\r" + " " * 60 + "\r")
    print(f"Saved: {result['path']}")
    print(f"Size:  {human_size(result['size'])}")
    if result["verified"]:
        print("MD5:   verified")
    return 0


def cmd_serve(args: argparse.Namespace, api: LibgenAPI) -> int:
    from .server import serve

    serve(args.host, args.port, api=api, open_browser=args.open)
    return 0


def cmd_mirrors(args: argparse.Namespace, api: LibgenAPI) -> int:
    mirrors = api.mirrors(probe=args.probe)
    if args.json:
        print(json.dumps({"mirrors": mirrors}, ensure_ascii=False, indent=2))
        return 0
    for mirror in mirrors:
        state = "ok" if mirror["healthy"] else "down"
        print(
            f"{state:>4}  {mirror['base']:<24} "
            f"failures={mirror['failures']}  {mirror.get('last_error') or ''}"
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="libgen-service",
        description=(
            "Search Library Genesis and download books without ads or "
            "redirect pages. Includes a local web UI."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    search = subparsers.add_parser("search", help="search the catalogue")
    search.add_argument("query", help="title, author, ISBN, DOI or MD5")
    search.add_argument("--mode", default="auto", choices=["auto", "isbn", "md5", "doi", "edonkey"])
    search.add_argument("--topic", default="all", help="l, f, c, m, a, s, r or all")
    search.add_argument("--ext", default="", help="pdf, epub, djvu, ... (comma separated)")
    search.add_argument("--lang", default="", help="eng, rus, fre, ...")
    search.add_argument("--year-from", type=int, default=None)
    search.add_argument("--year-to", type=int, default=None)
    search.add_argument(
        "--sort",
        default="relevance",
        choices=["relevance", "year_desc", "year_asc", "title", "size_desc", "added_desc"],
    )
    search.add_argument("--page", type=int, default=1)
    search.add_argument("--per-page", type=int, default=20)
    search.add_argument("--json", action="store_true", help="machine readable output")
    search.set_defaults(func=cmd_search)

    info = subparsers.add_parser("info", help="show details for an edition or MD5")
    info.add_argument("target", help="edition id or MD5 hash")
    info.add_argument("--json", action="store_true")
    info.set_defaults(func=cmd_info)

    get = subparsers.add_parser("get", help="download a file by MD5")
    get.add_argument("md5")
    get.add_argument("-o", "--output", default=".", help="directory or file name")
    get.add_argument("--no-verify", action="store_true", help="skip MD5 verification")
    get.set_defaults(func=cmd_get)

    serve = subparsers.add_parser("serve", help="start the local web UI")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--open", action="store_true", help="open the browser")
    serve.set_defaults(func=cmd_serve)

    mirrors = subparsers.add_parser("mirrors", help="show mirror health")
    mirrors.add_argument("--probe", action="store_true", help="perform a live check")
    mirrors.add_argument("--json", action="store_true")
    mirrors.set_defaults(func=cmd_mirrors)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    api = LibgenAPI(Settings.load())
    try:
        return int(args.func(args, api) or 0)
    except LibgenError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    finally:
        api.close()
